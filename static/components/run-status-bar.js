// <run-status-bar>
// data-state on the host controls visual accent (info|phase|error).

export class RunStatusBar extends HTMLElement {
  connectedCallback() {
    if (this._rendered) return;
    this._rendered = true;
    this.classList.add("run-status");
    this.setAttribute("role", "status");
    this.setAttribute("aria-live", "polite");
    if (!this.dataset.state) this.dataset.state = "info";

    const icon = document.createElement("span");
    icon.className = "run-status-icon";
    icon.textContent = "●";
    this.appendChild(icon);

    const text = document.createElement("span");
    text.className = "run-status-text";
    text.id = "status-msg";
    text.textContent = "ready";
    this.appendChild(text);
    this._text = text;
  }

  set(state, message) {
    this.dataset.state = state;
    if (this._text) this._text.textContent = message;
  }
}

customElements.define("run-status-bar", RunStatusBar);
