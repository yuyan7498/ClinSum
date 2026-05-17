"""
Flask orchestrator for the three-stage ClinSum pipeline.

Endpoints:
  GET  /                — single-page UI
  GET  /api/health      — Ollama reachability + model installation status
  POST /api/extract     — preview pages/chars after Stage 1 (no LLM)
  POST /api/summarize   — full pipeline (SSE): extract → archive → agents
"""
from __future__ import annotations

import io
import json
import logging
import time
import traceback
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

import config
from pipeline import agents, archiver, extractor
from pipeline.llm import LLMConfig, OllamaClient
from pipeline.schema import PatientRecord
from pipeline.tunnel import get_or_create_tunnel

logging.basicConfig(level=logging.INFO,
                    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("clinsum")

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_MB * 1024 * 1024


# ──────────────────────────────────────────────────────────────────
# Tunnel + LLM client construction
# ──────────────────────────────────────────────────────────────────

def _ensure_tunnel():
    return get_or_create_tunnel(
        config.SSH_HOST, config.SSH_PORT,
        config.SSH_USER, config.SSH_PASSWORD,
        config.REMOTE_OLLAMA_HOST, config.REMOTE_OLLAMA_PORT,
    )


def _archiver_client() -> OllamaClient:
    tun = _ensure_tunnel()
    return OllamaClient(LLMConfig(
        host=tun.local_url, model=config.ARCHIVER_MODEL,
        temperature=config.ARCHIVER_OPTIONS["temperature"],
        top_p=config.ARCHIVER_OPTIONS["top_p"],
        num_predict=config.ARCHIVER_OPTIONS["num_predict"],
        num_ctx=config.ARCHIVER_OPTIONS["num_ctx"],
        disable_thinking=True,
    ))


def _worker_client() -> OllamaClient:
    tun = _ensure_tunnel()
    return OllamaClient(LLMConfig(
        host=tun.local_url, model=config.WORKER_MODEL,
        temperature=config.WORKER_OPTIONS["temperature"],
        top_p=config.WORKER_OPTIONS["top_p"],
        num_predict=config.WORKER_OPTIONS["num_predict"],
        num_ctx=config.WORKER_OPTIONS["num_ctx"],
        disable_thinking=False,
    ))


def _supervisor_client() -> OllamaClient:
    tun = _ensure_tunnel()
    return OllamaClient(LLMConfig(
        host=tun.local_url, model=config.SUPERVISOR_MODEL,
        temperature=0.1, top_p=0.9, num_predict=2048, num_ctx=16384,
        disable_thinking=False,
    ))


# ──────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template(
        "index.html",
        available_models=config.AVAILABLE_MODELS,
        worker_model=config.WORKER_MODEL,
        archiver_model=config.ARCHIVER_MODEL,
    )


@app.route("/api/health")
def health():
    try:
        tun = _ensure_tunnel()
        r = requests.get(f"{tun.local_url}/api/tags", timeout=8)
        r.raise_for_status()
        installed = [m["name"] for m in r.json().get("models", [])]
        return jsonify({
            "ok": True,
            "host": f"{config.SSH_HOST}:{config.REMOTE_OLLAMA_PORT} (via SSH {config.SSH_PORT})",
            "tunnel_local": tun.local_url,
            "models_on_server": installed,
            "available_models": [
                {**m, "installed": m["key"] in installed}
                for m in config.AVAILABLE_MODELS
            ],
        })
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 503


@app.route("/api/extract", methods=["POST"])
def api_extract():
    """Stage-1-only preview (synchronous, no LLM)."""
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "未提供檔案"}), 400
    if Path(file.filename).suffix.lower() not in config.ALLOWED_EXTENSIONS:
        return jsonify({"error": "僅支援 PDF 檔"}), 400
    try:
        raw = extractor.extract(io.BytesIO(file.read()))
    except extractor.PDFExtractionError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"PDF 解析失敗: {e}"}), 400
    return jsonify({
        "filename": file.filename,
        "pages": raw.page_count,
        "chars": len(raw.full_text),
        "sections": list(raw.sections.keys()),
        "basic_info": raw.sections.get("基本資料", ""),
        "preview": raw.full_text[:600],
        "method": raw.extraction_method,
    })


