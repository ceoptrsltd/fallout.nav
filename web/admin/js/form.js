// Settings form: tracks changed fields, sends PATCH with only those,
// shows 422 errors next to fields and a "saved" indicator.
import { h, deepEqual, clone, getPath, setPath, isNum } from "./dom.js";
import { api, ApiError, errText } from "./api.js";
import { applySettings, on, store } from "./store.js";
import { segmented, switchEl, toast } from "./ui.js";

let uid = 0;

export class SettingsForm {
  /**
   * @param {object} opts
   * @param {(patch:object)=>Promise<boolean>} [opts.beforeSave] - return false to cancel
   * @param {()=>void} [opts.onSaved]
   */
  constructor(opts = {}) {
    this.opts = opts;
    this.fields = new Map();
    this.base = store.settings || {};
    this.saving = false;
    this.savedAt = 0;
    this.generalError = null;
    this.bar = this._buildBar();
    this._off = on("settings", (s) => this.sync(s));
    this.refreshBar();
  }

  destroy() {
    this._off && this._off();
    clearTimeout(this._savedTimer);
  }

  // ── field registration ──
  /**
   * spec: {path,label,hint,help,unit,type,options,min,max,step,nullable,build,wide}
   * type: number | range | text | password | switch | select | seg | multi | custom
   */
  add(spec) {
    const f = {
      spec,
      path: spec.path,
      value: clone(getPath(this.base, spec.path)),
      name: "f" + ++uid,
    };
    f.change = (v) => {
      f.value = v;
      this._clearErr(f);
      this.refreshBar();
    };
    f.widget = this._widget(f);
    f.err = h(".err", { hidden: true });
    f.unitEl = spec.unit || spec.unitFn ? h("span.unit", null, spec.unit || "") : null;
    // dynamic unit label (e.g. "= 7.2 km/h")
    if (spec.unitFn) {
      const upd0 = f.widget.update;
      const refresh = () => { f.unitEl.textContent = spec.unitFn(f.value) || ""; };
      f.widget.update = (v) => { upd0(v); refresh(); };
      const ch0 = f.change;
      f.change = (v) => { ch0(v); refresh(); };
    }
    f.row = spec.bare
      ? h("div.bare", null, f.widget.el, f.err)
      : h(
          ".field",
          null,
          h(".lab", null, h("b", null, spec.label), spec.hint ? h("span", null, spec.hint) : null),
          h(".ctl", null, f.widget.el, f.unitEl),
          f.err,
          spec.help ? h(".help", null, spec.help) : null,
        );
    f.widget.update(f.value);
    this.fields.set(spec.path, f);
    this._markDirty(f);
    return f.row;
  }

  /** Set a value programmatically (for custom widgets). */
  set(path, value) {
    const f = this.fields.get(path);
    if (!f) return;
    f.value = value;
    f.widget.update(value);
    this._clearErr(f);
    this.refreshBar();
  }
  get(path) {
    return this.fields.get(path)?.value;
  }

