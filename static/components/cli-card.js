// <cli-card name="codex" available="true" experimental="false"
//           selected="true" options-schema="[...]" options-value="{...}">
//
// Renders a checkbox + name + status badge + ⚙️ button that opens an <options-popover>.
// Emits events:
//   - cli-card:toggle   detail = { name, selected }
//   - cli-card:options  detail = { name, options }
//
// The element does not own the registry. It receives schema + value via attributes/properties
// and emits change events upward — keeping state ownership in the page-level controller.

export class CLICard extends HTMLElement {
  static get observedAttributes() {
    return ["name", "available", "experimental", "selected"];
  }

  constructor() {
    super();
    this._schema = [];
    this._values = {};
  }

  set schema(value) {
    this._schema = Array.isArray(value) ? value : [];
    this._renderGear();
  }

  set values(value) {
    this._values = value && typeof value === "object" ? { ...value } : {};
    this._renderGear();
  }

  get values() {
    return { ...this._values };
  }

  connectedCallback() {
    if (this._rendered) return;
    this._rendered = true;
    this._render();
  }

  attributeChangedCallback() {
    if (this._rendered) this._render();
  }

  _render() {
    const name = this.getAttribute("name") || "";
    const available = this.getAttribute("available") === "true";
    const experimental = this.getAttribute("experimental") === "true";
    const selected = this.getAttribute("selected") === "true";

    this.innerHTML = "";
    const label = document.createElement("label");
    label.className = "cli-card-label";
    if (!available) label.classList.add("disabled");
    if (experimental) label.classList.add("experimental");

    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.disabled = !available;
    cb.checked = selected && available;
    cb.addEventListener("change", () => {
      this.setAttribute("selected", String(cb.checked));
      this.dispatchEvent(
        new CustomEvent("cli-card:toggle", {
          bubbles: true,
          detail: { name, selected: cb.checked },
        }),
      );
    });

    label.appendChild(cb);
    label.appendChild(document.createTextNode(" " + name));

    if (experimental) {
      const badge = document.createElement("span");
      badge.className = "cli-card-badge";
      badge.textContent = "beta";
      label.appendChild(badge);
    }

    this.appendChild(label);

    this._gearBtn = document.createElement("button");
    this._gearBtn.type = "button";
    this._gearBtn.className = "cli-card-gear";
    this._gearBtn.textContent = "⚙️";
    this._gearBtn.title = "Options";
    this._gearBtn.disabled = this._schema.length === 0;
    this._gearBtn.addEventListener("click", () => this._openPopover());
    this.appendChild(this._gearBtn);

    this._renderGear();
  }

  _renderGear() {
    if (!this._gearBtn) return;
    this._gearBtn.disabled = this._schema.length === 0;
    const overrideCount = Object.keys(this._values).length;
    this._gearBtn.classList.toggle("has-overrides", overrideCount > 0);
  }

  _openPopover() {
    const popover = document.createElement("options-popover");
    popover.schema = this._schema;
    popover.values = this._values;
    popover.cliName = this.getAttribute("name") || "";
    popover.addEventListener("options-popover:save", (e) => {
      this._values = { ...e.detail.values };
      this._renderGear();
      this.dispatchEvent(
        new CustomEvent("cli-card:options", {
          bubbles: true,
          detail: { name: this.getAttribute("name") || "", options: { ...this._values } },
        }),
      );
      popover.remove();
    });
    popover.addEventListener("options-popover:close", () => popover.remove());
    document.body.appendChild(popover);
    popover.open();
  }
}

customElements.define("cli-card", CLICard);
