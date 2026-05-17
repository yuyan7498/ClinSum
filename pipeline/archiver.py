"""
Stage 2: RawSections → PatientRecord.

Mix of rule-based parsers (deterministic fields: basic info, diagnoses,
abnormal labs, imaging/pathology block boundaries) and Gemma4 LLM calls
(free-text fields: 病史 + 住院治療經過 → TimelineEvent list).

Each LLM call processes a *single* small chunk so context never grows beyond a
few thousand tokens — this is the primary hallucination-reduction lever from
the nightly_preprocessing playbook.
"""
from __future__ import annotations

import json
import re
from typing import Iterator, Optional

from . import prompts_archive
from .llm import OllamaClient
from .schema import (
    BasicInfo, Diagnosis, ImagingReport, LabFinding, PathologyReport,
    PatientRecord, RawSections, Surgery, TimelineEvent,
)


# ── progress events ─────────────────────────────────────────────────────

def _ev(stage: str, message: str, **extra) -> dict:
    return {"stage": stage, "message": message, **extra}


# ── public entrypoint ───────────────────────────────────────────────────

def archive(raw: RawSections, llm: OllamaClient) -> Iterator[dict]:
    """
    Streaming generator. Yields progress events; last event is
    {'stage': 'done', 'record': PatientRecord}.
    """
    rec = PatientRecord()

    yield _ev("basic", "解析基本資料…")
    rec.basic = _parse_basic_info(raw)

    yield _ev("diagnosis", "解析入院 / 出院診斷…")
    rec.admission_diagnoses = _parse_diagnoses(
        raw.sections.get("入院診斷", ""), is_admission=True)
    rec.discharge_diagnoses = _parse_diagnoses(
        raw.sections.get("出院診斷", ""), is_admission=False)

    yield _ev("complaint", "擷取主訴…")
    rec.chief_complaint = raw.sections.get("主訴", "").strip()

    yield _ev("surgeries", "解析手術紀錄…")
    rec.surgeries = _parse_surgeries(raw.sections.get("手術及處置", ""))

    # ── LLM-driven timeline extraction ───────────────────────────────
    history_text = raw.sections.get("病史", "").strip()
    course_text  = raw.sections.get("住院治療經過", "").strip()

    admission_year = (rec.basic.admission_date or "")[:4]

    # Diagnoses often pack date-rich English (e.g. "seizure attack on 9/12,
    # under Levetiracetam"). Feed them through the same extractor so they
    # contribute to the timeline.
    dx_text = "\n".join(d.text for d in rec.admission_diagnoses)
    if dx_text.strip():
        yield _ev("timeline_dx",
                  f"Gemma4 從入院診斷補抽日期事件 ({len(dx_text)} 字)…")
        dx_events = _llm_extract_timeline(
            llm, dx_text, in_admission=False, admission_year=admission_year,
        )
        # Keep only dated events; the rest are duplicates of the diagnosis text
        dx_events = [e for e in dx_events if e.date]
        rec.timeline.extend(dx_events)
        yield _ev("timeline_dx",
                  f"從診斷補進 {len(dx_events)} 筆有日期事件",
                  count=len(dx_events))

    if history_text:
        yield _ev("timeline_pre",
                  f"Gemma4 抽取入院前病程事件 ({len(history_text)} 字)…")
        events_pre = _llm_extract_timeline(
            llm, history_text, in_admission=False,
            admission_year=admission_year,
        )
        rec.timeline.extend(events_pre)
        yield _ev("timeline_pre",
                  f"抽取到 {len(events_pre)} 個入院前事件", count=len(events_pre))

    if course_text:
        yield _ev("timeline_in",
                  f"Gemma4 抽取住院治療事件 ({len(course_text)} 字)…")
        events_in = _llm_extract_timeline(
            llm, course_text, in_admission=True,
            admission_year=admission_year,
        )
        rec.timeline.extend(events_in)
        yield _ev("timeline_in",
                  f"抽取到 {len(events_in)} 個住院期間事件", count=len(events_in))

    rec.treatment_course_raw = course_text

    # ── Labs / imaging / pathology ───────────────────────────────────
    yield _ev("labs", "篩選異常檢驗值…")
    rec.abnormal_labs = _parse_abnormal_labs(raw.sections.get("其他", "")
                                             + "\n" + raw.full_text)

    yield _ev("imaging", "整理影像報告…")
    rec.radiology = _parse_imaging(raw.sections.get("放射線報告", ""))

    yield _ev("pathology", "整理病理報告…")
    rec.pathology = _parse_pathology(raw.sections.get("病理報告", ""))

    yield _ev("discharge", "擷取出院前狀況…")
    rec.discharge_status_raw = "\n\n".join(
        s for s in (raw.sections.get("出院照護計畫", ""),
                    raw.sections.get("出院時情況", "")) if s.strip()
    )

    # Sort timeline by best-effort date
    rec.timeline.sort(key=lambda e: _date_sort_key(e.date))

    yield {"stage": "done", "message": "歸檔完成", "record": rec}


