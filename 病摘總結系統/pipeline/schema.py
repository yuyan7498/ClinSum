from __future__ import annotations
from dataclasses import dataclass, field


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
