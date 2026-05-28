# 病摘總結系統

上傳出院病摘 PDF，以 AI 生成繁體中文結構化摘要。兩種模式：

| 模式 | 說明 |
|---|---|
| **含完整資料** | 所有段落（含放射線/病理報告原文）送入模型 |
| **去除檢驗/影像** | 過濾放射線報告與病理報告，聚焦臨床敘述，日期混淆風險較低 |

Prompt 可在 UI 自由編輯，必須保留 `{sections_block}` 佔位符。

---

## 安裝

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

---

## 啟動

```powershell
python app.py
```

開瀏覽器：`http://127.0.0.1:5000`

---

## 連線設定

預設連線 `http://140.116.240.181:45001`（gpt-oss:120b）。若需更換，建立 `.env`：

```
OLLAMA_URL=http://your-server:45001
WORKER_MODEL=gpt-oss:120b
```

---

## 專案結構

```
病摘總結系統/
├── app.py              Flask + SSE（/api/quick、/api/quick_no_lab）
├── config.py           Ollama URL / 模型設定
├── requirements.txt
├── pipeline/
│   ├── extractor.py    PDF → 段落（pdfplumber）
│   ├── quick.py        兩種 prompt 與段落組裝邏輯
│   ├── llm.py          Ollama /api/chat 串流客戶端
│   └── schema.py       RawSections dataclass
├── templates/
│   └── index.html
└── static/
    ├── style.css
    └── script.js
```

---

## API

| 方法 | Path | 說明 |
|---|---|---|
| `GET`  | `/api/health` | Ollama 連線狀態 |
| `POST` | `/api/extract` | 解析 PDF，回傳段落資訊（不呼叫 LLM） |
| `GET`  | `/api/quick/default_prompt` | 含完整資料的預設 prompt |
| `POST` | `/api/quick` | 含完整資料，SSE 串流（`file=`, `prompt=`） |
| `GET`  | `/api/quick_no_lab/default_prompt` | 去除檢驗的預設 prompt |
| `POST` | `/api/quick_no_lab` | 去除檢驗/影像，SSE 串流（`file=`, `prompt=`） |
