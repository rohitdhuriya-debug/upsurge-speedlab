/* UPSURGE SpeedLab - single page console. Vanilla, no build step. */

const SPEEDS = [1.1, 1.25, 1.4, 1.5, 1.75, 2.0];
const PROFILES = [["voice_music", "Voice + music"], ["voice_only", "Voice only"]];
const REEL_WARN = "Above 1.5× on vertical content: burned-in captions become hard to read, " +
  "avatar mouth movement looks unnatural, and the music bed starts to smear.";

let batchId = null;
let jobs = [];
let pollTimer = null;
let uploading = false;
let revealAvailable = true;
const rowCache = new Map();   // job_id -> {tr, cells}
const openQA = new Set();

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ helpers */

function fmtSpeed(s) {
  return String(parseFloat(s)).replace(/\.0$/, "");
}

function fmtDuration(sec) {
  if (!sec && sec !== 0) return "—";
  const s = Math.max(0, sec);
  const m = Math.floor(s / 60);
  const r = s - m * 60;
  return m + ":" + (r < 10 ? "0" : "") + r.toFixed(1);
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.status + " " + res.statusText;
    try { const j = await res.json(); if (j.detail) msg = j.detail; } catch (e) {}
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

/* -------------------------------------------------------------- capabilities */

async function loadCapabilities() {
  try {
    const caps = await api("/api/capabilities");
    $("capline").textContent =
      "ffmpeg " + caps.ffmpeg_version + " · audio engine: " + caps.engine +
      " · port " + location.port;
    revealAvailable = caps.reveal_available !== false;
    $("capline").title = "ffmpeg: " + caps.ffmpeg_path +
      "\nffprobe: " + caps.ffprobe_path +
      "\nselected by: " + caps.binary_source;
    if (!caps.rubberband) {
      const b = $("banner");
      b.innerHTML = "<strong>rubberband not found &mdash; using atempo fallback.</strong> " +
        "Audio quality above 1.5× will be reduced. " +
        "Install the gyan.dev &lsquo;full&rsquo; FFmpeg build to fix." +
        (caps.forced_atempo ? " <code>(SPEEDLAB_FORCE_ATEMPO is set)</code>" : "");
      b.classList.remove("hidden");
    }
    if (!caps.ffmpeg_available) {
      const b = $("banner");
      b.innerHTML = "<strong>ffmpeg not found on PATH.</strong> Nothing can be processed " +
        "until it is installed.";
      b.classList.remove("hidden");
    }
  } catch (e) {
    $("capline").textContent = "capability check failed: " + e.message;
  }
}

/* --------------------------------------------------------------------- upload */

function wireDropzone() {
  const dz = $("dropzone");
  const picker = $("filepicker");

  dz.addEventListener("click", () => { if (!uploading) picker.click(); });
  picker.addEventListener("change", () => {
    if (picker.files.length) sendFiles(picker.files);
    picker.value = "";
  });

  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("over"); }));
  dz.addEventListener("drop", (e) => {
    if (e.dataTransfer.files.length) sendFiles(e.dataTransfer.files);
  });
}

async function sendFiles(fileList) {
  uploading = true;
  const dz = $("dropzone");
  dz.classList.add("busy");
  dz.querySelector(".dz-main").textContent =
    "probing " + fileList.length + " file" + (fileList.length === 1 ? "" : "s") + "…";

  const fd = new FormData();
  for (const f of fileList) fd.append("files", f, f.name);
  fd.append("match_loudness", $("master-loudness").checked ? "1" : "0");
  if (batchId) fd.append("batch_id", batchId);

  try {
    const res = await api("/api/upload", { method: "POST", body: fd });
    batchId = res.batch_id;
    location.hash = batchId;      // survive a page refresh mid-batch
    await refresh();
  } catch (e) {
    alert("Upload failed: " + e.message);
  } finally {
    uploading = false;
    dz.classList.remove("busy");
    dz.querySelector(".dz-main").textContent = "Drop video files here";
  }
}

/* ---------------------------------------------------------------- master bar */