# ── basic info ──────────────────────────────────────────────────────────

def _parse_basic_info(raw: RawSections) -> BasicInfo:
    info = BasicInfo()
    block = raw.sections.get("基本資料", "")
    for line in block.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip()
            if k == "姓名":    info.patient_name  = v
            elif k == "病歷號": info.chart_no      = v
            elif k == "性別":  info.sex           = v
            elif k == "出生日期": info.birth_date  = _normalize_zh_date(v)
            elif k == "科別":  info.department    = v
            elif k == "入院日期": info.admission_date = _normalize_zh_date(v)
            elif k == "出院日期": info.discharge_date = _normalize_zh_date(v)
    return info


_ZH_DATE_RE = re.compile(r"(\d{4})年(\d+)月(\d+)日")


def _normalize_zh_date(s: str) -> str:
    m = _ZH_DATE_RE.search(s or "")
    if not m:
        return (s or "").strip()
    y, mo, d = m.groups()
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"


# ── diagnoses (rule-based) ──────────────────────────────────────────────

_CANCER_HINTS = (
    "rhabdomyosarcoma", "carcinoma", "sarcoma", "lymphoma", "leukemia",
    "melanoma", "tumor", "neoplasm", "metasta", "癌", "瘤",
)
_DIAG_LINE_HEAD = re.compile(r"^\s*(\d+)\s*[\.\、]\s*(.*)")


def _parse_diagnoses(block: str, is_admission: bool) -> list[Diagnosis]:
    """Parse '1. ...' numbered diagnosis lines; continuation lines join the
    previous diagnosis. Lines starting with '-' or 's/p ' are merged."""
    if not block.strip():
        return []
    items: list[list[str]] = []
    current: list[str] | None = None
    for line in block.splitlines():
        s = line.rstrip()
        m = _DIAG_LINE_HEAD.match(s)
        if m:
            current = [m.group(2).strip()]
            items.append(current)
        elif current is not None and s.strip():
            current.append(s.strip())
    out: list[Diagnosis] = []
    for parts in items:
        text = " ".join(p for p in parts if p)
        if not text:
            continue
        is_cancer = any(h in text.lower() for h in _CANCER_HINTS)
        out.append(Diagnosis(text=text, is_cancer=is_cancer,
                             is_admission=is_admission, raw_quote=text))
    return out


# ── surgeries ───────────────────────────────────────────────────────────

def _parse_surgeries(block: str) -> list[Surgery]:
    if not block.strip() or block.strip().lower() in {"nil", "none", "無"}:
        return []
    out: list[Surgery] = []
    for line in block.splitlines():
        s = line.strip()
        if not s or s.lower() in {"nil", "none"}:
            continue
        date = _grep_date(s) or ""
        out.append(Surgery(date=date, procedure=s, raw_quote=s))
    return out


# ── timeline (LLM) ──────────────────────────────────────────────────────

_MAX_CHUNK_CHARS = 1800  # keep each LLM call small


def _llm_extract_timeline(llm: OllamaClient, text: str,
                          in_admission: bool,
                          admission_year: str = "") -> list[TimelineEvent]:
    """Strip lab-table noise, split, send each chunk to gemma4 → events."""
    text = _strip_lab_tables(text)
    chunks = _chunk_text(text, _MAX_CHUNK_CHARS)
    all_events: list[TimelineEvent] = []
    for chunk in chunks:
        try:
            data = llm.complete_json(
                system=prompts_archive.TIMELINE_SYSTEM,
                user=prompts_archive.timeline_user(chunk, in_admission,
                                                    admission_year),
                num_predict=2048,
            )
        except Exception as e:
            print(f"[archiver] timeline chunk failed: {e}")
            continue
        events = _parse_event_list(data, fallback_quote=chunk,
                                   in_admission=in_admission)
        all_events.extend(events)
    return all_events


