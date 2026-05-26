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
from pipeline import extractor
from pipeline.llm import LLMConfig, OllamaClient
from pipeline.quick import (
    DEFAULT_QUICK_NO_LAB_PROMPT,
    DEFAULT_QUICK_PROMPT,
    build_sections_block,
    build_sections_block_no_lab,
    stream_quick_summary,
)

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
log = logging.getLogger("app")

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_MB * 1024 * 1024


def _worker_client() -> OllamaClient:
    return OllamaClient(LLMConfig(
        host=config.OLLAMA_BASE_URL,
        model=config.WORKER_MODEL,
        temperature=config.WORKER_OPTIONS["temperature"],
        top_p=config.WORKER_OPTIONS["top_p"],
        num_predict=config.WORKER_OPTIONS["num_predict"],
        num_ctx=config.WORKER_OPTIONS["num_ctx"],
        disable_thinking=False,
    ))


@app.route("/")
def index():
    return render_template("index.html", worker_model=config.WORKER_MODEL)


@app.route("/api/health")
def health():
    try:
        r = requests.get(f"{config.OLLAMA_BASE_URL}/api/tags", timeout=8)
        r.raise_for_status()
        installed = [m["name"] for m in r.json().get("models", [])]
        return jsonify({"ok": True, "host": config.OLLAMA_BASE_URL, "models": installed})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.route("/api/extract", methods=["POST"])
def api_extract():
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
    })


@app.route("/api/quick/default_prompt")
def api_quick_default_prompt():
    return jsonify({"prompt": DEFAULT_QUICK_PROMPT})


@app.route("/api/quick_no_lab/default_prompt")
def api_quick_no_lab_default_prompt():
    return jsonify({"prompt": DEFAULT_QUICK_NO_LAB_PROMPT})


def _quick_sse(file, prompt_template: str, sections_builder) -> Response:
    def event(name: str, data: dict) -> str:
        return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    try:
        raw = extractor.extract(io.BytesIO(file.read()))
    except extractor.PDFExtractionError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"PDF 解析失敗: {e}"}), 400

    @stream_with_context
    def generate():
        t0 = time.time()
        try:
            yield event("status", {
                "stage": "extract_done",
                "message": f"解析完成：{raw.page_count} 頁、{len(raw.full_text)} 字、{len(raw.sections)} 個段落",
            })
            final_prompt = prompt_template.replace("{sections_block}", sections_builder(raw.sections))
            yield event("status", {"stage": "llm_start", "message": f"傳送給 {config.WORKER_MODEL}…"})
            worker = _worker_client()
            collected = ""
            for ev in stream_quick_summary(worker, final_prompt):
                if "delta" in ev:
                    collected += ev["delta"]
                    yield event("delta", {"text": ev["delta"]})
                if ev.get("done"):
                    yield event("done", {
                        "elapsed": round(time.time() - t0, 1),
                        "final_text": collected,
                    })
                    return
        except Exception as e:
            log.exception("SSE error")
            yield event("error", {
                "message": f"{type(e).__name__}: {e}",
                "trace": traceback.format_exc()[-2000:],
            })

    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/quick", methods=["POST"])
def api_quick():
    if not (request.content_type or "").startswith("multipart/"):
        return jsonify({"error": "請以 multipart/form-data 上傳 PDF"}), 400
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "未提供檔案"}), 400
    if Path(file.filename).suffix.lower() not in config.ALLOWED_EXTENSIONS:
        return jsonify({"error": "僅支援 PDF 檔"}), 400
    prompt_template = request.form.get("prompt") or DEFAULT_QUICK_PROMPT
    if "{sections_block}" not in prompt_template:
        return jsonify({"error": "Prompt 必須包含 {sections_block} 佔位符"}), 400
    return _quick_sse(file, prompt_template, build_sections_block)


@app.route("/api/quick_no_lab", methods=["POST"])
def api_quick_no_lab():
    if not (request.content_type or "").startswith("multipart/"):
        return jsonify({"error": "請以 multipart/form-data 上傳 PDF"}), 400
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "未提供檔案"}), 400
    if Path(file.filename).suffix.lower() not in config.ALLOWED_EXTENSIONS:
        return jsonify({"error": "僅支援 PDF 檔"}), 400
    prompt_template = request.form.get("prompt") or DEFAULT_QUICK_NO_LAB_PROMPT
    if "{sections_block}" not in prompt_template:
        return jsonify({"error": "Prompt 必須包含 {sections_block} 佔位符"}), 400
    return _quick_sse(file, prompt_template, build_sections_block_no_lab)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