function wireMaster() {
  const sel = $("master-speed");
  SPEEDS.forEach((s) => {
    const o = document.createElement("option");
    o.value = s; o.textContent = fmtSpeed(s) + "×";
    sel.appendChild(o);
  });
  sel.value = "1.25";

  // Master writes into every row once. Later per-row edits win because the row
  // value is read back from the server, never re-stamped on render.
  sel.addEventListener("change", () => applyToAll({ speed: parseFloat(sel.value) }));
  $("master-profile").addEventListener("change", () =>
    applyToAll({ audio_profile: $("master-profile").value }));

  $("master-loudness").addEventListener("change", async () => {
    if (!batchId) return;
    await api("/api/batches/" + batchId, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ match_loudness: $("master-loudness").checked }),
    });
  });

  $("btn-process").addEventListener("click", processAll);
  $("btn-cancel").addEventListener("click", cancelBatch);
  $("btn-clear").addEventListener("click", removeAll);
}

function editable(j) {
  return j.status === "queued" || j.status === "failed" || j.status === "skipped_duplicate";
}

async function applyToAll(patch) {
  const targets = jobs.filter(editable);
  for (const j of targets) {
    try { await patchJob(j.id, patch, false); } catch (e) { /* row shown as-is */ }
  }
  await refresh();
}

