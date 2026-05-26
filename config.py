import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

OLLAMA_BASE_URL = os.environ.get("OLLAMA_URL", "http://140.116.240.181:45001")
WORKER_MODEL    = os.environ.get("WORKER_MODEL", "gpt-oss:120b")
WORKER_OPTIONS  = {"temperature": 0.2, "top_p": 0.9, "num_predict": 4096, "num_ctx": 16384}
MAX_UPLOAD_MB   = 25
ALLOWED_EXTENSIONS = {".pdf"}
