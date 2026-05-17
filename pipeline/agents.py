"""
Stage 3: Worker / TeamSupervisor / SubSupervisor agents.

Mirrors nightly_preprocessing/src/engine/multi_agent_engine.py:
  - WorkerAgent generates one summary section.
  - TeamSupervisorAgent runs structure check (Call 1) + claim split (Call 2),
    then dispatches SubSupervisorAgents to verify each claim against the
    PatientRecord ground truth (instead of a SQLite DB).
  - Up to 2 revision rounds per section.

Key difference from nightly: workers don't have tool access. They get a fully
prepared, filtered slice of the structured record so they can only reference
verified facts. This is the strongest hallucination guard for an extractive
summary task.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterator, Optional

from . import prompts_agents as P
from .llm import OllamaClient
from .schema import PatientRecord, TimelineEvent


# ──────────────────────────────────────────────────────────────────────
# Helpers for shaping PatientRecord → worker user prompts
# ──────────────────────────────────────────────────────────────────────

def _events_to_md(events: list[TimelineEvent]) -> str:
    if not events:
        return "（無）"
    lines = []
    for e in events:
        date = e.date or "?"
        lines.append(f"- [{e.category}] {date} — {e.event}（原文：「{e.raw_quote[:120]}」）")
    return "\n".join(lines)


def _diagnoses_to_md(diags) -> str:
    if not diags:
        return "（無）"
    return "\n".join(f"{i+1}. {d.text}" for i, d in enumerate(diags))


def _labs_to_md(labs) -> str:
    if not labs:
        return "（無顯著異常）"
    # Group by item, show min/max + last
    from collections import defaultdict
    by_item: dict[str, list] = defaultdict(list)
    for l in labs:
        by_item[l.item].append(l)
    out = []
    for item, vals in by_item.items():
        nums = []
        for v in vals:
            try:
                nums.append((v.date, float(v.value.lstrip("<>")), v.flag, v.unit))
            except ValueError:
                pass
        if not nums:
            continue
        nums.sort()
        lowest = min(nums, key=lambda t: t[1])
        highest = max(nums, key=lambda t: t[1])
        unit = lowest[3]
        out.append(
            f"- {item}: 最低 {lowest[1]} {unit} ({lowest[0]}); "
            f"最高 {highest[1]} {unit} ({highest[0]}); n={len(nums)}"
        )
    return "\n".join(out) if out else "（無顯著異常）"


def _imaging_to_md(imgs, kind_hint: str = "") -> str:
    if not imgs:
        return "（無）"
    out = []
    for r in imgs:
        out.append(f"- {r.date} {r.modality}: {r.impression[:300]}")
    return "\n".join(out)


# ──────────────────────────────────────────────────────────────────────
# WorkerAgent
# ──────────────────────────────────────────────────────────────────────

class WorkerAgent:
    """Generates one section of the summary. Stateless across sections."""

    def __init__(self, section_name: str, llm: OllamaClient):
        self.section_name = section_name
        self.llm = llm
        self.system_prompt = P.WORKER_SYSTEM[section_name]

    def generate(self, user_prompt: str, feedback: Optional[str] = None,
                 previous_content: Optional[str] = None) -> str:
        """Initial pass or feedback-driven rewrite.

        On revision, the model gets the *previous output* and the *feedback*,
        and is told to fix only what's wrong while keeping the section scope.
        It does NOT get the original task prompt again — that tends to make
        the model regenerate from scratch and drift into other sections.
        """
        if feedback and previous_content:
            rewrite = (
                f"你正在撰寫【{self.section_name}】段落。監察者發現以下問題：\n\n"
                f"{feedback}\n\n"
                f"---\n"
                f"以下是你之前的輸出。請只修正上面點名的錯誤，保留其他正確內容不變，"
                f"重新輸出**完整且只包含【{self.section_name}】這一段**的修訂版："
                f"\n\n{previous_content}"
            )
            text = self.llm.complete(self.system_prompt, rewrite, num_predict=4096)
        else:
            text = self.llm.complete(self.system_prompt, user_prompt, num_predict=4096)
        return text.strip()


# ──────────────────────────────────────────────────────────────────────
# SubSupervisorAgent — verify a single claim against PatientRecord
# ──────────────────────────────────────────────────────────────────────

@dataclass
class Verdict:
    verdict: str  # 【正確】 / 【錯誤】 / 【無法核實】
    detail: str
    is_correct: bool


class SubSupervisorAgent:
    """Verifies one claim against the PatientRecord (passed as JSON)."""

    def __init__(self, llm: OllamaClient, record: PatientRecord):
        self.llm = llm
        self.record_json = self._compact_record_json(record)

    @staticmethod
    def _compact_record_json(record: PatientRecord) -> str:
        """Drop bulky raw text fields the verifier doesn't need."""
        d = record.to_dict()
        d.pop("treatment_course_raw", None)
        d.pop("discharge_status_raw", None)
        for e in d.get("timeline", []):
            e["raw_quote"] = (e.get("raw_quote") or "")[:120]
        return json.dumps(d, ensure_ascii=False, indent=1)

    def verify(self, claim: str) -> Verdict:
        user = (
            f"主張：\n{claim}\n\n"
            f"---\nPatientRecord (結構化病歷檔案)：\n{self.record_json}"
        )
        resp = self.llm.complete(
            system=P.SUB_SUPERVISOR_SYSTEM, user=user, num_predict=512
        )
        first_line = (resp or "").strip().splitlines()[0] if resp else ""
        is_correct = "【錯誤】" not in first_line
        return Verdict(verdict=first_line or "【無法核實】",
                       detail=(resp or "").strip()[:600],
                       is_correct=is_correct)