  _widget(f) {
    const s = f.spec;
    switch (s.type || "text") {
      case "number": {
        const inp = h("input", { type: "number", min: s.min, max: s.max, step: s.step ?? "any", inputmode: "decimal" });
        inp.addEventListener("input", () => {
          const raw = inp.value.trim();
          let v;
          if (raw === "") v = s.nullable ? null : NaN;
          else v = Number(raw);
          inp.classList.toggle("invalid", Number.isNaN(v));
          f.change(v);
        });
        return { el: inp, update: (v) => { inp.value = v === null || v === undefined || Number.isNaN(v) ? "" : String(v); inp.classList.remove("invalid"); } };
      }
      case "range": {
        const rng = h("input", { type: "range", min: s.min, max: s.max, step: s.step ?? "any", style: { width: "220px" } });
        const num = h("input", { type: "number", min: s.min, max: s.max, step: s.step ?? "any" });
        const sync = (v) => { rng.value = v; num.value = v; };
        rng.addEventListener("input", () => { num.value = rng.value; f.change(Number(rng.value)); });
        num.addEventListener("input", () => {
          const v = Number(num.value);
          if (num.value !== "" && Number.isFinite(v)) { rng.value = v; f.change(v); } else f.change(NaN);
        });
        return { el: h("span", { style: { display: "inline-flex", gap: "12px", alignItems: "center" } }, rng, num), update: sync };
      }
      case "text":
      case "password": {
        const inp = h("input", { type: s.type === "password" ? "password" : "text", autocomplete: "off", spellcheck: "false", placeholder: s.placeholder });
        inp.addEventListener("input", () => f.change(inp.value));
        const kids = [inp];
        if (s.type === "password") {
          const eye = h("button.btn.sm", { type: "button", onclick: () => { inp.type = inp.type === "password" ? "text" : "password"; eye.textContent = inp.type === "password" ? "Show" : "Hide"; } }, "Show");
          kids.push(eye);
        }
        return { el: h("span", { style: { display: "inline-flex", gap: "8px", alignItems: "center", flexWrap: "wrap" } }, kids), update: (v) => { inp.value = v ?? ""; } };
      }
      case "switch": {
        const sw = switchEl({ checked: !!f.value, label: s.switchLabel, onchange: (v) => f.change(v) });
        return { el: sw, update: (v) => { sw.input.checked = !!v; } };
      }
      case "select": {
        const sel = h("select", null, s.options.map((o, i) => h("option", { value: String(i) }, o.label)));
        sel.addEventListener("change", () => f.change(s.options[Number(sel.value)].value));
        return {
          el: sel,
          update: (v) => {
            const i = s.options.findIndex((o) => o.value === v);
            if (i < 0) {
              // value not in the list: add it as is
              s.options.push({ value: v, label: String(v) });
              sel.append(h("option", { value: String(s.options.length - 1) }, String(v)));
              sel.value = String(s.options.length - 1);
            } else sel.value = String(i);
          },
        };
      }
      case "seg": {
        const seg = segmented({ name: f.name, options: s.options, value: f.value, onchange: (v) => f.change(v) });
        return { el: seg, update: (v) => seg.set(v) };
      }
      case "multi": {
        const boxes = s.options.map((o) => {
          const sw = switchEl({ checked: false, label: o.label, onchange: () => {
            f.change(s.options.filter((_, i) => boxes[i].input.checked).map((x) => x.value));
          } });
          return sw;
        });
        return {
          el: h("span", { style: { display: "inline-flex", gap: "18px", flexWrap: "wrap" } }, boxes),
          update: (v) => s.options.forEach((o, i) => { boxes[i].input.checked = Array.isArray(v) && v.includes(o.value); }),
        };
      }
      case "custom":
        return s.build(f);
      default:
        throw new Error("unknown field type " + s.type);
    }
  }

  // ── state ──
  isDirty(f) {
    return !deepEqual(f.value ?? null, getPath(this.base, f.path) ?? null);
  }
  dirtyFields() {
    return [...this.fields.values()].filter((f) => this.isDirty(f));
  }
  _markDirty(f) {
    f.row.classList.toggle("dirty", this.isDirty(f));
  }

  /** New settings from the server: fields edited by the user are left alone. */
  sync(settings) {
    if (!settings) return;
    const old = this.base;
    this.base = settings;
    for (const f of this.fields.values()) {
      const wasDirty = !deepEqual(f.value ?? null, getPath(old, f.path) ?? null);
      if (!wasDirty) {
        f.value = clone(getPath(settings, f.path));
        f.widget.update(f.value);
      }
    }
    this.refreshBar();
  }

  revert() {
    for (const f of this.fields.values()) {
      f.value = clone(getPath(this.base, f.path));
      f.widget.update(f.value);
      this._clearErr(f);
    }
    this.generalError = null;
    this.refreshBar();
  }

  _clearErr(f) {
    f.err.hidden = true;
    f.err.textContent = "";
    this.generalError = null;
  }
  _setErr(f, text) {
    f.err.hidden = false;
    f.err.textContent = text;
    f.row.scrollIntoView?.({ block: "nearest" });
  }