# Lines that are pure lab tables — strip before LLM sees them.
# These get caught by the separate abnormal_labs parser instead.
_NUMERIC_DATA_RE  = re.compile(r"^\s*\d{8}(\s+[\d.<>+\-]+){3,}\s*$")
_LAB_HEADER_RE    = re.compile(r"^(BLOOD|URINE|BIOCHEM|CSF|STOOL)\s*:\s+", re.IGNORECASE)
# Special lab record blocks: "*** 2026-03-25 K," and the value line that follows
_SPECIAL_LAB_HEAD = re.compile(r"^\*{2,}\s*\d{4}-\d{2}-\d{2}\s+.+?,?\s*$")
# Lab value line: ItemName  <value>  <unit>  <ref-range>
_LAB_VALUE_LINE   = re.compile(
    r"^[A-Za-z][\w\-+]*\s+(?:<|>)?\d+\.?\d*\s+\S+\s+.+$"
)


def _strip_lab_tables(text: str) -> str:
    """Drop lab-only lines so the LLM doesn't extract them as fake events."""
    keep: list[str] = []
    in_table = False
    skip_next = 0  # how many follow-up lines to drop after a *** header
    for line in text.splitlines():
        stripped = line.strip()
        if skip_next > 0 and stripped:
            skip_next -= 1
            continue
        # *** YYYY-MM-DD Item line → also drop the next 1-6 value lines
        if _SPECIAL_LAB_HEAD.match(stripped):
            skip_next = 6
            continue
        if _LAB_VALUE_LINE.match(stripped):
            continue
        if _NUMERIC_DATA_RE.match(stripped) or _LAB_HEADER_RE.match(stripped):
            in_table = True
            continue
        if not stripped:
            in_table = False
            skip_next = 0
            keep.append(line)
            continue
        if in_table and not re.search(r"[A-Za-z一-鿿]{4,}", stripped):
            continue
        in_table = False
        keep.append(line)
    return "\n".join(keep)


def _chunk_text(text: str, max_chars: int) -> list[str]:
    """Greedy paragraph chunker."""
    paragraphs = re.split(r"\n\s*\n", text)
    chunks, cur = [], ""
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        if len(cur) + len(p) + 2 <= max_chars:
            cur = (cur + "\n\n" + p) if cur else p
        else:
            if cur:
                chunks.append(cur)
            if len(p) <= max_chars:
                cur = p
            else:
                # very long paragraph — hard-split
                for i in range(0, len(p), max_chars):
                    chunks.append(p[i:i + max_chars])
                cur = ""
    if cur:
        chunks.append(cur)
    return chunks


_VALID_CATEGORIES = {"確診", "手術", "化療", "放療", "標靶", "併發症", "檢驗", "影像", "其他"}


def _parse_event_list(data: object, fallback_quote: str,
                      in_admission: bool) -> list[TimelineEvent]:
    if not isinstance(data, list):
        # Sometimes models wrap in {"events": [...]}
        if isinstance(data, dict) and "events" in data:
            data = data["events"]
        else:
            return []
    out: list[TimelineEvent] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        date = str(item.get("date", "")).strip()
        category = str(item.get("category", "其他")).strip()
        if category not in _VALID_CATEGORIES:
            category = "其他"
        event = str(item.get("event", "")).strip()
        raw_quote = str(item.get("raw_quote", "")).strip()
        if not event:
            continue
        if not raw_quote:
            raw_quote = fallback_quote[:200]
        out.append(TimelineEvent(
            date=date, category=category, event=event,
            raw_quote=raw_quote, in_admission=in_admission,
        ))
    return out


def _date_sort_key(date: str) -> tuple:
    """Sort keys: (year, month, day) with fallbacks for partial dates."""
    s = (date or "").strip()
    # Try full YYYY-MM-DD or YYYY/MM/DD
    m = re.match(r"(\d{4})[/\-年](\d{1,2})[/\-月](\d{1,2})", s)
    if m:
        y, mo, d = m.groups()
        return (int(y), int(mo), int(d))
    m = re.match(r"(\d{4})[/\-年](\d{1,2})", s)
    if m:
        y, mo = m.groups()
        return (int(y), int(mo), 0)
    m = re.match(r"(\d{4})", s)
    if m:
        return (int(m.group(1)), 0, 0)
    return (9999, 0, 0)


# ── abnormal labs (rule-based) ──────────────────────────────────────────

# `*** 2026-03-25 K,`  followed by  `K  3.0 mmol/L  M: 3.5 - 4.5 ; F: 3.4 - 4.4`
_LAB_HEAD_RE = re.compile(r"^\*{2,}\s*(\d{4}-\d{2}-\d{2})\s+(.+?),?\s*$")
_LAB_VALUE_RE = re.compile(
    r"^([A-Za-z][\w\-+]*)\s+([\d.]+|<[\d.]+|>[\d.]+)\s+(\S+)\s+(.+)$"
)


