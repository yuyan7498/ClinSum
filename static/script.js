(() => {
  const $ = (id) => document.getElementById(id);

  const dropzone = $("dropzone");
  const fileInput = $("file-input");
  const browseBtn = $("browse-btn");
  const dzEmpty = $("dz-empty");
  const dzFile = $("dz-file");
  const fileName = $("file-name");
  const fileStats = $("file-stats");
  const clearBtn = $("clear-file");
  const runBtn = $("run-btn");
  const runLabel = runBtn.querySelector(".btn-label");
  const runSpin = runBtn.querySelector(".btn-spinner");
  const output = $("output");
  const metaRow = $("meta-row");
  const metaElapsed = $("meta-elapsed");
  const metaEvents = $("meta-events");
  const metaClaims = $("meta-claims");
  const copyBtn = $("copy-btn");
  const downloadBtn = $("download-btn");
  const modelChip = $("model-chip");
  const healthBtn = $("health-btn");
  const diagOut = $("diag-out");
  const footerHost = $("footer-host");
  const basicInfoBox = $("basic-info");
  const basicInfoContent = $("basic-info-content");
  const pipelineBox = $("pipeline");
  const archiveSummary = $("archive-summary");
  const sectionList = $("section-list");
  const reviewLog = $("review-log");
  const reviewLogBody = $("review-log-body");

  let currentFile = null;
  let finalMarkdown = "";

  marked.setOptions({ breaks: false, gfm: true });

  /* ---------- Health check ---------- */
  async function ping() {
    try {
      const r = await fetch("/api/health");
      const d = await r.json();
      diagOut.textContent = JSON.stringify(d, null, 2);
      footerHost.textContent = d.host || "";
      modelChip.classList.toggle("ok", !!d.ok);
      modelChip.classList.toggle("err", !d.ok);
      modelChip.title = d.ok ? `已連線 · ${d.host}` : (d.error || "未連線");
    } catch (e) {
      modelChip.classList.add("err");
      diagOut.textContent = String(e);
    }
  }
  healthBtn?.addEventListener("click", ping);
  ping();

  /* ---------- File handling ---------- */
  function fmtBytes(n) {
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / 1024 / 1024).toFixed(2) + " MB";
  }

  async function handleFile(file) {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".pdf")) {
      alert("僅支援 PDF 檔");
      return;
    }
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
  dropzone.addEventListener("drop", (e) => {
    const f = e.dataTransfer?.files?.[0];
    if (f) handleFile(f);
  });
  clearBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    currentFile = null;
    fileInput.value = "";
    dzEmpty.hidden = false;
    dzFile.hidden = true;
    runBtn.disabled = true;
    basicInfoBox.hidden = true;
  });

  /* ---------- Pipeline stage helpers ---------- */
  function setStage(name, state, detail) {
    const el = pipelineBox.querySelector(`.stage[data-stage="${name}"]`);
    if (!el) return;
    el.classList.remove("active", "done", "error");
    if (state) el.classList.add(state);
    if (detail !== undefined) {
      el.querySelector(".stage-detail").textContent = detail;
    }
  }

  function setSection(name, statusText, kind) {
    const li = sectionList.querySelector(`li[data-section="${name}"]`);
    if (!li) return;
    li.classList.remove("active", "done", "warn", "err");
    if (kind) li.classList.add(kind);
    li.querySelector(".sect-status").textContent = statusText;
  }

  function renderArchiveSummary(s) {
    archiveSummary.hidden = false;
    archiveSummary.innerHTML = `
      <div class="kv-row"><span>病患</span><span>${s.patient || "—"}</span></div>
      <div class="kv-row"><span>病歷號</span><span>${s.chart_no || "—"}</span></div>
      <div class="kv-row"><span>入院日</span><span>${s.admission_date || "—"}</span></div>
      <div class="kv-row"><span>事件 (入院前 / 入院中)</span><span>${s.pre_admission_events} / ${s.in_admission_events}</span></div>
      <div class="kv-row"><span>診斷 (入院 / 出院)</span><span>${s.admission_diagnoses} / ${s.discharge_diagnoses}</span></div>
      <div class="kv-row"><span>異常檢驗 / 影像 / 病理</span><span>${s.abnormal_labs} / ${s.radiology} / ${s.pathology}</span></div>
    `;
  }

  function renderReviewLog(entries) {
    if (!entries?.length) return;
    reviewLog.hidden = false;
    reviewLogBody.innerHTML = entries.map((e) => {
      const tag = e.approved ? "✓" : (e.structural ? "結構修" : "事實修");
      const errs = e.errors?.length
        ? `<ul>${e.errors.map((er) =>
              `<li><b>${er.verdict}</b> ${er.claim}<br><i>${er.detail.slice(0, 200)}</i></li>`
            ).join("")}</ul>`
        : "";
      return `<div class="log-entry ${e.approved ? "ok" : "warn"}">
        <div><b>${e.section}</b> · 第 ${e.round} 輪 · ${tag} · ${e.claim_count} 主張</div>
        ${e.structure_error ? `<div class="struct-err">${e.structure_error}</div>` : ""}
        ${errs}
      </div>`;
    }).join("");
  }

  /* ---------- Run ---------- */
  runBtn.addEventListener("click", async () => {
    if (!currentFile) return;
    runBtn.disabled = true;
    runBtn.classList.add("loading");
    runSpin.hidden = false;
    runLabel.textContent = "管線執行中...";
    copyBtn.disabled = true;
    downloadBtn.disabled = true;
    metaRow.hidden = true;
    reviewLog.hidden = true;
    pipelineBox.hidden = false;
    output.innerHTML = `<div class="thinking-indicator"><span class="pulse"></span>啟動管線 ...</div>`;
    finalMarkdown = "";

    pipelineBox.querySelectorAll(".stage").forEach((el) => el.classList.remove("active", "done", "error"));
    sectionList.querySelectorAll("li").forEach((li) => { li.classList.remove("active", "done", "warn"); li.querySelector(".sect-status").textContent = "—"; });
    archiveSummary.hidden = true;
    setStage("extract", "active", "上傳並解析 PDF…");

    let archiveSummaryData = null;
    let reviewLogEntries = [];

    try {
      const fd = new FormData();
      fd.append("file", currentFile);
      const resp = await fetch("/api/summarize", { method: "POST", body: fd });
      if (!resp.ok || !resp.body) throw new Error("伺服器無回應");

      const reader = resp.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buf = "";

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
          handleEvent(evname, payload);
        }
      }

      function handleEvent(name, p) {
        if (name === "status") {
          if (p.stage === "extract_done") {
            setStage("extract", "done", p.message);
            setStage("archive", "active", "歸檔啟動中…");
          } else if (p.stage === "archive_start") {
            setStage("archive", "active", p.message);
          } else if (p.stage === "agents_start") {
            setStage("archive", "done");
            setStage("agents", "active", p.message);
          }
        } else if (name === "archive_progress") {
          setStage("archive", "active", p.message);
        } else if (name === "archive_done") {
          archiveSummaryData = p.summary;
          renderArchiveSummary(p.summary);
          setStage("archive", "done", p.message);
          const total = p.summary.pre_admission_events + p.summary.in_admission_events;
          metaEvents.textContent = `${total} 事件`;
        } else if (name === "agent_progress") {
          if (p.section) {
            if (p.stage === "worker_start") setSection(p.section, "工作者撰寫中", "active");
            else if (p.stage === "supervisor") setSection(p.section, `審查 ${p.round + 1}/3`, "active");
            else if (p.stage === "supervisor_pass") setSection(p.section, "通過 ✓", "done");
            else if (p.stage === "worker_revise") setSection(p.section, "修訂中", "active");
            else if (p.stage === "section_done" && !sectionList.querySelector(`li[data-section="${p.section}"]`).classList.contains("done")) setSection(p.section, "完成", "done");
            else if (p.stage === "warning") setSection(p.section, "達修訂上限", "warn");
          }
          setStage("agents", "active", p.message);
        } else if (name === "delta") {
          finalMarkdown += p.text;
          try { output.innerHTML = marked.parse(finalMarkdown); }
          catch { output.textContent = finalMarkdown; }
          output.scrollTop = output.scrollHeight;
        } else if (name === "done") {
          setStage("agents", "done");
          setStage("done", "done", `總時長 ${p.elapsed}s`);
          finalMarkdown = (p.final_text || finalMarkdown).trim();
          try { output.innerHTML = marked.parse(finalMarkdown); }
          catch { output.textContent = finalMarkdown; }
          metaRow.hidden = false;
          metaElapsed.textContent = `${p.elapsed}s`;
          reviewLogEntries = p.review_log || [];
          const totalClaims = reviewLogEntries.reduce((a, b) => a + (b.claim_count || 0), 0);
          metaClaims.textContent = `${totalClaims} 主張查核`;
          renderReviewLog(reviewLogEntries);
        } else if (name === "error") {
          throw new Error(p.message);
        }
      }
    } catch (e) {
      output.innerHTML = `<div class="placeholder"><h3 style="color:var(--error)">產生失敗</h3><p>${e.message}</p></div>`;
      pipelineBox.querySelector(".stage.active")?.classList.add("error");
    } finally {
      runBtn.disabled = !currentFile;
      runBtn.classList.remove("loading");
      runSpin.hidden = true;
      runLabel.textContent = "重新產生";
      copyBtn.disabled = !finalMarkdown;
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
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = `${stem}_summary.md`;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  });
})();
