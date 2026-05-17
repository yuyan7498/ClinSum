"""
Structured PatientRecord schema produced by the archiver (Stage 2) and consumed
by every downstream agent (Stage 3). All facts the workers / supervisors use
must trace back to one of these fields — they never see the raw PDF text.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


# ── Atomic facts ─────────────────────────────────────────────────────────

@dataclass
class Diagnosis:
    text: str                       # full English / Chinese diagnosis line
    is_cancer: bool = False
    is_admission: bool = True       # True = 入院診斷, False = 出院診斷
    raw_quote: str = ""             # exact source string in PDF


@dataclass
class TimelineEvent:
    """A single dated clinical event extracted from 病史 / 治療經過."""
    date: str                       # YYYY-MM-DD, YYYY-MM, or free text fallback
    category: str                   # 確診/手術/化療/放療/併發症/標靶/檢驗/其他
    event: str                      # short Chinese description
    raw_quote: str = ""             # source text snippet (≤200 chars)
    in_admission: bool = False      # True = during this admission


@dataclass
class Surgery:
    date: str
    procedure: str                  # English procedure name
    indication: str = ""
    raw_quote: str = ""


@dataclass
class LabFinding:
    date: str
    item: str                       # e.g. WBC, Hb, Cr
    value: str                      # raw value as string
    unit: str = ""
    ref_range: str = ""
    flag: str = ""                  # H / L / "" — only abnormals are kept


@dataclass
class ImagingReport:
    date: str
    modality: str                   # MRI / CT / KUB / Chest X-ray ...
    impression: str                 # 1-3 sentence summary
    raw_quote: str = ""


@dataclass
class PathologyReport:
    date: str
    site: str
    diagnosis: str
    raw_quote: str = ""


@dataclass
class BasicInfo:
    patient_name: str = ""
    chart_no: str = ""
    sex: str = ""
    birth_date: str = ""
    admission_date: str = ""
    discharge_date: str = ""
    department: str = ""


# ── Top-level record ────────────────────────────────────────────────────

@dataclass
class PatientRecord:
    basic: BasicInfo = field(default_factory=BasicInfo)
    chief_complaint: str = ""
    admission_diagnoses: list[Diagnosis] = field(default_factory=list)
    discharge_diagnoses: list[Diagnosis] = field(default_factory=list)
    timeline: list[TimelineEvent] = field(default_factory=list)
    surgeries: list[Surgery] = field(default_factory=list)
    abnormal_labs: list[LabFinding] = field(default_factory=list)
    radiology: list[ImagingReport] = field(default_factory=list)
    pathology: list[PathologyReport] = field(default_factory=list)
    treatment_course_raw: str = ""  # 住院治療經過 原文 (for worker reference)
    discharge_status_raw: str = ""  # 出院前現況 / 出院照護計畫 原文

    def to_dict(self) -> dict:
        return asdict(self)

    # ── helpers used by agents ────────────────────────────────────────

    def pre_admission_timeline(self) -> list[TimelineEvent]:
        return [e for e in self.timeline if not e.in_admission]

    def in_admission_timeline(self) -> list[TimelineEvent]:
        return [e for e in self.timeline if e.in_admission]

    def cancer_diagnoses(self) -> list[Diagnosis]:
        return [d for d in self.discharge_diagnoses if d.is_cancer]

    def lookup_event(self, keyword: str) -> list[TimelineEvent]:
        kw = keyword.lower()
        return [
            e for e in self.timeline
            if kw in e.event.lower() or kw in e.raw_quote.lower() or kw in e.date
        ]

    def lookup_lab(self, item: str) -> list[LabFinding]:
        kw = item.lower()
        return [l for l in self.abnormal_labs if kw in l.item.lower()]

    def lookup_diagnosis(self, keyword: str) -> list[Diagnosis]:
        kw = keyword.lower()
        return [
            d for d in (self.admission_diagnoses + self.discharge_diagnoses)
            if kw in d.text.lower()
        ]


# ── Stage-1 intermediate (raw sections from PDF) ─────────────────────────

@dataclass
class RawSections:
    """Output of pipeline.extractor — section name → raw text."""
    sections: dict[str, str] = field(default_factory=dict)
    full_text: str = ""
    page_count: int = 0
    extraction_method: str = "pdfplumber"

    def get(self, *names: str) -> str:
        for n in names:
            if n in self.sections:
                return self.sections[n]
        return ""
