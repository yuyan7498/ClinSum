(() => {
  const $ = (id) => document.getElementById(id);

  const dropzone       = $("dropzone");
  const fileInput      = $("file-input");
  const browseBtn      = $("browse-btn");
  const dzEmpty        = $("dz-empty");
  const dzFile         = $("dz-file");
  const fileName       = $("file-name");
  const fileStats      = $("file-stats");
  const clearBtn       = $("clear-file");
  const runBtn         = $("run-btn");
  const runLabel       = runBtn.querySelector(".btn-label");
  const runSpin        = runBtn.querySelector(".btn-spinner");
  const output         = $("output");
  const metaRow        = $("meta-row");
  const metaElapsed    = $("meta-elapsed");
  const metaMode       = $("meta-mode");
  const copyBtn        = $("copy-btn");
  const downloadBtn    = $("download-btn");
  const modelChip      = $("model-chip");
  const healthBtn      = $("health-btn");
  const diagOut        = $("diag-out");
  const footerHost     = $("footer-host");
  const basicInfoBox   = $("basic-info");
  const basicInfoContent = $("basic-info-content");
  const promptEditor   = $("prompt-editor");
  const promptTextarea = $("prompt-textarea");
  const resetPromptBtn = $("reset-prompt-btn");
  const modeRadios     = document.querySelectorAll('input[name="mode"]');

  let currentFile  = null;
  let finalMarkdown = "";
  const defaultPrompts = {};

  marked.setOptions({ breaks: false, gfm: true });

  /* ---------- Mode ---------- */
  function currentMode() {
    return document.querySelector('input[name="mode"]:checked')?.value || "quick";
  }

  function applyMode() {
    const m = currentMode();
    promptTextarea.value = defaultPrompts[m] || "";
    const labels = { quick: "啟動摘要（完整）", quick_no_lab: "啟動摘要（去除檢驗）" };
    runLabel.textContent = labels[m] || "啟動摘要";
  }
  modeRadios.forEach((r) => r.addEventListener("change", applyMode));

  async function loadDefaultPrompts() {
    try {
      const [r1, r2] = await Promise.all([
        fetch("/api/quick/default_prompt"),
        fetch("/api/quick_no_lab/default_prompt"),
      ]);
      const [d1, d2] = await Promise.all([r1.json(), r2.json()]);
      defaultPrompts["quick"]        = d1.prompt || "";
      defaultPrompts["quick_no_lab"] = d2.prompt || "";
      promptTextarea.value = defaultPrompts[currentMode()] || "";
    } catch {}
  }
  resetPromptBtn.addEventListener("click", () => {
    promptTextarea.value = defaultPrompts[currentMode()] || "";
  });
  loadDefaultPrompts();
  applyMode();

  /* ---------- Health ---------- */
  async function ping() {
    try {
      const r = await fetch("/api/health");
      const d = await r.json();
      diagOut.textContent = JSON.stringify(d, null, 2);
      footerHost.textContent = d.host || "";
      modelChip.classList.toggle("ok",  !!d.ok);
      modelChip.classList.toggle("err", !d.ok);
      modelChip.title = d.ok ? `已連線 · ${d.host}` : (d.error || "未連線");
    } catch (e) {
      modelChip.classList.add("err");
      diagOut.textContent = String(e);
    }
  }
  healthBtn?.addEventListener("click", ping);
  ping();

  /* ---------- File ---------- */
  function fmtBytes(n) {
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / 1024 / 1024).toFixed(2) + " MB";
  }

  async function handleFile(file) {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".pdf")) { alert("僅支援 PDF 檔"); return; }
    currentFile = file;
    fileName.textContent = file.name;
    fileStats.textContent = `${fmtBytes(file.size)} · 解析中...`;
    dzEmpty.hidden = true;
    dzFile.hidden = false;
    runBtn.disabled = true;
    basicInfoBox.hidden = true;

    const fd = new FormData();
    fd.append("file", file);
    try {
      const r = await fetch("/api/extract", { method: "POST", body: fd });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "PDF 解析失敗");
      fileStats.textContent = `${fmtBytes(file.size)} · ${d.pages} 頁 · ${d.chars.toLocaleString()} 字 · ${d.sections.length} 段`;
      if (d.basic_info) {
        basicInfoBox.hidden = false;
        basicInfoContent.textContent = d.basic_info;
      }
      runBtn.disabled = false;
    } catch (e) {
      fileStats.textContent = "解析失敗：" + e.message;
      currentFile = null;
    }
  }

  browseBtn.addEventListener("click", (e) => { e.stopPropagation(); fileInput.click(); });
  dropzone.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => handleFile(fileInput.files[0]));
  ["dragenter", "dragover"].forEach((ev) =>
    dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.add("dragover"); })
  );
  ["dragleave", "drop"].forEach((ev) =>
    dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.remove("dragover"); })
  );
  dropzone.addEventListener("drop", (e) => { const f = e.dataTransfer?.files?.[0]; if (f) handleFile(f); });
  clearBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    currentFile = null;
    fileInput.value = "";
    dzEmpty.hidden = false;
    dzFile.hidden = true;
    runBtn.disabled = true;
    basicInfoBox.hidden = true;
  });

  /* ---------- Run ---------- */
  runBtn.addEventListener("click", async () => {
    if (!currentFile) return;
    const mode = currentMode();
    runBtn.disabled = true;
    runBtn.classList.add("loading");
    runSpin.hidden = false;
    runLabel.textContent = "摘要產生中...";
    copyBtn.disabled = true;
    downloadBtn.disabled = true;
    metaRow.hidden = true;
    finalMarkdown = "";

    output.innerHTML = `<div class="thinking-indicator"><span class="pulse"></span>送往 LLM ...</div>`;

    try {
      const fd = new FormData();
      fd.append("file", currentFile);
      fd.append("prompt", promptTextarea.value || defaultPrompts[mode] || "");
      const endpoint = mode === "quick_no_lab" ? "/api/quick_no_lab" : "/api/quick";
      const resp = await fetch(endpoint, { method: "POST", body: fd });
      if (!resp.ok || !resp.body) {
        let msg = "伺服器無回應";
        try { const j = await resp.json(); if (j.error) msg = j.error; } catch {}
        throw new Error(msg);
      }

      const reader  = resp.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buf = "";
      let started = false;

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const raw = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          let evname = "message", data = "";
          for (const line of raw.split("\n")) {
            if (line.startsWith("event:")) evname = line.slice(6).trim();
            else if (line.startsWith("data:")) data += line.slice(5).trimStart();
          }
          let payload;
          try { payload = JSON.parse(data); } catch { continue; }

          if (evname === "delta") {
            if (!started) { output.innerHTML = ""; started = true; }
            finalMarkdown += payload.text;
            try { output.innerHTML = marked.parse(finalMarkdown); } catch { output.textContent = finalMarkdown; }
            output.scrollTop = output.scrollHeight;
          } else if (evname === "done") {
            finalMarkdown = (payload.final_text || finalMarkdown).trim();
            try { output.innerHTML = marked.parse(finalMarkdown); } catch { output.textContent = finalMarkdown; }
            metaRow.hidden = false;
            metaElapsed.textContent = `${payload.elapsed}s`;
            metaMode.textContent = mode === "quick_no_lab" ? "去除檢驗/影像" : "含完整資料";
          } else if (evname === "error") {
            throw new Error(payload.message);
          }
        }
      }
    } catch (e) {
      output.innerHTML = `<div class="placeholder"><h3 style="color:var(--error)">產生失敗</h3><p>${e.message}</p></div>`;
    } finally {
      runBtn.disabled = !currentFile;
      runBtn.classList.remove("loading");
      runSpin.hidden = true;
      const labels = { quick: "啟動摘要（完整）", quick_no_lab: "啟動摘要（去除檢驗）" };
      runLabel.textContent = labels[currentMode()] || "啟動摘要";
      copyBtn.disabled    = !finalMarkdown;
      downloadBtn.disabled = !finalMarkdown;
    }
  });

  /* ---------- Copy / Download ---------- */
  copyBtn.addEventListener("click", async () => {
    if (!finalMarkdown) return;
    await navigator.clipboard.writeText(finalMarkdown);
    const old = copyBtn.textContent;
    copyBtn.textContent = "已複製 ✓";
    setTimeout(() => (copyBtn.textContent = old), 1500);
  });
  downloadBtn.addEventListener("click", () => {
    if (!finalMarkdown) return;
    const stem = (currentFile?.name || "summary").replace(/\.pdf$/i, "");
    const blob = new Blob([finalMarkdown], { type: "text/markdown;charset=utf-8" });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement("a");
    a.href = url; a.download = `${stem}_summary.md`;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  });
})();
