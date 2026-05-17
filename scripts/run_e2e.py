"""
Full end-to-end pipeline run against a sample discharge note.

Prints progress to stdout and writes:
  data/sample_patient_record.json    — Stage 2 PatientRecord
  data/sample_final_summary.md        — Final assembled Markdown
  data/sample_review_log.json         — Per-section supervisor review trace
"""
import io
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                errors="replace", line_buffering=True)

import config
from pipeline import agents
from pipeline.archiver import archive
from pipeline.extractor import extract_from_path
from pipeline.llm import LLMConfig, OllamaClient
from pipeline.tunnel import get_or_create_tunnel


def banner(s: str):
    print(f"\n{'='*70}\n  {s}\n{'='*70}")


def main():
    pdf_path = ROOT / "data" / "23325106_Discharge Note.pdf"
    out_dir = ROOT / "data"

    banner("Connecting via SSH tunnel")
    tun = get_or_create_tunnel(
        config.SSH_HOST, config.SSH_PORT,
        config.SSH_USER, config.SSH_PASSWORD,
        config.REMOTE_OLLAMA_HOST, config.REMOTE_OLLAMA_PORT,
    )
    print(f"local tunnel → {tun.local_url}")

    # ── Stage 1 ────────────────────────────────────────────────
    banner("Stage 1 — PDF extract")
    t0 = time.time()
    raw = extract_from_path(str(pdf_path))
    print(f"{time.time()-t0:.1f}s · {raw.page_count} pages · "
          f"{len(raw.full_text)} chars · {len(raw.sections)} sections")

    # ── Stage 2 ────────────────────────────────────────────────
    banner("Stage 2 — archive (gemma4:31b)")
    arch_llm = OllamaClient(LLMConfig(
        host=tun.local_url, model=config.ARCHIVER_MODEL,
        temperature=0.1, num_predict=2048, num_ctx=8192,
        disable_thinking=True,
    ))
    t0 = time.time()
    record = None
    for ev in archive(raw, arch_llm):
        s = ev.get("stage", "")
        m = ev.get("message", "")
        print(f"  [{s:<14}] {m}")
        if ev.get("stage") == "done":
            record = ev["record"]
    print(f"Stage 2 total: {time.time()-t0:.1f}s")
    if record is None:
        print("ARCHIVER FAILED — abort")
        return

    (out_dir / "sample_patient_record.json").write_text(
        json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    banner("PatientRecord summary")
    print(f"病患: {record.basic.patient_name} ({record.basic.chart_no})")
    print(f"入院: {record.basic.admission_date}")
    print(f"timeline: {len(record.timeline)} 事件 "
          f"({len(record.pre_admission_timeline())} 入院前 / "
          f"{len(record.in_admission_timeline())} 住院中)")
    print(f"診斷: 入院 {len(record.admission_diagnoses)} / 出院 "
          f"{len(record.discharge_diagnoses)}")
    print(f"異常檢驗: {len(record.abnormal_labs)}, "
          f"影像: {len(record.radiology)}, 病理: {len(record.pathology)}")

    # ── Stage 3 ────────────────────────────────────────────────
    banner("Stage 3 — workers + supervisors (gpt-oss:120b)")
    worker_llm = OllamaClient(LLMConfig(
        host=tun.local_url, model=config.WORKER_MODEL,
        temperature=0.2, num_predict=4096, num_ctx=16384,
        disable_thinking=False,
    ))
    sup_llm = OllamaClient(LLMConfig(
        host=tun.local_url, model=config.SUPERVISOR_MODEL,
        temperature=0.1, num_predict=2048, num_ctx=16384,
        disable_thinking=False,
    ))

    t0 = time.time()
    sections = None
    review_log = []
    for ev in agents.run_agents(record, worker_llm, sup_llm):
        stage = ev.get("stage", "")
        msg = ev.get("message", "")
        elapsed = f"{time.time()-t0:5.0f}s"
        print(f"  [{elapsed}] [{stage:<18}] {msg}")
        if ev.get("stage") == "done":
            sections = ev["sections"]
            review_log = ev["review_log"]
    print(f"Stage 3 total: {time.time()-t0:.1f}s")

    if not sections:
        print("AGENTS FAILED — abort")
        return

    # ── Assemble final ───────────────────────────────────────
    from app import _assemble
    final_md = _assemble(record, sections)
    out_md = out_dir / "sample_final_summary.md"
    out_md.write_text(final_md, encoding="utf-8")

    banner("Final summary")
    print(final_md)

    out_review = out_dir / "sample_review_log.json"
    out_review.write_text(
        json.dumps(review_log, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    banner("Files written")
    print(f"  {out_md.relative_to(ROOT)}")
    print(f"  {out_review.relative_to(ROOT)}")
    print(f"  data/sample_patient_record.json")


if __name__ == "__main__":
    main()