  _validate() {
    let ok = true;
    for (const f of this.dirtyFields()) {
      const s = f.spec;
      if (s.type === "number" || s.type === "range") {
        const v = f.value;
        if (v === null && s.nullable) continue;
        if (!isNum(v)) { this._setErr(f, "Enter a number"); ok = false; continue; }
        if (s.min !== undefined && v < s.min) { this._setErr(f, `Minimum ${s.min}`); ok = false; }
        else if (s.max !== undefined && v > s.max) { this._setErr(f, `Maximum ${s.max}`); ok = false; }
      }
      if (s.validate) {
        const msg = s.validate(f.value);
        if (msg) { this._setErr(f, msg); ok = false; }
      }
    }
    return ok;
  }

  buildPatch() {
    const patch = {};
    for (const f of this.dirtyFields()) setPath(patch, f.path, f.value);
    return patch;
  }

  async save() {
    if (this.saving) return;
    const dirty = this.dirtyFields();
    if (!dirty.length) return;
    this.generalError = null;
    if (!this._validate()) { this.generalError = "Check the highlighted fields"; this.refreshBar(); return; }
    const patch = this.buildPatch();
    if (this.opts.beforeSave && !(await this.opts.beforeSave(patch, dirty))) return;
    this.saving = true;
    this.refreshBar();
    try {
      const res = await api.patch("/api/settings", patch);
      this.base = res && res.display ? res : store.settings;
      if (res && res.display) applySettings(res);
      for (const f of this.fields.values()) {
        if (dirty.includes(f)) { f.value = clone(getPath(this.base, f.path)); f.widget.update(f.value); this._clearErr(f); }
      }
      this.savedAt = Date.now();
      clearTimeout(this._savedTimer);
      this._savedTimer = setTimeout(() => this.refreshBar(), 4000);
      this.opts.onSaved && this.opts.onSaved(res);
    } catch (e) {
      if (e instanceof ApiError && e.status === 422) {
        const { fields, general } = e.fieldErrors();
        const unmatched = [];
        for (const [path, msg] of Object.entries(fields)) {
          // find the field with the exact path or a parent (e.g. zoom_table.1.zoom -> nav.zoom_table)
          let f = this.fields.get(path);
          if (!f) {
            f = [...this.fields.values()].find((x) => path.startsWith(x.path + ".") || x.path.startsWith(path + "."));
          }
          if (f) this._setErr(f, msg); else unmatched.push(`${path}: ${msg}`);
        }
        this.generalError = [general, ...unmatched].filter(Boolean).join("; ") || (Object.keys(fields).length ? "Check the highlighted fields" : "Settings rejected (422)");
      } else {
        this.generalError = errText(e);
        toast("error", "Could not save: " + errText(e));
      }
    } finally {
      this.saving = false;
      this.refreshBar();
    }
  }

  // ── save bar ──
  _buildBar() {
    this.msg = h("span.msg");
    this.btnRevert = h("button.btn", { type: "button", onclick: () => this.revert() }, "Revert");
    this.btnSave = h("button.btn.primary", { type: "button", onclick: () => this.save() }, "Save");
    return h(".savebar", null, this.msg, this.btnRevert, this.btnSave);
  }

  refreshBar() {
    for (const f of this.fields.values()) this._markDirty(f);
    const n = this.dirtyFields().length;
    this.bar.classList.toggle("dirty", n > 0);
    this.btnSave.disabled = n === 0 || this.saving;
    this.btnRevert.disabled = n === 0 || this.saving;
    this.btnSave.textContent = this.saving ? "Saving…" : "Save";
    this.msg.className = "msg";
    if (this.generalError) {
      this.msg.classList.add("error");
      this.msg.textContent = "✕ " + this.generalError;
    } else if (n) {
      this.msg.textContent = `${n} unsaved change${n > 1 ? "s" : ""}. Press "Save" to apply.`;
    } else if (Date.now() - this.savedAt < 4000) {
      this.msg.classList.add("saved");
      this.msg.textContent = "✓ Saved";
    } else {
      this.msg.textContent = "All changes saved";
    }
  }
}

/** Card with a section title. */
export function section(title, ...content) {
  return h(".card", null, h("h2", null, title), ...content);
}
