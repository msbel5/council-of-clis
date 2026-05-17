// <cli-pane name="codex" available="true">
//
// Renders pane head (title + status + clear) and a <pre> body. Supports:
//   - .appendChunk(kind, text) to stream output
//   - .clear() to wipe body and reset error state
//   - .markError() / .markOk() for visual state

export class CLIPane extends HTMLElement {
  static get observedAttributes() {
    return ["name", "available"];
  }

  connectedCallback() {
    if (this._rendered) return;
    this._rendered = true;
    this._render();
  }

  attributeChangedCallback() {
    if (this._rendered) this._render();
  }

  appendChunk(kind, text) {
    if (!this._body) return;
    const span = document.createElement("span");
    span.className = `chunk-${kind}`;
    span.textContent = text;
    this._body.appendChild(span);
    this._body.scrollTop = this._body.scrollHeight;
    if (kind === "error") this.markError();
  }

  clear() {
    if (this._body) this._body.textContent = "";
    this.classList.remove("pane-error");
  }

  markError() {
    this.classList.add("pane-error");
    this.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  markOk() {
    this.classList.remove("pane-error");
  }

  _render() {
    const name = this.getAttribute("name") || "";
    const available = this.getAttribute("available") === "true";

    this.innerHTML = "";
    this.classList.add("pane");
    this.setAttribute("data-cli", name);

    const head = document.createElement("div");
    head.className = "pane-head";

    const title = document.createElement("span");
    title.className = "pane-title";
    title.textContent = name;
    head.appendChild(title);

    const status = document.createElement("span");
    status.className = available ? "pane-ok" : "pane-down";
    status.textContent = available ? "● ready" : "○ not installed";
    head.appendChild(status);

    const clearBtn = document.createElement("button");
    clearBtn.type = "button";
    clearBtn.className = "pane-clear";
    clearBtn.textContent = "clear";
    clearBtn.addEventListener("click", () => this.clear());
    head.appendChild(clearBtn);

    const body = document.createElement("pre");
    body.className = "pane-body";

    this.appendChild(head);
    this.appendChild(body);
    this._body = body;
  }
}

customElements.define("cli-pane", CLIPane);
