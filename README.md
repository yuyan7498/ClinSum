# ClinSum · 癌症病歷多代理人摘要

把出院病摘 (Discharge Note) PDF 丟進來，輸出符合「Prompt.docx 情境一」規格的繁體中文結構化摘要。


---

## 架構

```
PDF
 └─[Stage 1] extractor      pdfplumber + 段落正規表達式      ~0.4s
                                                   ↓
       RawSections {基本資料, 主訴, 入院/出院診斷, 病史, 治療經過, 影像, 病理, …}
                                                   ↓
 └─[Stage 2] archiver       Gemma4 31B (small, fast)          ~70s
       ├─ 規則：基本資料、入院/出院診斷、手術、異常檢驗 (數值規則)、影像、病理
       └─ LLM：timeline 抽取 (3 source：入院診斷補日期 + 病史 + 住院治療經過)
                                                   ↓
       PatientRecord {basic, timeline[], surgeries[], abnormal_labs[], radiology[], pathology[]}
                                                   ↓
 └─[Stage 3] agents         GPT-OSS 120B (big, reasoning)     ~5min
       ├─ Worker × 5 段落 (病史時間序 / 主要診斷 / 入院原因 / 治療過程 / 出院前現況)
       │     工作者只看 PatientRecord 對應 slice，看不到原 PDF
       ├─ TeamSupervisor: 結構檢查 (Call 1) + 主張拆分 (Call 2)
       └─ SubSupervisor × N: 用 PatientRecord 當 ground truth 查核每個主張
                                                   ↓
 └─最終組裝 → Markdown
       Summary: + 入院前 timeline (bullets) + 一、二、三、四 編號段落
```

### 模型分工
| 角色 | 模型 | 為什麼 |
|---|---|---|
| 歸檔員 | gemma4:31b | 大量小任務、JSON-mode、要快 (`think: false`) |
| 工作者 | gpt-oss:120b | 醫療長文需要推理 |
| 監察者 | gpt-oss:120b | 結構檢查 + 主張比對 |

---

## 安裝

```powershell
pip install -r requirements.txt
```

`requirements.txt`:
```
Flask>=3.0
pdfplumber>=0.11
requests>=2.31
paramiko>=3.4
```

---

## 連線設定

Ollama 跑在遠端 multi-tenant 容器，外部 45001 沒對外開放，需用 SSH tunnel 從本機 forward 過去。

`config.py` 的預設值 (可用環境變數覆寫)：
```
CLINSUM_SSH_HOST       140.116.240.181
CLINSUM_SSH_PORT       45017
CLINSUM_SSH_USER       root
CLINSUM_SSH_PASSWORD   medflow911114
CLINSUM_REMOTE_HOST    127.0.0.1
CLINSUM_REMOTE_PORT    45001
CLINSUM_WORKER_MODEL   gpt-oss:120b
CLINSUM_SUPERVISOR_MODEL gpt-oss:120b
CLINSUM_ARCHIVER_MODEL gemma4:31b
```

Tunnel 在 Flask 啟動時自動建立 (`pipeline.tunnel.get_or_create_tunnel`)。本機隨機 port → 容器內 127.0.0.1:45001。

---

## 三種測試方式

### 方式 1 · 網頁 UI (推薦給 demo)

```powershell
python app.py
```

開瀏覽器 → http://127.0.0.1:5000

1. **上傳病歷** — 拖 PDF 進左側 dropzone (或點瀏覽)，會自動跑 Stage 1 顯示頁數 / 字數 / 段落 / 基本資料
2. **產生摘要** — 點「啟動三階段管線」按鈕。右側即時串流：
   - 階段 1：PDF 解析 (~0.4s)
   - 階段 2：病歷歸檔 + 階段事件計數
   - 階段 3：5 段工作者撰寫 + 監察者審查狀態 (active / done / 修訂中)
