"""ClinSum configuration.

Connection layout:
    Flask (this host) ─SSH(45017)─→ container ─HTTP(45001)─→ Ollama
    The SSH tunnel is started once at boot (pipeline.tunnel.get_or_create_tunnel).

Model split (set by /loop iteration with user):
    - gemma4:31b     → archiver (Stage 2) — many small JSON-mode calls
    - gpt-oss:120b   → worker + supervisor (Stage 3) — heavier reasoning
"""
import os

# ── SSH tunnel target ────────────────────────────────────────────────
SSH_HOST = os.environ.get("CLINSUM_SSH_HOST", "140.116.240.181")
SSH_PORT = int(os.environ.get("CLINSUM_SSH_PORT", "45017"))
SSH_USER = os.environ.get("CLINSUM_SSH_USER", "root")
SSH_PASSWORD = os.environ.get("CLINSUM_SSH_PASSWORD", "medflow911114")
REMOTE_OLLAMA_HOST = os.environ.get("CLINSUM_REMOTE_HOST", "127.0.0.1")
REMOTE_OLLAMA_PORT = int(os.environ.get("CLINSUM_REMOTE_PORT", "45001"))

# ── Model assignments ──────────────────────────────────────────────
ARCHIVER_MODEL = os.environ.get("CLINSUM_ARCHIVER_MODEL", "gemma4:31b")
WORKER_MODEL   = os.environ.get("CLINSUM_WORKER_MODEL",   "gpt-oss:120b")
SUPERVISOR_MODEL = os.environ.get("CLINSUM_SUPERVISOR_MODEL", "gpt-oss:120b")

# Models surfaced in the UI for status (worker is what user thinks of as "the
# model"). Archiver is shown as a fixed dependency.
AVAILABLE_MODELS = [
    {
        "key": "gpt-oss:120b",
        "label": "GPT-OSS 120B",
        "description": "工作者 + 監察者 (撰寫與查核)",
        "role": "worker",
        "think_supported": True,
    },
    {
        "key": "gemma4:31b",
        "label": "Gemma 4 31B",
        "description": "歸檔員 (結構化抽取)",
        "role": "archiver",
        "think_supported": False,
    },
]

# ── Generation defaults ────────────────────────────────────────────
ARCHIVER_OPTIONS = {"temperature": 0.1, "top_p": 0.9, "num_predict": 2048, "num_ctx": 8192}
WORKER_OPTIONS   = {"temperature": 0.2, "top_p": 0.9, "num_predict": 4096, "num_ctx": 16384}

# ── Upload limits ─────────────────────────────────────────────────
MAX_UPLOAD_MB = 25
ALLOWED_EXTENSIONS = {".pdf"}
