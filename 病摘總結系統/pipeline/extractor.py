"""
Stage 1: PDF → RawSections.

Strategy:
  1. pdfplumber extracts page text.
  2. Strip per-page headers/footers (repeated patient banner + page number).
  3. Split body by numbered section markers like "(9) 診斷", "(16) 住院治療經過".
  4. Basic-info section (top of page 1) is parsed separately into a {姓名, 病歷號, ...} dict.

Vision fallback: if total extracted text < 200 chars across all pages, the PDF
is likely scanned. Caller can switch to a vision-based path; for now this just
raises so the UI can surface the situation.
"""
from __future__ import annotations

import io
import re
from typing import BinaryIO

import pdfplumber

from .schema import RawSections


# Headers/footers to strip (per page)
_HEADER_LINES = (
    re.compile(r"^.{0,12}住院中病歷摘要\s*$"),
    re.compile(r"^姓名[:：]"),
    re.compile(r"^生日[:：].*病歷號"),
    re.compile(r"^身份[:：].*高雄榮民總醫院"),
    re.compile(r"^身份[:：].*醫院\s*$"),
    re.compile(r"^.{0,15}此份病歷.*醫院\s*$"),
)
_FOOTER_LINES = (
    re.compile(r"^\d+年\d+月\d+日修訂.*第\s*\d+\s*頁\s*/\s*共\s*\d+\s*頁\s*$"),
    re.compile(r"^第\s*\d+\s*頁\s*/\s*共\s*\d+\s*頁\s*$"),
    re.compile(r"^-{20,}\s*$"),
)

# Section marker:  (12) 手術及處置   or   (16) 住院治療經過
_SECTION_RE = re.compile(r"^\((\d+)\)\s*(.+?)\s*$")

# Map numeric section id → canonical name (the rest are captured but unmapped)
_CANONICAL = {
    "9":  "診斷",
    "12": "手術及處置",
    "13": "主訴",
    "14": "病史",
    "15": "身體檢查",
    "16": "住院治療經過",
    "17": "放射線報告",
    "18": "病理報告",
    "19": "其他",
    "20": "出院照護計畫",
    "21": "出院時情況",
}


class PDFExtractionError(Exception):
    pass


def extract(stream: BinaryIO) -> RawSections:
    """Extract structured sections from a discharge-note PDF stream."""
    with pdfplumber.open(stream) as pdf:
        pages = [(p.extract_text() or "") for p in pdf.pages]
        page_count = len(pages)

    full = "\n".join(pages)
    if len(full.strip()) < 200:
        raise PDFExtractionError(
            "pdfplumber 抽取到的文字過少 (<200 字)，可能為掃描檔。"
            "請改用視覺模式或檢查原檔。"
        )

    cleaned_lines: list[str] = []
    for page_text in pages:
        for line in page_text.splitlines():
            line = line.rstrip()
            if not line.strip():
                if cleaned_lines and cleaned_lines[-1] != "":
                    cleaned_lines.append("")
                continue
            if _is_header_or_footer(line):
                continue
            cleaned_lines.append(line)

    body = "\n".join(cleaned_lines).strip()

    sections = _split_sections(body)

    # Inject basic info parsed from the very top of the document
    sections["基本資料"] = _extract_basic_info_block(body)

    return RawSections(
        sections=sections,
        full_text=body,
        page_count=page_count,
        extraction_method="pdfplumber",
    )


# ── helpers ──────────────────────────────────────────────────────────────

def _is_header_or_footer(line: str) -> bool:
    s = line.strip()
    for pat in _HEADER_LINES:
        if pat.match(s):
            return True
    for pat in _FOOTER_LINES:
        if pat.match(s):
            return True
    return False


def _split_sections(body: str) -> dict[str, str]:
    """Walk the body line-by-line, chunking at (N) markers."""
    sections: dict[str, str] = {}
    current_name: str | None = None
    buf: list[str] = []

    def flush():
        if current_name and buf:
            text = "\n".join(buf).strip()
            if text:
                # Append rather than overwrite so 2 入院/出院 sub-blocks share key
                if current_name in sections:
                    sections[current_name] = sections[current_name] + "\n\n" + text
                else:
                    sections[current_name] = text

    for line in body.splitlines():
        m = _SECTION_RE.match(line.strip())
        if m:
            num, raw_title = m.group(1), m.group(2)
            # IDs 1-8 are header/basic-info fields, not real sections — skip
            if num.isdigit() and int(num) <= 8:
                buf.append(line)
                continue
            flush()
            current_name = _CANONICAL.get(num, raw_title)
            buf = []
            continue
        buf.append(line)
    flush()

    # Split (9) 診斷 → admission_diagnoses / discharge_diagnoses subblocks
    if "診斷" in sections:
        adm, dis = _split_diagnosis_block(sections["診斷"])
        sections["入院診斷"] = adm
        sections["出院診斷"] = dis

    return sections


_ADMISSION_HEAD = re.compile(r"^\s*\d+\s*[\.\、]\s*入院診斷")
_DISCHARGE_HEAD = re.compile(r"^\s*\d+\s*[\.\、]\s*出院診斷")


def _split_diagnosis_block(text: str) -> tuple[str, str]:
    """Inside (9) 診斷, the format is:
       1.入院診斷
         1. ...
         2. ...
       ----------
       2.出院診斷
         1. ...
    Returns (admission_text, discharge_text); either may be empty.
    """
    adm, dis = [], []
    bucket: list[str] | None = None
    for line in text.splitlines():
        if _ADMISSION_HEAD.match(line):
            bucket = adm
            continue
        if _DISCHARGE_HEAD.match(line):
            bucket = dis
            continue
        if bucket is not None:
            bucket.append(line)
    return "\n".join(adm).strip(), "\n".join(dis).strip()


# ── Basic info parser ───────────────────────────────────────────────────

_BASIC_FIELDS = {
    "姓名":  re.compile(r"\(1\)\s*姓名[:：]\s*(\S+)"),
    "病歷號": re.compile(r"\(4\)\s*病歷號[:：]\s*(\S+)"),
    "性別":  re.compile(r"性別[:：]\s*([男女])"),
    "出生日期": re.compile(r"\(3\)\s*出生日期[:：]\s*(\d{4}年\d+月\d+日)"),
    "科別":  re.compile(r"科別[:：]\s*(\S+)"),
    "入院日期": re.compile(r"\(6\)\s*入院[:：]\s*(\d{4}年\d+月\d+日)"),
    "出院日期": re.compile(r"\(8\)\s*出院[:：]\s*(\d{4}年\d+月\d+日)?"),
}


def _extract_basic_info_block(body: str) -> str:
    """Return a compact 'key: value' block for downstream archiver / display."""
    head = body[:1200]
    parts: list[str] = []
    for label, pat in _BASIC_FIELDS.items():
        m = pat.search(head)
        if m and m.group(1):
            parts.append(f"{label}: {m.group(1)}")
    return "\n".join(parts)


# ── convenience for non-Flask callers (e.g. CLI test) ───────────────────

def extract_from_path(path: str) -> RawSections:
    with open(path, "rb") as f:
        return extract(io.BytesIO(f.read()))
