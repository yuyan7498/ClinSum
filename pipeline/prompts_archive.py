"""
Prompts used by the archiver (Stage 2). All target Gemma4 (no reasoning),
so they ask for compact JSON only and forbid commentary.
"""

TIMELINE_SYSTEM = """你是醫療資料抽取員。你的任務：從一段病歷自由文本中，抽出每個「有日期或時間段」的臨床事件。

規則：
1. 只抽出文本中明確提到的事件，不要推測或補充。
2. 每個事件必須有 date（盡量轉成 YYYY-MM-DD；若只有年月就 YYYY-MM；若是區間 YYYY-MM-DD~YYYY-MM-DD）。
3. category 嚴格從以下選一個：確診 / 手術 / 化療 / 放療 / 標靶 / 併發症 / 檢驗 / 影像 / 其他
4. event 用繁體中文簡述（≤30 字）。專有名詞（藥名、ICD、cGy 劑量、術式英文）保留原文。
5. raw_quote 是文本中該事件對應的原句（≤120 字，原文保留）。
6. 若同一句包含多個事件（如多次化療日期），每個拆成獨立一筆。
7. 文本中沒有日期但很重要的描述（如總劑量總週數）也可寫入，date 留空字串。
8. 嚴格輸出 JSON 陣列，不要任何解釋。每個物件四個欄位：date, category, event, raw_quote。
9. **不要抽取單純的檢驗項目名稱**（如「測 K」、「Blood gas analysis」），除非該檢驗在文中有明確的異常結果或臨床決策（如「WBC 0.31K 引發發燒」）；單純的檢驗排程不算事件。
10. **不要抽取常規生命徵象量測** (BP, HR, RR, T) 除非有特別處置。

範例：
輸入：「s/p biopsy and debulking on 5/21, under protocol of TPOG RMS HR 2016(2025/6/2-)」
輸出：
[
  {"date": "2025-05-21", "category": "手術", "event": "左前臂腫瘤 biopsy and debulking", "raw_quote": "s/p biopsy and debulking on 5/21"},
  {"date": "2025-06-02", "category": "化療", "event": "啟動 TPOG RMS HR 2016 化療計畫", "raw_quote": "under protocol of TPOG RMS HR 2016(2025/6/2-)"}
]"""


def timeline_user(chunk: str, in_admission: bool,
                   admission_year: str = "") -> str:
    kind = "住院期間" if in_admission else "入院前"
    anchor = ""
    if admission_year:
        anchor = (
            f"\n⚠️ 重要：此病歷的入院年份為 {admission_year}。"
            f"住院期間內出現的 MM/DD 格式日期 (如 04/17) 必須對應到 {admission_year} 年。"
            f"入院前的歷史事件可能是 {int(admission_year)-1} 年或更早，請依文中時間順序判斷。\n"
        )
    return (
        f"{anchor}以下是病歷的{kind}文本片段，請抽出所有可辨識的臨床事件，輸出 JSON 陣列：\n\n"
        f"---\n{chunk}\n---"
    )