async function patchJob(id, patch, doRefresh = true) {
  await api("/api/jobs/" + id, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (doRefresh) await refresh();
}

async function processAll() {
  if (!batchId) return;
  $("btn-process").disabled = true;
  try {
    await api("/api/batches/" + batchId + "/start", { method: "POST" });
  } catch (e) {
    alert("Could not start: " + e.message);
  }
  await refresh();
  startPolling();
}

async function cancelBatch() {
  if (!batchId) return;
  await api("/api/batches/" + batchId + "/cancel", { method: "POST" });
  await refresh();
}

async function removeJob(id) {
  const job = jobs.find((j) => j.id === id);
  if (!job) return;
  const warnOutput = job.status === "done"
    ? "\n\nThe finished file in outbox/ is kept."
    : "";
  if (!confirm("Remove " + job.src_name + " from the list?" + warnOutput)) return;
  try {
    await api("/api/jobs/" + id, { method: "DELETE" });
  } catch (e) {
    alert("Could not remove: " + e.message);
    return;
  }
  const entry = rowCache.get(id);
  if (entry) { entry.tr.remove(); rowCache.delete(id); }
  openQA.delete(id);
  await refresh();
}

async function removeAll() {
  if (!batchId || !jobs.length) { clearAll(); return; }
  const delivered = jobs.filter((j) => j.status === "done").length;
  const note = delivered
    ? "\n\n" + delivered + " finished file" + (delivered === 1 ? "" : "s") +
      " in outbox/ will be kept."
    : "";
  if (!confirm("Remove all " + jobs.length + " files from the list?" + note)) return;
  try {
    await api("/api/batches/" + batchId, { method: "DELETE" });
  } catch (e) {
    alert("Could not remove: " + e.message);
    return;
  }
  clearAll();
}

function clearAll() {
  batchId = null;
  location.hash = "";
  jobs = [];
  rowCache.clear();
  openQA.clear();
  stopPolling();
  render();
}

/* ---------------------------------------------------------------------- state */

async function refresh() {
  if (!batchId) { render(); return; }
  try {
    const data = await api("/api/batches/" + batchId);
    jobs = data.jobs;
    $("master-loudness").checked = !!data.batch.match_loudness;
    $("btn-cancel").classList.toggle("hidden", !anyActive());
    render();
    if (anyActive()) startPolling(); else stopPolling();
  } catch (e) {
    stopPolling();
  }
}

function anyActive() {
  return jobs.some((j) => j.status === "running") ||
    (jobs.some((j) => j.status === "queued") && document.body.dataset.started === "1");
}

function startPolling() {
  document.body.dataset.started = "1";
  if (pollTimer) return;
  pollTimer = setInterval(refresh, 1000);
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  if (!jobs.some((j) => j.status === "running" || j.status === "queued")) {
    document.body.dataset.started = "0";
  }
  $("btn-process").disabled = !jobs.some(editable);
}

/* --------------------------------------------------------------------- render */

function render() {
  const tbody = $("jobrows");
  const has = jobs.length > 0;
  $("jobtable").classList.toggle("hidden", !has);
  $("master").classList.toggle("hidden", !has);
  $("empty").classList.toggle("hidden", has);

  const seen = new Set();
  jobs.forEach((j) => {
    seen.add(j.id);
    let entry = rowCache.get(j.id);
    if (!entry) {
      entry = buildRow(j);
      rowCache.set(j.id, entry);
      tbody.appendChild(entry.tr);
    }
    updateRow(entry, j);
  });

  for (const [id, entry] of rowCache) {
    if (!seen.has(id)) { entry.tr.remove(); rowCache.delete(id); }
  }

  $("btn-process").disabled = !jobs.some(editable);
}

function makeSelect(cls, options, onChange) {
  const s = document.createElement("select");
  s.className = cls;
  options.forEach(([v, label]) => {
    const o = document.createElement("option");
    o.value = v; o.textContent = label;
    s.appendChild(o);
  });
  s.addEventListener("change", onChange);
  return s;
}

function buildRow(j) {
  const tr = document.createElement("tr");

  const tdThumb = document.createElement("td");
  const img = document.createElement("img");
  img.className = "thumb";
  img.src = "/api/thumb/" + j.id;
  img.alt = "";
  img.onerror = () => { img.style.visibility = "hidden"; };
  tdThumb.appendChild(img);

  const tdName = document.createElement("td");
  tdName.className = "name";

  const tdDur = document.createElement("td");
  tdDur.className = "mono dim";

  const tdType = document.createElement("td");
  tdType.className = "mono dim";

  const tdSpeed = document.createElement("td");
  const speedSel = makeSelect("sp", SPEEDS.map((s) => [s, fmtSpeed(s) + "×"]),
    () => patchJob(j.id, { speed: parseFloat(speedSel.value) }));
  const badge = document.createElement("span");
  badge.className = "badge amber hidden";
  badge.textContent = "!";
  badge.title = REEL_WARN;
  tdSpeed.appendChild(speedSel);
  tdSpeed.appendChild(badge);

  const tdProfile = document.createElement("td");
  const profSel = makeSelect("pr", PROFILES,
    () => patchJob(j.id, { audio_profile: profSel.value }));
  tdProfile.appendChild(profSel);

  const tdFps = document.createElement("td");
  const fpsSel = makeSelect("fp", [["auto", "Auto"], ["30", "30"], ["60", "60"]],
    () => patchJob(j.id, { fps_mode: fpsSel.value }));
  tdFps.appendChild(fpsSel);

  const tdStatus = document.createElement("td");
  const tdOut = document.createElement("td");

  const tdDel = document.createElement("td");
  const del = document.createElement("button");
  del.className = "rowdel";
  del.textContent = "×";
  del.onclick = () => removeJob(j.id);
  tdDel.appendChild(del);

  [tdThumb, tdName, tdDur, tdType, tdSpeed, tdProfile, tdFps, tdStatus, tdOut, tdDel]
    .forEach((td) => tr.appendChild(td));

  return { tr, img, tdName, tdDur, tdType, speedSel, badge, profSel, fpsSel,
           tdStatus, tdOut, del };
}

function setSelect(sel, value, disabled) {
  if (document.activeElement !== sel && sel.value !== String(value)) sel.value = String(value);
  sel.disabled = disabled;
}

function updateRow(e, j) {
  const p = j.probe || {};
  e.tr.className = j.status === "skipped_duplicate" ? "dup"
    : (j.status === "failed" ? "failed" : "");

  e.tdName.textContent = j.src_name;
  e.tdDur.textContent = fmtDuration(p.duration);
  e.tdType.textContent = j.kind || "—";
  if (p.width) e.tdType.title = p.width + "×" + p.height + " @ " +
    (p.fps ? p.fps.toFixed(3) : "?") + "fps" + (p.has_audio ? "" : " · no audio");

  const locked = !editable(j);
  setSelect(e.speedSel, fmtSpeed(j.speed), locked);
  setSelect(e.profSel, j.audio_profile, locked);
  setSelect(e.fpsSel, j.fps_mode || "auto", locked);
  e.fpsSel.options[0].textContent = "Auto (" + (j.target_fps || "?") + ")";

  e.badge.classList.toggle("hidden", !(j.speed > 1.5 && j.kind === "reel"));

  // A running job holds an open ffmpeg process, so it cannot be removed mid-encode.
  e.del.disabled = j.status === "running";
  e.del.title = j.status === "running"
    ? "Cancel the batch before removing a running file"
    : (j.status === "done"
        ? "Remove from list and delete the uploaded copy. The delivered file in outbox is kept."
        : "Remove from list and delete the uploaded copy");

  renderStatus(e.tdStatus, j);
  renderOutput(e.tdOut, j);
}

function renderStatus(td, j) {
  td.innerHTML = "";
  const line = document.createElement("div");
  line.className = "mono";

  if (j.status === "queued") {
    line.className += " s-queued";
    line.textContent = "queued";
  } else if (j.status === "running") {
    line.className += " s-run";
    line.textContent = (j.stage || "working") + " " + Math.round((j.progress || 0) * 100) + "%";
    const bar = document.createElement("div");
    bar.className = "bar";
    const i = document.createElement("i");
    i.style.width = Math.round((j.progress || 0) * 100) + "%";
    bar.appendChild(i);
    td.appendChild(line);
    td.appendChild(bar);
    return;
  } else if (j.status === "done") {
    line.className += " s-done";
    const qa = j.qa || {};
    line.textContent = "✓ done · " + fmtDuration(qa.out_duration) +
      " @ " + (qa.out_fps || j.target_fps) + "fps";
  } else if (j.status === "skipped_duplicate") {
    line.className += " s-dup";
    line.textContent = "duplicate — not processed";
  } else if (j.status === "failed") {
    line.className += " s-failed";
    line.textContent = "failed";
  } else {
    line.textContent = j.status;
  }
  td.appendChild(line);

  if (j.error) {
    const err = document.createElement("div");
    err.className = "err";
    err.textContent = j.error;
    td.appendChild(err);
  }

  if (j.status === "skipped_duplicate") {
    const btn = document.createElement("button");
    btn.className = "mini";
    btn.textContent = "Force anyway";
    btn.onclick = () => patchJob(j.id, { force: true });
    td.appendChild(btn);
  }

  if (j.status === "failed" && j.kind) {
    const btn = document.createElement("button");
    btn.className = "mini";
    btn.textContent = "Retry";
    btn.onclick = () => patchJob(j.id, { retry: true });
    td.appendChild(btn);
    const log = document.createElement("a");
    log.className = "link";
    log.style.marginLeft = "8px";
    log.href = "/api/log/" + j.id;
    log.target = "_blank";
    log.textContent = "log";
    td.appendChild(log);
  }

  if (j.qa) {
    const toggle = document.createElement("button");
    toggle.className = "qa-toggle";
    toggle.style.display = "block";
    toggle.textContent = openQA.has(j.id) ? "hide QA" : "QA detail";
    toggle.onclick = () => {
      if (openQA.has(j.id)) openQA.delete(j.id); else openQA.add(j.id);
      renderStatus(td, j);
    };
    td.appendChild(toggle);

    if (openQA.has(j.id)) {
      const panel = document.createElement("div");
      panel.className = "qa-panel";
      (j.qa.checks || []).forEach((c) => {
        const row = document.createElement("div");
        row.className = c.passed ? "ok" : "no";
        row.textContent = (c.passed ? "✓ " : "✗ ") + c.name + ": " + c.detail;
        panel.appendChild(row);
      });
      if (j.qa.loudness) {
        const row = document.createElement("div");
        row.textContent = "loudness: " + JSON.stringify(j.qa.loudness);
        panel.appendChild(row);
      }
      td.appendChild(panel);
    }
  }
}

function renderOutput(td, j) {
  td.innerHTML = "";
  if (j.status !== "done" || !j.out_path) {
    td.innerHTML = '<span class="mono dim">—</span>';
    return;
  }
  const name = j.out_path.split(/[\\/]/).pop();

  if (revealAvailable) {
    const open = document.createElement("a");
    open.className = "link outname";
    open.href = "#";
    open.textContent = name;
    open.title = j.out_path + "\n(click to reveal in file manager)";
    open.onclick = (e) => {
      e.preventDefault();
      fetch("/api/reveal/" + j.id, { method: "POST" });
    };
    td.appendChild(open);
  } else {
    // Remote session: revealing a folder happens on the host, not here.
    const label = document.createElement("span");
    label.className = "mono outname";
    label.textContent = name;
    label.title = j.out_path;
    td.appendChild(label);
  }

  const play = document.createElement("a");
  play.className = "link dim";
  play.style.display = "block";
  play.href = "/api/output/" + j.id;
  play.target = "_blank";
  play.textContent = "open file";
  td.appendChild(play);
}

/* ----------------------------------------------------------------------- init */

loadCapabilities();
wireDropzone();
wireMaster();
render();

// A batch survives a reload: the id lives in the URL hash, state lives on the server.
const resumeId = location.hash.replace(/^#/, "").trim();
if (resumeId) {
  batchId = resumeId;
  refresh();
}
