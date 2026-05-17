// Council UI — WebSocket client for multi-CLI orchestration.

const CLIS = ["codex", "claude", "copilot", "gemini"];
const RECENTS_KEY = "council:recent-project-dirs";
const MAX_RECENTS = 5;

const state = {
  convId: null,
  ws: null,
  availableClis: {},
  inFlight: false,
};

function setInFlight(value) {
  state.inFlight = value;
  const btn = document.getElementById("send-btn");
  if (btn) {
    btn.disabled = value;
    btn.textContent = value ? "Sending..." : "Send to selected CLIs";
  }
}

// ---- DOM utils ----

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "className") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2).toLowerCase(), v);
    else node.setAttribute(k, v);
  }
  for (const c of children) {
    if (typeof c === "string") node.appendChild(document.createTextNode(c));
    else if (c) node.appendChild(c);
  }
  return node;
}

function buildPanes() {
  const panes = document.getElementById("panes");
  panes.innerHTML = "";
  for (const cli of CLIS) {
    const enabled = state.availableClis[cli]?.available;
    const pane = el(
      "div",
      { className: "pane", "data-cli": cli },
      el(
        "div",
        { className: "pane-head" },
        el("span", { className: "pane-title" }, cli),
        el("span", { className: enabled ? "pane-ok" : "pane-down" }, enabled ? "● ready" : "○ not installed"),
        el(
          "button",
          {
            className: "pane-clear",
            onClick: () => {
              pane.querySelector(".pane-body").textContent = "";
              pane.classList.remove("pane-error");
            },
          },
          "clear",
        ),
      ),
      el("pre", { className: "pane-body" }),
    );
    panes.appendChild(pane);
  }
}

function appendToPane(cli, kind, data) {
  const paneEl = document.querySelector(`.pane[data-cli="${cli}"]`);
  if (!paneEl) return;
  const body = paneEl.querySelector(".pane-body");
  const span = document.createElement("span");
  span.className = `chunk-${kind}`;
  span.textContent = data;
  body.appendChild(span);
  body.scrollTop = body.scrollHeight;
  if (kind === "error") {
    paneEl.classList.add("pane-error");
    paneEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

function setRunStatus(kind, text) {
  const wrap = document.getElementById("run-status");
  const msg = document.getElementById("status-msg");
  wrap.dataset.state = kind;
  msg.textContent = text;
}

function appendStatus(text, klass = "info") {
  const stateMap = { error: "error", phase: "phase", info: "info" };
  setRunStatus(stateMap[klass] || "info", text);
}

// ---- API ----

async function api(path, opts) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

async function newConversation() {
  const { id } = await api("/api/conversations", { method: "POST" });
  state.convId = id;
  document.getElementById("conv-id-display").textContent = id;
  document.getElementById("conv-id-display").classList.add("active");
  if (state.ws) state.ws.close();
  state.ws = new WebSocket(`ws://${location.host}/ws/${id}`);
  state.ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.cli === "*") {
      if (m.kind === "trust_required") {
        setInFlight(false);
        document.getElementById("trust-target").textContent = m.data;
        document.getElementById("trust-reason").textContent = m.reason || "needs approval";
        const dlg = document.getElementById("trust-dialog");
        const btn = document.getElementById("trust-approve");
        btn.onclick = () => approveTrust(m.data);
        dlg.showModal();
        return;
      }
      if (m.kind === "batch_done" || m.kind === "error") {
        setInFlight(false);
      }
      appendStatus(`[${m.kind}] ${m.data}`, m.kind === "error" ? "error" : m.kind === "phase" ? "phase" : "info");
      return;
    }
    const tag = m.label ? `[${m.label}] ` : "";
    appendToPane(m.cli, m.kind, tag + m.data);
  };
  state.ws.onclose = () => appendStatus("ws closed", "info");
  state.ws.onerror = () => appendStatus("ws error", "error");
  await new Promise((resolve) => {
    state.ws.onopen = () => {
      appendStatus(`conversation ${id} ready`, "info");
      resolve();
    };
  });
}

async function loadClis() {
  const data = await api("/api/clis");
  state.availableClis = data.clis;
  for (const cb of document.querySelectorAll("input[data-cli]")) {
    const name = cb.dataset.cli;
    const avail = state.availableClis[name]?.available;
    if (!avail) {
      cb.checked = false;
      cb.disabled = true;
      cb.parentElement.title = "not installed locally";
      cb.parentElement.classList.add("disabled");
    }
  }
}

function getSelectedClis() {
  return [...document.querySelectorAll("input[data-cli]:checked")].map(
    (cb) => cb.dataset.cli,
  );
}

async function send() {
  if (state.inFlight) {
    appendStatus("already sending — wait for current run to finish", "error");
    return;
  }
  if (!state.ws || state.ws.readyState !== 1) {
    appendStatus("no active conversation — creating one", "info");
    await newConversation();
  }
  const prompt = document.getElementById("prompt-input").value.trim();
  if (!prompt) {
    appendStatus("empty prompt", "error");
    return;
  }
  const clis = getSelectedClis();
  if (clis.length === 0) {
    appendStatus("select at least one CLI", "error");
    return;
  }
  const mode = document.getElementById("mode-select").value;
  const includeStatus = document.getElementById("include-status").checked;
  const projectDir = document.getElementById("project-dir").value.trim();
  if (projectDir) {
    localStorage.setItem("council:last-project-dir", projectDir);
    saveRecentDir(projectDir);
  }
  setInFlight(true);
  for (const p of document.querySelectorAll(".pane.pane-error")) {
    p.classList.remove("pane-error");
  }
  state.ws.send(
    JSON.stringify({
      action: "send",
      prompt,
      clis,
      mode,
      include_status: includeStatus,
      project_dir: projectDir,
    }),
  );
  appendStatus(`mode=${mode} → ${clis.join(", ")} (cwd=${projectDir || "council"})`, "phase");
}