# ──────────────────────────────────────────────────────────────────────
# TeamSupervisorAgent — structure check + claim split + dispatch
# ──────────────────────────────────────────────────────────────────────

@dataclass
class ReviewResult:
    approved: bool
    error_report: str          # to be fed back to worker
    needs_tool_rewrite: bool   # True = structural rewrite (with full source)
    verifications: list[dict]
    structure_error: Optional[str] = None


class TeamSupervisorAgent:

    MAX_CLAIMS = 5

    def __init__(self, llm: OllamaClient, record: PatientRecord):
        self.llm = llm
        self.record = record
        self.sub = SubSupervisorAgent(llm, record)

    def review(self, section_name: str, content: str) -> ReviewResult:
        # Step 1: structure check (LLM semantic)
        struct_err = self._check_structure(section_name, content)
        if struct_err:
            return ReviewResult(
                approved=False, error_report=struct_err,
                needs_tool_rewrite=True, verifications=[],
                structure_error=struct_err,
            )
        # Step 2: split claims
        claims = self._split_claims(section_name, content)
        if not claims:
            return ReviewResult(True, "", False, [])
        # Step 3: dispatch verifiers
        verifications = []
        errors = []
        for c in claims[:self.MAX_CLAIMS]:
            v = self.sub.verify(c)
            verifications.append({
                "claim": c,
                "verdict": v.verdict,
                "detail": v.detail,
                "is_correct": v.is_correct,
            })
            if not v.is_correct:
                errors.append(f"• 主張：{c}\n  查核：{v.detail}")
        if not errors:
            return ReviewResult(True, "", False, verifications)
        report = (
            f"【監察團隊查核 — 發現 {len(errors)}/{len(claims)} 項錯誤】\n\n"
            + "\n\n".join(errors)
            + "\n\n請只修正以上錯誤，保留正確內容；不需重新查資料，依據 PatientRecord 的正確值改寫。"
        )
        return ReviewResult(False, report, False, verifications)

    def _check_structure(self, section_name: str, content: str) -> Optional[str]:
        user = (
            f"以下是【{section_name}】段落內容，請依規則判斷結構是否完整：\n\n"
            f"---\n{content[:2000]}{'…(截斷)' if len(content) > 2000 else ''}\n---"
        )
        resp = (self.llm.complete(P.STRUCTURE_CHECK_SYSTEM, user, num_predict=512)
                or "").strip()
        if not resp:
            return None
        if resp.startswith("STRUCTURE_OK"):
            return None
        if "STRUCTURE_INCOMPLETE" in resp:
            missing = [l.strip() for l in resp.splitlines()
                       if l.strip().startswith("MISSING:")]
            if missing:
                return (
                    f"【結構警告】【{section_name}】段落缺少必要元素：\n"
                    + "\n".join(missing)
                    + "\n\n請在保留現有正確內容基礎上補齊上述缺失元素。"
                )
            return f"【結構警告】【{section_name}】段落結構不完整。"
        return None

    def _split_claims(self, section_name: str, content: str) -> list[str]:
        user = (
            f"段落：【{section_name}】\n\n{content[:2000]}"
            f"\n\n請拆出最多 {self.MAX_CLAIMS} 個可核實的具體主張。"
        )
        resp = (self.llm.complete(P.SUPERVISOR_SPLIT_SYSTEM, user, num_predict=1024)
                or "")
        claims = []
        for line in resp.splitlines():
            line = line.strip()
            if line.upper().startswith("CLAIM:"):
                c = line[6:].strip()
                if c and c not in {"無", "（無）", "无"}:
                    claims.append(c)
        return claims[:self.MAX_CLAIMS]


# ──────────────────────────────────────────────────────────────────────
# Engine: orchestrates 5 sections, max 2 revisions each
# ──────────────────────────────────────────────────────────────────────

SECTIONS = ["病史時間序", "主要診斷", "入院原因", "治療過程", "出院前現況"]
MAX_REVISIONS = 2


def _ev(stage: str, message: str, **extra) -> dict:
    return {"stage": stage, "message": message, **extra}