def _parse_abnormal_labs(text: str) -> list[LabFinding]:
    out: list[LabFinding] = []
    current_date = ""
    for line in text.splitlines():
        line = line.rstrip()
        m_head = _LAB_HEAD_RE.match(line)
        if m_head:
            current_date = m_head.group(1)
            continue
        m_val = _LAB_VALUE_RE.match(line.strip())
        if m_val and current_date:
            item, value, unit, rng = m_val.groups()
            flag = _flag_lab(value, rng)
            if flag:
                out.append(LabFinding(
                    date=current_date, item=item, value=value,
                    unit=unit, ref_range=rng.strip(), flag=flag,
                ))
    # Deduplicate identical (date, item, value)
    seen: set = set()
    deduped: list[LabFinding] = []
    for l in out:
        key = (l.date, l.item, l.value)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(l)
    return deduped


def _flag_lab(value: str, rng: str) -> str:
    """Return 'H', 'L', or '' based on value vs reference range string."""
    try:
        v = float(value.lstrip("<>"))
    except ValueError:
        return ""
    # Range patterns: "3.5 - 4.5", "M: 3.5 - 4.5 ; F: 3.4 - 4.4", "60- 95"
    nums = [float(x) for x in re.findall(r"\d+\.?\d*", rng)]
    if len(nums) < 2:
        return ""
    lo, hi = min(nums[:2]), max(nums[:2])
    if v < lo:
        return "L"
    if v > hi:
        return "H"
    return ""


# ── imaging / pathology (rule-based with summary keep-as-is) ───────────

_IMG_HEAD = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+(.+)$")


def _parse_imaging(block: str) -> list[ImagingReport]:
    if not block.strip():
        return []
    items: list[ImagingReport] = []
    cur_date, cur_modality, cur_body = "", "", []
    for line in block.splitlines() + [""]:
        m = _IMG_HEAD.match(line.strip())
        if m and not line.lstrip().startswith("FINDINGS"):
            if cur_modality and cur_body:
                items.append(_build_imaging(cur_date, cur_modality, cur_body))
            cur_date, cur_modality, cur_body = m.group(1), m.group(2), []
        else:
            cur_body.append(line)
    if cur_modality and cur_body:
        items.append(_build_imaging(cur_date, cur_modality, cur_body))
    return items


def _build_imaging(date: str, modality: str, body: list[str]) -> ImagingReport:
    body_text = "\n".join(body).strip()
    # Prefer IMPRESSION/SUGGESTION block if present, else first ~3 lines
    impression = ""
    m = re.search(r"(IMPRESSION|SUGGESTION|FINDINGS)\s*[:：]?\s*(.+?)(?=\n[A-Z]{4,}|$)",
                  body_text, re.DOTALL | re.IGNORECASE)
    if m:
        impression = m.group(2).strip()
    else:
        lines = [l for l in body_text.splitlines() if l.strip()]
        impression = " ".join(lines[:4])
    return ImagingReport(date=date, modality=modality.strip(),
                         impression=impression[:600], raw_quote=body_text[:800])


_PATH_HEAD = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+(Surgical pathology|Pathology|病理)",
                         re.IGNORECASE)


def _parse_pathology(block: str) -> list[PathologyReport]:
    if not block.strip():
        return []
    items: list[PathologyReport] = []
    cur_date, cur_body = "", []
    for line in block.splitlines() + [""]:
        m = _PATH_HEAD.match(line.strip())
        if m:
            if cur_body:
                items.append(_build_path(cur_date, cur_body))
            cur_date = m.group(1)
            cur_body = []
        else:
            cur_body.append(line)
    if cur_body:
        items.append(_build_path(cur_date, cur_body))
    return items


def _build_path(date: str, body: list[str]) -> PathologyReport:
    body_text = "\n".join(body).strip()
    site = ""
    m_site = re.search(r"Spine,?\s*([\w\d/]+)|forearm|axilla|liver|lung|brain",
                       body_text, re.IGNORECASE)
    if m_site:
        site = m_site.group(0)
    diag = ""
    m_diag = re.search(r"Pathological diagnosis\s*[:：]?\s*(.+?)(?:\nGross|$)",
                       body_text, re.DOTALL | re.IGNORECASE)
    if m_diag:
        diag = " ".join(m_diag.group(1).split())[:300]
    return PathologyReport(date=date, site=site, diagnosis=diag,
                           raw_quote=body_text[:800])


# ── tiny helpers ────────────────────────────────────────────────────────

_GREP_DATE = re.compile(r"(\d{4}[/\-]\d{1,2}[/\-]\d{1,2}|\d{4}年\d+月\d+日)")


def _grep_date(s: str) -> Optional[str]:
    m = _GREP_DATE.search(s)
    return m.group(1) if m else None
