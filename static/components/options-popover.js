// <options-popover schema="[...]" values="{...}" cli-name="codex">
//
// Generic popover that renders inputs based on an options schema. Supports:
//   enum   -> <select>
//   bool   -> checkbox
//   number -> <input type=number> with min/max
//   string -> <input type=text>
//
// Emits:
//   - options-popover:save    detail = { cliName, values }
//   - options-popover:close

export class OptionsPopover extends HTMLElement {
  constructor() {
    super();
    this._schema = [];
    this._values = {};
    this._cliName = "";
  }

  set schema(v) {
    this._schema = Array.isArray(v) ? v : [];
  }

  set values(v) {
    this._values = v && typeof v === "object" ? { ...v } : {};
  }

  set cliName(v) {
    this._cliName = String(v || "");
  }

  open() {
    if (this._dialog) {
      this._dialog.showModal();
      return;
    }
    this._render();
    this._dialog.showModal();
  }

  _render() {
    const dialog = document.createElement("dialog");
    dialog.className = "options-popover-dialog";

    const form = document.createElement("form");
    form.method = "dialog";

    const h = document.createElement("h2");
    h.textContent = `⚙️ ${this._cliName} options`;
    form.appendChild(h);

    if (this._schema.length === 0) {
      const p = document.createElement("p");
      p.className = "muted";
      p.textContent = "No configurable options for this CLI yet.";
      form.appendChild(p);
    }

    const fieldMap = new Map();
    for (const opt of this._schema) {
      const row = document.createElement("div");
      row.className = "options-row";

      const lbl = document.createElement("label");
      lbl.className = "options-label";
      lbl.textContent = opt.name;
      lbl.htmlFor = `opt-${this._cliName}-${opt.name}`;

      let input;
      const current =
        opt.name in this._values
          ? this._values[opt.name]
          : opt.default !== undefined && opt.default !== null
            ? opt.default
            : "";

      if (opt.type === "enum") {
        input = document.createElement("select");
        for (const ch of opt.choices || []) {
          const o = document.createElement("option");
          o.value = String(ch);
          o.textContent = String(ch);
          if (String(ch) === String(current)) o.selected = true;
          input.appendChild(o);
        }
      } else if (opt.type === "bool") {
        input = document.createElement("input");
        input.type = "checkbox";
        input.checked = Boolean(current);
      } else if (opt.type === "number") {
        input = document.createElement("input");
        input.type = "number";
        if (opt.min !== null && opt.min !== undefined) input.min = String(opt.min);
        if (opt.max !== null && opt.max !== undefined) input.max = String(opt.max);
        input.value = current !== "" ? String(current) : "";
      } else {
        input = document.createElement("input");
        input.type = "text";
        input.value = current !== undefined && current !== null ? String(current) : "";
      }
      input.id = lbl.htmlFor;
      input.className = "options-input";

      const desc = document.createElement("span");
      desc.className = "options-desc muted";
      desc.textContent = opt.description || "";

      row.appendChild(lbl);
      row.appendChild(input);
      row.appendChild(desc);
      form.appendChild(row);
      fieldMap.set(opt.name, { opt, input });
    }

    const buttons = document.createElement("div");
    buttons.className = "dialog-buttons";

    const resetBtn = document.createElement("button");
    resetBtn.type = "button";
    resetBtn.className = "options-reset";
    resetBtn.textContent = "Reset to defaults";
    resetBtn.addEventListener("click", () => {
      for (const [, { opt, input }] of fieldMap) {
        if (opt.type === "bool") input.checked = Boolean(opt.default);
        else input.value = opt.default !== undefined && opt.default !== null
          ? String(opt.default)
          : "";
      }
    });
    buttons.appendChild(resetBtn);

    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    saveBtn.textContent = "Save";
    saveBtn.className = "options-save";
    saveBtn.addEventListener("click", () => {
      const values = {};
      for (const [name, { opt, input }] of fieldMap) {
        if (opt.type === "bool") values[name] = input.checked;
        else if (opt.type === "number")
          values[name] = input.value === "" ? null : Number(input.value);
        else values[name] = input.value;
      }
      this.dispatchEvent(
        new CustomEvent("options-popover:save", {
          bubbles: true,
          detail: { cliName: this._cliName, values },
        }),
      );
      dialog.close();
    });
    buttons.appendChild(saveBtn);

    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.textContent = "Cancel";
    cancelBtn.addEventListener("click", () => {
      this.dispatchEvent(new CustomEvent("options-popover:close", { bubbles: true }));
      dialog.close();
    });
    buttons.appendChild(cancelBtn);

    form.appendChild(buttons);
    dialog.appendChild(form);
    this.appendChild(dialog);
    this._dialog = dialog;
  }
}

customElements.define("options-popover", OptionsPopover);