def run_agents(record: PatientRecord, worker_llm: OllamaClient,
               supervisor_llm: OllamaClient) -> Iterator[dict]:
    """
    Drive all five workers + supervisor. Streaming generator:
      - yields {'stage','message','section', ...} progress events
      - last event: {'stage':'done', 'sections': {name: text}, 'review_log': [...]}
    """
    sections_out: dict[str, str] = {}
    review_log: list[dict] = []

    supervisor = TeamSupervisorAgent(supervisor_llm, record)

    for section_name in SECTIONS:
        yield _ev("worker_start", f"工作者撰寫【{section_name}】…",
                  section=section_name)

        user_prompt = _build_user_prompt(section_name, record, sections_out)
        worker = WorkerAgent(section_name, worker_llm)
        content = worker.generate(user_prompt)

        approved = False
        for round_num in range(MAX_REVISIONS + 1):
            yield _ev("supervisor", f"監察者審查【{section_name}】(第 {round_num + 1} 次)…",
                      section=section_name, round=round_num)
            result = supervisor.review(section_name, content)
            review_log.append({
                "section": section_name,
                "round": round_num,
                "approved": result.approved,
                "structural": result.structure_error is not None,
                "structure_error": result.structure_error,
                "verifications": result.verifications,
                "content_preview": content[:600],
            })
            if result.approved:
                approved = True
                yield _ev("supervisor_pass", f"【{section_name}】通過 ✓",
                          section=section_name, claims=len(result.verifications))
                break
            yield _ev("worker_revise",
                      f"工作者修訂【{section_name}】"
                      f"({'結構' if result.needs_tool_rewrite else '事實'}錯誤)…",
                      section=section_name, round=round_num)
            content = worker.generate(user_prompt, feedback=result.error_report,
                                       previous_content=content)

        if not approved:
            yield _ev("warning",
                      f"【{section_name}】達修訂上限，使用最終版",
                      section=section_name)

        sections_out[section_name] = content
        yield _ev("section_done", f"【{section_name}】完成", section=section_name,
                  preview=content[:200])

    yield {
        "stage": "done", "message": "全部段落完成",
        "sections": sections_out, "review_log": review_log,
    }


# ──────────────────────────────────────────────────────────────────────
# Per-section user-prompt builders
# ──────────────────────────────────────────────────────────────────────

def _build_user_prompt(section_name: str, record: PatientRecord,
                       sections_so_far: dict[str, str]) -> str:
    if section_name == "病史時間序":
        events = record.pre_admission_timeline()
        if not events:
            return P.worker_user_timeline("（無入院前事件）",
                                          record.basic.patient_name, "?", "?")
        dates = [e.date for e in events if e.date]
        earliest = min(dates) if dates else "?"
        latest = record.basic.admission_date or (max(dates) if dates else "?")
        return P.worker_user_timeline(_events_to_md(events),
                                       record.basic.patient_name,
                                       earliest, latest)

    if section_name == "主要診斷":
        return P.worker_user_diagnosis(
            _diagnoses_to_md(record.admission_diagnoses),
            _diagnoses_to_md(record.discharge_diagnoses),
        )

    if section_name == "入院原因":
        # imaging on / around admission day (±3 days)
        admit_d = record.basic.admission_date
        admission_imaging = [
            r for r in record.radiology
            if _within(r.date, admit_d, days=3)
        ]
        first_events = [
            e for e in record.in_admission_timeline()
            if _within(e.date, admit_d, days=2)
        ]
        return P.worker_user_admission(
            chief_complaint=record.chief_complaint,
            admit_date=admit_d or "?",
            adm_diagnoses_md=_diagnoses_to_md(record.admission_diagnoses),
            admission_imaging_md=_imaging_to_md(admission_imaging),
            initial_treatment_md=_events_to_md(first_events),
        )

    if section_name == "治療過程":
        # Exclude the very first 2 days (already in 入院原因)
        admit_d = record.basic.admission_date
        in_events = [
            e for e in record.in_admission_timeline()
            if not _within(e.date, admit_d, days=2)
        ]
        return P.worker_user_treatment(
            events_in_admission_md=_events_to_md(in_events),
            course_excerpt=record.treatment_course_raw[:2500],
            abnormal_labs_md=_labs_to_md(record.abnormal_labs),
        )

    if section_name == "出院前現況":
        # last 7 days of in-admission events
        in_events = record.in_admission_timeline()
        last_dates = sorted({e.date for e in in_events if e.date})
        if last_dates:
            cutoff = last_dates[-7] if len(last_dates) >= 7 else last_dates[0]
            late_events = [e for e in in_events if e.date and e.date >= cutoff]
        else:
            late_events = in_events
        return P.worker_user_discharge(
            events_last_week_md=_events_to_md(late_events),
            status_raw=record.discharge_status_raw,
        )

    raise ValueError(f"unknown section: {section_name}")


def _within(date_str: str, anchor: str, days: int) -> bool:
    """Loose date proximity check: returns True if date_str is within `days`
    of anchor (both YYYY-MM-DD). Returns False if either is unparseable."""
    if not date_str or not anchor:
        return False
    m1 = re.match(r"(\d{4})-(\d{2})-(\d{2})", date_str)
    m2 = re.match(r"(\d{4})-(\d{2})-(\d{2})", anchor)
    if not m1 or not m2:
        return False
    from datetime import date
    try:
        d1 = date(*map(int, m1.groups()))
        d2 = date(*map(int, m2.groups()))
        return abs((d1 - d2).days) <= days
    except ValueError:
        return False