@app.route("/api/summarize", methods=["POST"])
def api_summarize():
    """Full pipeline as SSE stream."""
    # Accept either uploaded file (multipart) or pre-extracted text JSON
    raw_sections = None
    if request.content_type and request.content_type.startswith("multipart/"):
        file = request.files.get("file")
        if not file:
            return jsonify({"error": "未提供檔案"}), 400
        try:
            raw_sections = extractor.extract(io.BytesIO(file.read()))
        except extractor.PDFExtractionError as e:
            return jsonify({"error": str(e)}), 400
    else:
        return jsonify({"error": "請以 multipart/form-data 上傳 PDF"}), 400

    def event(name: str, data: dict) -> str:
        return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    @stream_with_context
    def generate():
        t0 = time.time()
        try:
            yield event("status", {"stage": "extract_done",
                                    "message": f"解析完成：{raw_sections.page_count} 頁、"
                                               f"{len(raw_sections.full_text)} 字、"
                                               f"{len(raw_sections.sections)} 個段落"})

            # ── Stage 2: archive ─────────────────────────────────
            yield event("status", {"stage": "archive_start",
                                    "message": "啟動 Gemma4 歸檔器…"})
            arch_llm = _archiver_client()
            record: PatientRecord | None = None
            for ev in archiver.archive(raw_sections, arch_llm):
                if ev.get("stage") == "done":
                    record = ev["record"]
                    yield event("archive_done", {
                        "message": "歸檔完成",
                        "summary": _archive_summary(record),
                    })
                else:
                    yield event("archive_progress", ev)

            if record is None:
                yield event("error", {"message": "歸檔失敗：未產生 PatientRecord"})
                return

            # ── Stage 3: workers + supervisors ───────────────────
            yield event("status", {"stage": "agents_start",
                                    "message": "啟動 GPT-OSS 工作者與監察者…"})
            worker_llm = _worker_client()
            supervisor_llm = _supervisor_client()

            sections_out: dict | None = None
            review_log = []
            for ev in agents.run_agents(record, worker_llm, supervisor_llm):
                if ev.get("stage") == "done":
                    sections_out = ev["sections"]
                    review_log = ev["review_log"]
                else:
                    yield event("agent_progress", ev)

            if not sections_out:
                yield event("error", {"message": "工作者管線未產生結果"})
                return

            # ── Final assembly ───────────────────────────────────
            final_md = _assemble(record, sections_out)
            yield event("delta", {"text": final_md})
            yield event("done", {
                "elapsed": round(time.time() - t0, 1),
                "final_text": final_md,
                "review_log": _summarize_review_log(review_log),
            })

        except Exception as e:
            log.exception("pipeline error")
            yield event("error", {"message": f"{type(e).__name__}: {e}",
                                   "trace": traceback.format_exc()[-2000:]})

    return Response(generate(), mimetype="text/event-stream")


# ──────────────────────────────────────────────────────────────────
# Final markdown assembly
# ──────────────────────────────────────────────────────────────────

import re as _re

# Strip markdown bold/italic markers from section titles (worker safety net)
_BOLD_TITLE = _re.compile(r"\*{1,2}([一二三四]、[^*\n]+)\*{1,2}")

# Section header regex used to slice out stray other-section content from a
# worker's output (e.g. a 入院原因 worker accidentally appended 三、治療過程).
_SECTION_HEADERS = {
    "主要診斷":   _re.compile(r"^\s*\*{0,2}一、\s*主要診斷", _re.MULTILINE),
    "入院原因":   _re.compile(r"^\s*\*{0,2}二、\s*入院原因", _re.MULTILINE),
    "治療過程":   _re.compile(r"^\s*\*{0,2}三、\s*治療過程", _re.MULTILINE),
    "出院前現況": _re.compile(r"^\s*\*{0,2}四、\s*出院前現況", _re.MULTILINE),
}
_OTHERS = {
    "主要診斷":   ["入院原因", "治療過程", "出院前現況"],
    "入院原因":   ["主要診斷", "治療過程", "出院前現況"],
    "治療過程":   ["主要診斷", "入院原因", "出院前現況"],
    "出院前現況": ["主要診斷", "入院原因", "治療過程"],
}


def _sanitize_section(name: str, content: str) -> str:
    """Strip markdown bold from titles + cut off any stray sibling section."""
    if not content:
        return ""
    text = _BOLD_TITLE.sub(r"\1", content)
    # If the worker accidentally appended other section headers, truncate there.
    if name in _OTHERS:
        cutoffs = []
        for other in _OTHERS[name]:
            m = _SECTION_HEADERS[other].search(text)
            if m:
                cutoffs.append(m.start())
        if cutoffs:
            text = text[: min(cutoffs)]
    return text.strip()


def _assemble(record: PatientRecord, sections: dict) -> str:
    parts = ["Summary:", ""]
    timeline = sections.get("病史時間序", "").strip()
    if timeline:
        prefix = f"根據病歷資料，{record.basic.patient_name or '本病患'}"
        if not timeline.startswith(prefix):
            parts.append(f"{prefix}入院前的主要病史與治療進程如下：")
            parts.append("")
        parts.append(_sanitize_section("病史時間序", timeline))
        parts.append("")
    transition = f"病患{record.basic.patient_name or ''}的主要診斷與治療過程詳述如下："
    parts.append(transition)
    parts.append("")
    for sect in ("主要診斷", "入院原因", "治療過程", "出院前現況"):
        content = _sanitize_section(sect, sections.get(sect, ""))
        if content:
            parts.append(content)
            parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _archive_summary(record: PatientRecord) -> dict:
    return {
        "patient": record.basic.patient_name,
        "chart_no": record.basic.chart_no,
        "admission_date": record.basic.admission_date,
        "timeline_events": len(record.timeline),
        "pre_admission_events": len(record.pre_admission_timeline()),
        "in_admission_events": len(record.in_admission_timeline()),
        "admission_diagnoses": len(record.admission_diagnoses),
        "discharge_diagnoses": len(record.discharge_diagnoses),
        "abnormal_labs": len(record.abnormal_labs),
        "radiology": len(record.radiology),
        "pathology": len(record.pathology),
    }


def _summarize_review_log(log_entries: list) -> list:
    out = []
    for entry in log_entries:
        out.append({
            "section": entry["section"],
            "round": entry["round"] + 1,
            "approved": entry["approved"],
            "structural": entry.get("structural", False),
            "structure_error": entry.get("structure_error"),
            "claim_count": len(entry.get("verifications", [])),
            "errors": [
                v for v in entry.get("verifications", [])
                if not v.get("is_correct", True)
            ],
        })
    return out


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