// ---- Trust ----

async function openTrustList() {
  const data = await api("/api/trust");
  const list = document.getElementById("trust-list-items");
  list.innerHTML = "";
  if (data.trusted.length === 0) {
    list.appendChild(el("li", { className: "muted" }, "no trusted folders yet"));
  } else {
    for (const path of data.trusted) {
      const item = el(
        "li",
        {},
        el("code", {}, path),
        " ",
        el("button", {
          className: "trust-revoke",
          onClick: async (e) => {
            e.preventDefault();
            await api("/api/trust/revoke", {
              method: "POST",
              body: JSON.stringify({ project_dir: path, note: "" }),
            });
            openTrustList();
          },
        }, "revoke"),
      );
      list.appendChild(item);
    }
  }
  document.getElementById("trust-list-dialog").showModal();
}

async function approveTrust(target) {
  try {
    await api("/api/trust/approve", {
      method: "POST",
      body: JSON.stringify({ project_dir: target, note: "from Council UI" }),
    });
    appendStatus(`approved: ${target}`, "info");
    document.getElementById("trust-dialog").close();
    send();
  } catch (err) {
    appendStatus(`approve failed: ${err.message}`, "error");
  }
}

// ---- Status editor ----

async function openStatus() {
  const data = await api("/api/status");
  document.getElementById("status-editor").value = data.status;
  document.getElementById("status-dialog").showModal();
}

async function saveStatus() {
  const value = document.getElementById("status-editor").value;
  const data = await api("/api/status", {
    method: "POST",
    body: JSON.stringify({ status: value }),
  });
  appendStatus(`status saved (${data.bytes} bytes)`, "info");
  document.getElementById("status-dialog").close();
}

// ---- Recent project dirs ----

function loadRecentDirs() {
  try {
    return JSON.parse(localStorage.getItem(RECENTS_KEY) || "[]");
  } catch {
    return [];
  }
}

function saveRecentDir(dir) {
  if (!dir) return;
  const next = [dir, ...loadRecentDirs().filter((x) => x !== dir)].slice(0, MAX_RECENTS);
  localStorage.setItem(RECENTS_KEY, JSON.stringify(next));
  renderRecentDirs();
}

function renderRecentDirs() {
  const dl = document.getElementById("recent-projects");
  if (!dl) return;
  dl.innerHTML = "";
  for (const dir of loadRecentDirs()) {
    dl.appendChild(el("option", { value: dir }));
  }
}

function normalizeDroppedPath(raw) {
  if (!raw) return "";
  const trimmed = raw.trim();
  if (trimmed.startsWith("file:///")) {
    const decoded = decodeURIComponent(trimmed.slice("file:///".length));
    if (/^[A-Za-z]:/.test(decoded)) return decoded.replaceAll("/", "\\");
    return "/" + decoded;
  }
  if (trimmed.startsWith("file://")) {
    return decodeURIComponent(trimmed.slice("file://".length));
  }
  if (/^[A-Za-z]:[\\/]/.test(trimmed)) return trimmed;
  if (trimmed.startsWith("/")) return trimmed;
  return "";
}

function wireProjectDirDnD() {
  const input = document.getElementById("project-dir");
  if (!input) return;
  input.addEventListener("dragover", (e) => {
    e.preventDefault();
    input.classList.add("drop-target");
  });
  input.addEventListener("dragleave", () => input.classList.remove("drop-target"));
  input.addEventListener("drop", (e) => {
    e.preventDefault();
    input.classList.remove("drop-target");
    const uri =
      e.dataTransfer?.getData("text/uri-list") ||
      e.dataTransfer?.getData("text/plain") ||
      "";
    const path = normalizeDroppedPath(uri.split("\n")[0]);
    if (path) {
      input.value = path;
      appendStatus(`project folder set: ${path}`, "info");
      updateProjectHint();
    } else {
      appendStatus("drop failed — paste absolute path instead", "error");
    }
  });
  input.addEventListener("input", updateProjectHint);
}

function updateProjectHint() {
  const input = document.getElementById("project-dir");
  const hint = document.getElementById("project-hint");
  if (!input || !hint) return;
  if (input.value.trim()) {
    hint.textContent = `cwd: ${input.value.trim()}`;
    hint.classList.remove("muted");
  } else {
    hint.textContent = "using Council cwd";
    hint.classList.add("muted");
  }
}

// ---- Init ----

window.addEventListener("DOMContentLoaded", async () => {
  buildPanes();
  await loadClis();
  buildPanes();

  const lastProjectDir = localStorage.getItem("council:last-project-dir");
  if (lastProjectDir) document.getElementById("project-dir").value = lastProjectDir;

  renderRecentDirs();
  wireProjectDirDnD();
  updateProjectHint();

  document.getElementById("new-conv").addEventListener("click", newConversation);
  document.getElementById("send-btn").addEventListener("click", send);
  document.getElementById("open-status").addEventListener("click", openStatus);
  document.getElementById("save-status").addEventListener("click", saveStatus);
  document.getElementById("trust-list").addEventListener("click", openTrustList);

  document.getElementById("prompt-input").addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") send();
  });
});