3. **檢查結果** — 摘要 markdown 即時 render；下方「監察者審查日誌」展開可看每段被拆出多少主張、查核結果
4. **匯出** — 複製 Markdown 或下載 `.md`

範例檔：`data/23325106_Discharge Note.pdf`

### 方式 2 · CLI 腳本 (適合批次 / debugging)

```powershell
python scripts/run_e2e.py
```

跑整套管線並寫三份檔案到 `data/`：
- `sample_patient_record.json` — Stage 2 結構化記錄 (38KB，可人工檢驗)
- `sample_final_summary.md` — 最終摘要 (與 23325106_Summary.pdf 並列比對)
- `sample_review_log.json` — 監察者審查紀錄 (每段主張、查核裁定)

預設讀 `data/23325106_Discharge Note.pdf`；要換檔改第 27 行的 `pdf_path`。

### 方式 3 · API (適合整合測試)

健康檢查：
```powershell
curl http://127.0.0.1:5000/api/health
```
回傳 Ollama 連線狀態 + 模型安裝清單。

只跑 Stage 1 (預覽，不用 LLM)：
```powershell
curl -F "file=@data/23325106_Discharge Note.pdf" http://127.0.0.1:5000/api/extract
```

完整管線 (SSE 串流)：
```powershell
curl -N -F "file=@data/23325106_Discharge Note.pdf" http://127.0.0.1:5000/api/summarize
```

事件類型：`status`, `archive_progress`, `archive_done`, `agent_progress` (含 `worker_start`/`supervisor`/`supervisor_pass`/`worker_revise`/`section_done`), `delta` (最終 markdown), `done`, `error`。

---

## 驗證指南

每次跑完 `scripts/run_e2e.py` 之後，比較這三個面向：

| 檢查項目 | 做法 |
|---|---|
| **結構化抽取正確** | 看 `sample_patient_record.json` 的 `timeline[]` — 重要日期 / 藥名 / 劑量是否都在 (對照原 PDF 的 `(14) 病史` 和 `(16) 住院治療經過`) |
| **摘要與 ground truth 一致** | `sample_final_summary.md` 對照 `data/23325106_Summary.pdf` — 日期、藥名、cGy 劑量、化療週次必須完全相符 |
| **監察者有確實查核** | `sample_review_log.json` — 每段 `verifications[]` 是否有 ≥3 個主張、各帶 PatientRecord 引用依據 |
| **沒有跨段污染** | 每段內容應**只含**該段標題；不可在「入院原因」段裡看到「一、主要診斷」等其他段標題 |

---

## 專案結構

```
ClinSum/
├── app.py                       Flask + SSE pipeline orchestrator
├── config.py                    SSH / 模型 / port 設定
├── requirements.txt
├── README.md                    (this file)
│
├── pipeline/
│   ├── __init__.py
│   ├── schema.py                PatientRecord, TimelineEvent, Diagnosis, …
│   ├── extractor.py             Stage 1 — pdfplumber + 段落切割
│   ├── archiver.py              Stage 2 — Gemma4 結構化抽取
│   ├── agents.py                Stage 3 — Worker / TeamSupervisor / SubSupervisor
│   ├── prompts_archive.py       歸檔員 system prompts (TIMELINE_SYSTEM)
│   ├── prompts_agents.py        工作者/監察者 prompts (WORKER_SYSTEM, STRUCTURE_CHECK_SYSTEM …)
│   ├── llm.py                   Ollama /api/chat client (think=False for gemma4)
│   └── tunnel.py                SSH local-port forwarder (paramiko)
│
├── templates/index.html         單頁 UI (兩欄式：上傳 + 摘要)
├── static/
│   ├── style.css                樣式 (含階段進度、段落 list、審查日誌)
│   └── script.js                SSE 消費 + Markdown 即時 render
│
├── scripts/
│   └── run_e2e.py               CLI 全管線 (跑完寫 3 個檔案到 data/)
│
└── data/
    ├── 23325106_Discharge Note.pdf   範例輸入 (12 頁)
    ├── 23325106_Summary.pdf          人工標準輸出 (比對基準)
    ├── Prompt.docx                   SOP 規格 (情境一 / 情境二)
    ├── sample_patient_record.json    Stage 2 輸出 (run_e2e.py 產生)
    ├── sample_final_summary.md       最終摘要 (run_e2e.py 產生)
    └── sample_review_log.json        審查紀錄 (run_e2e.py 產生)
```

