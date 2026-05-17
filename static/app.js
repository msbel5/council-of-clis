// Council UI controller — state + WebSocket only, all rendering in custom elements.

const RECENTS_KEY = "council:recent-project-dirs";
const MAX_RECENTS = 5;

const state = {
  convId: null,
  ws: null,
  registry: {},      // {name: {available, options_schema, ...}}
  selected: new Set(),
  options: {},       // {cli_name: {opt_name: value}}
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

// ---- Status bar ----

function appendStatus(text, klass = "info") {
  const bar = document.getElementById("run-status");
  const stateMap = { error: "error", phase: "phase", info: "info" };
  if (bar && typeof bar.set === "function") {
    bar.set(stateMap[klass] || "info", text);
  }
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

// ---- CLI cards + panes ----

function renderCliCards() {
  const container = document.getElementById("cli-cards-container");
  if (!container) return;
  container.innerHTML = "";
  for (const [name, info] of Object.entries(state.registry)) {
    const card = document.createElement("cli-card");
    card.setAttribute("name", name);
    card.setAttribute("available", String(Boolean(info.available)));
    card.setAttribute("experimental", String(Boolean(info.experimental)));
    card.setAttribute("selected", String(state.selected.has(name)));
    card.schema = info.options_schema || [];
    card.values = state.options[name] || {};
    container.appendChild(card);
  }
}

function renderPanes() {
  const panes = document.getElementById("panes");
  if (!panes) return;
  panes.innerHTML = "";
  for (const [name, info] of Object.entries(state.registry)) {
    const pane = document.createElement("cli-pane");
    pane.setAttribute("name", name);
    pane.setAttribute("available", String(Boolean(info.available)));
    panes.appendChild(pane);
  }
}

function paneFor(name) {
  return document.querySelector(`cli-pane[name="${CSS.escape(name)}"]`);
}

async function loadClis() {
  const data = await api("/api/clis");
  state.registry = data.clis || {};
  // Auto-select codex + claude if available and nothing selected yet.
  if (state.selected.size === 0) {
    for (const seed of ["codex", "claude"]) {
      if (state.registry[seed]?.available) state.selected.add(seed);
    }
  }
  renderCliCards();
  renderPanes();
}

// ---- Conversation ----

async function newConversation() {
  const { id } = await api("/api/conversations", { method: "POST" });
  state.convId = id;
  const chip = document.getElementById("conv-id-display");
  chip.textContent = id;
  chip.classList.add("active");
  if (state.ws) state.ws.close();
  state.ws = new WebSocket(`ws://${location.host}/ws/${id}`);
  state.ws.onmessage = onWsMessage;
  state.ws.onclose = () => appendStatus("ws closed", "info");
  state.ws.onerror = () => appendStatus("ws error", "error");
  await new Promise((resolve) => {
    state.ws.onopen = () => {
      appendStatus(`conversation ${id} ready`, "info");
      resolve();
    };
  });
}

function onWsMessage(ev) {
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
    appendStatus(
      `[${m.kind}] ${m.data}`,
      m.kind === "error" ? "error" : m.kind === "phase" ? "phase" : "info",
    );
    return;
  }
  const tag = m.label ? `[${m.label}] ` : "";
  const pane = paneFor(m.cli);
  if (pane) pane.appendChunk(m.kind, tag + m.data);
}

// ---- Send ----

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
  const clis = [...state.selected];
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
  for (const p of document.querySelectorAll("cli-pane.pane-error")) {
    p.markOk();
  }
  const cliOptions = {};
  for (const name of clis) {
    if (state.options[name] && Object.keys(state.options[name]).length > 0) {
      cliOptions[name] = state.options[name];
    }
  }
  state.ws.send(
    JSON.stringify({
      action: "send",
      prompt,
      clis,
      mode,
      include_status: includeStatus,
      project_dir: projectDir,
      cli_options: cliOptions,
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
      list.appendChild(
        el(
          "li",
          {},
          el("code", {}, path),
          " ",
          el(
            "button",
            {
              className: "trust-revoke",
              onClick: async (e) => {
                e.preventDefault();
                await api("/api/trust/revoke", {
                  method: "POST",
                  body: JSON.stringify({ project_dir: path, note: "" }),
                });
                openTrustList();
              },
            },
            "revoke",
          ),
        ),
      );
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

// ---- Project folder helpers ----

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

async function pickFolder() {
  const initial = document.getElementById("project-dir").value.trim();
  try {
    const data = await api("/api/fs/pick-folder", {
      method: "POST",
      body: JSON.stringify({ initial_dir: initial }),
    });
    if (data.cancelled) {
      appendStatus("folder picker cancelled", "info");
      return;
    }
    if (data.path) {
      document.getElementById("project-dir").value = data.path;
      saveRecentDir(data.path);
      updateProjectHint();
      appendStatus(`picked: ${data.path}`, "info");
    }
  } catch (err) {
    appendStatus(`picker failed: ${err.message}`, "error");
  }
}

// ---- Init ----

window.addEventListener("DOMContentLoaded", async () => {
  await loadClis();

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
  document.getElementById("browse-folder").addEventListener("click", pickFolder);

  // Listen for component events
  document.addEventListener("cli-card:toggle", (e) => {
    const { name, selected } = e.detail;
    if (selected) state.selected.add(name);
    else state.selected.delete(name);
  });
  document.addEventListener("cli-card:options", (e) => {
    const { name, options } = e.detail;
    state.options[name] = options;
  });

  document.getElementById("prompt-input").addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") send();
  });
});
