// Council UI — WebSocket client for multi-CLI orchestration.

const CLIS = ["codex", "claude", "copilot", "gemini"];

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
            onClick: () => pane.querySelector(".pane-body").textContent = "",
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
  const pane = document.querySelector(`.pane[data-cli="${cli}"] .pane-body`);
  if (!pane) return;
  const span = document.createElement("span");
  span.className = `chunk-${kind}`;
  span.textContent = data;
  pane.appendChild(span);
  pane.scrollTop = pane.scrollHeight;
}

function appendStatus(msg, klass = "info") {
  const log = document.getElementById("status-msg");
  log.textContent = msg;
  log.className = klass;
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
  if (state.ws) state.ws.close();
  state.ws = new WebSocket(`ws://${location.host}/ws/${id}`);
  state.ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.cli === "*") {
      if (m.kind === "trust_required") {
        setInFlight(false);  // not actually running — waiting on user approval
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
      const cls = m.kind === "error" ? "error" : m.kind === "phase" ? "phase" : "info";
      appendStatus(`[${m.kind}] ${m.data}`, cls);
      return;
    }
    // Show label (round/phase tag) as a prefix on each chunk
    const tag = m.label ? `[${m.label}] ` : "";
    appendToPane(m.cli, m.kind, tag + m.data);
  };
  state.ws.onclose = () => appendStatus("ws closed", "info");
  state.ws.onerror = () => appendStatus("ws error", "error");
  // wait for ws open
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
  // Disable checkboxes for unavailable CLIs
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
  // Remember last-used project dir
  if (projectDir) localStorage.setItem("council:last-project-dir", projectDir);
  setInFlight(true);
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
  appendStatus(`mode=${mode} → ${clis.join(", ")} (cwd=${projectDir || "council"})...`, "info");
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
    send();  // retry the prompt
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

// ---- Init ----

window.addEventListener("DOMContentLoaded", async () => {
  buildPanes();
  await loadClis();
  buildPanes(); // re-render with availability info

  // Restore last-used project dir
  const lastProjectDir = localStorage.getItem("council:last-project-dir");
  if (lastProjectDir) document.getElementById("project-dir").value = lastProjectDir;

  document.getElementById("new-conv").addEventListener("click", newConversation);
  document.getElementById("send-btn").addEventListener("click", send);
  document.getElementById("open-status").addEventListener("click", openStatus);
  document.getElementById("save-status").addEventListener("click", saveStatus);
  document.getElementById("trust-list").addEventListener("click", openTrustList);

  document.getElementById("prompt-input").addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") send();
  });
});