---

## 預期效能

實測在範例 PDF (12 頁、18869 字) 上：

| 階段 | 耗時 |
|---|---|
| Stage 1 (extract) | 0.4s |
| Stage 2 (archive, gemma4:31b × 4-5 chunks) | ~70s |
| Stage 3 (agents, gpt-oss:120b × 5 worker + supervisor × N) | ~5min |
| **總計** | **~6-7min** |

第一輪過審率：5/5 (沒有跨段污染) — 若任一段打到修訂上限，看 review_log 該段的 structure_error。

---

## 已知限制

- **轉院/門診計畫**：若原 PDF `(20) 出院照護計畫` 是 `Nil`，「四、出院前現況」段不會出現家屬安排，因為 `discharge_status_raw` 為空
- **入院前 9 月 Jeavons 事件**：在「主要診斷」段提到，但「病史時間序」bullet 不會列出 (因為 `(14) 病史` 原文沒寫，是診斷裡才有的資訊)
- **檢驗谷底數值的時間定位**：worker 寫的「4/20 WBC 0.31K」靠 `abnormal_labs[]` 確認，正確；但若同一檢驗有多個區間 (如 BE(b) 4/17 有正負兩個值)，模型可能會選錯一個 → 待補：在歸檔員加極值聚合
- **PDF 必須是文字型** (非掃描)。若 pdfplumber 抽到字數 < 200，會 `PDFExtractionError`；Gemma4 視覺 OCR fallback 尚未啟用

---

## 排錯

| 症狀 | 排查 |
|---|---|
| `/api/health` 回 503 | 檢查 SSH tunnel：`python -c "from pipeline.tunnel import get_or_create_tunnel; import config; t=get_or_create_tunnel(config.SSH_HOST,config.SSH_PORT,config.SSH_USER,config.SSH_PASSWORD,config.REMOTE_OLLAMA_HOST,config.REMOTE_OLLAMA_PORT); print(t.local_url)"` |
| 摘要中出現空白章節或 `資料未提及` 過多 | 看 `sample_patient_record.json` 對應欄位是否空 — 可能 Stage 2 的章節切割漏接，或 PDF 段落標頭格式不符 (`(N) 標題`) |
| 某段一直被監察者退回 | 開 `sample_review_log.json`，看該 round 的 `structure_error` 或 `verifications` 有哪些 verdict 為 `【錯誤】`；若是模型過嚴，調 `pipeline/prompts_agents.py` 對應段落的 `STRUCTURE_CHECK_SYSTEM` 必要元素 |
| Stage 3 速度過慢 | 降 `config.WORKER_OPTIONS["num_predict"]` 或 `MAX_REVISIONS` (`pipeline/agents.py`) 從 2 改 1 |

---

## 後續可改進

1. **歸檔員加極值聚合** — `abnormal_labs[]` 目前是每點獨立，工作者要自己找 nadir/peak。可加 `lab_extremes` 欄位
2. **Stage 2 加入院前夕事件抽取** — 病史最後段落 (`3/20 onset of pain`, `3/22 ED`) 容易被遺漏在 timeline 之外
3. **情境二 (`查病歷號`) 支援** — 目前只實作 Prompt.docx 情境一 (上傳 PDF)；情境二需接 PostgreSQL `patient_reports` 並抽歷次出院摘要
4. **視覺 OCR fallback** — 掃描檔 PDF 跳到 Gemma4 多模態視覺路徑
