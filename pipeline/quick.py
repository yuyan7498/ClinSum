"""
Quick-mode summariser — one LLM call against a user-editable prompt.

Two variants:
  - quick (full):    all extracted sections including lab and imaging reports
  - quick (no-lab):  same but 放射線報告 / 病理報告 and any section whose
                     name contains "報告" or "檢驗" are excluded, letting the
                     model focus on the clinical narrative
"""
from __future__ import annotations

from typing import Iterator

from .llm import OllamaClient


DEFAULT_QUICK_PROMPT = """\
以下是一份住院病歷摘要，請根據所有提供的資料，完整回答下列三項，以繁體中文條理分明地呈現。

{sections_block}

---

請回答：

(1) 摘要病史與治療過程（從 present illness、past history 開始），依照時間序整理重要事件。

(2) 此次住院的主要診斷、入院原因、治療過程與出院狀況。

(3) 此次住院重要的檢驗異常結果（若病歷中有提及）。
"""

DEFAULT_QUICK_NO_LAB_PROMPT = """\
以下是一份住院病歷摘要（已去除原始檢驗數值與影像/病理報告），請根據所有提供的臨床資料，\
完整回答下列三項，以繁體中文條理分明地呈現。

{sections_block}

---

請回答：

(1) 摘要病史與治療過程（從 present illness、past history 開始），依照時間序整理重要事件。

(2) 此次住院的主要診斷、入院原因、治療過程與出院狀況。

(3) 住院期間的主要治療措施、手術或處置（依病歷記載說明，無需補充數值）。
"""

# Order in which we surface sections to the model. Anything not on this
# list is appended afterwards so the prompt never silently drops data.
_PREFERRED_ORDER = [
    "基本資料", "主訴", "病史", "身體檢查",
    "入院診斷", "出院診斷", "住院治療經過",
    "出院時情況", "出院照護計畫",
]

# Section keys that represent raw lab / imaging data, excluded in no-lab mode
_NO_LAB_EXCLUDE = {"放射線報告", "病理報告"}


def _is_lab_section(key: str) -> bool:
    """Return True for sections that contain raw report data."""
    if key in _NO_LAB_EXCLUDE:
        return True
    return "報告" in key or "檢驗" in key


def build_sections_block(sections: dict[str, str]) -> str:
    """Render the section dict into the `{sections_block}` placeholder body."""
    return _build(sections, exclude_lab=False)


def build_sections_block_no_lab(sections: dict[str, str]) -> str:
    """Same as build_sections_block but omits lab/imaging report sections."""
    return _build(sections, exclude_lab=True)


def _build(sections: dict[str, str], *, exclude_lab: bool) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for key in _PREFERRED_ORDER:
        if exclude_lab and _is_lab_section(key):
            continue
        val = sections.get(key, "").strip()
        if val:
            parts.append(f"=== {key} ===\n{val}")
            seen.add(key)
    for key, val in sections.items():
        if key in seen:
            continue
        if exclude_lab and _is_lab_section(key):
            continue
        v = (val or "").strip()
        if v:
            parts.append(f"=== {key} ===\n{v}")
    return "\n\n".join(parts)


def stream_quick_summary(worker: OllamaClient, prompt: str) -> Iterator[dict]:
    """Stream a single-turn worker chat completion.

    Yields the same shape as `OllamaClient.stream_complete`:
      {"delta": "..."}            for each new chunk
      {"done": True, "final_text": "..."}   when the model is finished
    """
    yield from worker.stream_complete(system="", user=prompt)
