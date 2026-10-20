// UI primitives: toasts, modals, confirmations, switches.
import { h, icon, clear } from "./dom.js";

// ───── toasts ─────
export function toast(level, text, ttl) {
  const box = document.getElementById("toasts");
  if (!box) return;
  const lv = ["info", "ok", "warn", "error"].includes(level) ? level : "info";
  const t = h(".toast." + lv, null, h("div", null, text), h("button.x", { title: "Close", onclick: () => kill() }, "×"));
  let timer;
  const kill = () => { clearTimeout(timer); t.remove(); };
  box.append(t);
  while (box.children.length > 5) box.firstChild.remove();
  timer = setTimeout(kill, ttl ?? (lv === "error" ? 9000 : lv === "warn" ? 7000 : 4500));
  return kill;
}

// ───── modal dialogs ─────
const stack = [];
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && stack.length) {
    const top = stack[stack.length - 1];
    if (top.dismissable) top.close();
  }
});

/**
 * modal({title, body, foot, width, dismissable, onClose}) -> {el, body, close}
 * body/foot: Node or array of Nodes.
 */
export function modal({ title, body, foot, width, dismissable = true, onClose, headExtra } = {}) {
  const bodyEl = h(".modal-body", null, body);
  const m = h(
    ".modal",
    { role: "dialog", "aria-modal": "true", style: width ? { width: width + "px" } : null },
    h(".modal-head", null, h("h2", null, title || ""), headExtra, h("button.btn.icon.ghost", { title: "Close (Esc)", onclick: () => api.close() }, icon("x"))),
    bodyEl,
    foot ? h(".modal-foot", null, foot) : null,
  );
  const ov = h(".overlay", {
    onmousedown: (e) => { if (e.target === ov && dismissable) api.close(); },
  }, m);
  const prevFocus = document.activeElement;
  const api = {
    el: m, body: bodyEl, overlay: ov, dismissable,
    close() {
      const i = stack.indexOf(api);
      if (i < 0) return;
      stack.splice(i, 1);
      ov.remove();
      try { prevFocus && prevFocus.focus && prevFocus.focus(); } catch { /* */ }
      if (onClose) onClose();
    },
  };
  stack.push(api);
  document.body.append(ov);
  return api;
}

/** Confirmation dialog. Returns Promise<boolean>. */
export function confirmDialog({ title = "Please confirm", text, okText = "Yes", cancelText = "Cancel", danger = false } = {}) {
  return new Promise((resolve) => {
    let result = false;
    const ok = h("button.btn" + (danger ? ".danger.solid" : ".primary"), { onclick: () => { result = true; m.close(); } }, okText);
    const cancel = h("button.btn", { onclick: () => m.close() }, cancelText);
    const m = modal({ title, body: h("div", { style: { maxWidth: "460px" } }, text), foot: [cancel, ok], width: 480, onClose: () => resolve(result) });
    (danger ? cancel : ok).focus();
  });
}

// ───── form elements ─────

/** Toggle switch. */
export function switchEl({ checked, label, onchange }) {
  const input = h("input", { type: "checkbox", checked: !!checked, onchange: () => onchange && onchange(input.checked) });
  const el = h("label.switch", null, input, h("span.tr"), label ? h("span", null, label) : null);
  el.input = input;
  return el;
}

/** Segmented control. options: [{value,label}] */
export function segmented({ name, options, value, onchange }) {
  const el = h(".seg", { role: "radiogroup" });
  const labels = new Map();
  const set = (v) => {
    for (const [val, lab] of labels) {
      lab.classList.toggle("sel", val === v);
      lab.querySelector("input").checked = val === v;
    }
  };
  for (const o of options) {
    const input = h("input", { type: "radio", name, value: String(o.value) });
    const lab = h("label", null, input, o.label);
    input.addEventListener("change", () => {
      set(o.value);
      onchange && onchange(o.value);
    });
    labels.set(o.value, lab);
    el.append(lab);
  }
  set(value);
  el.set = set;
  return el;
}

export function badge(text, kind) {
  return h("span.badge" + (kind ? "." + kind : ""), null, text);
}

export function banner(kind, content, iconName) {
  return h(".banner" + (kind ? "." + kind : ""), null, icon(iconName || (kind === "bad" || kind === "warn" ? "warn" : "info")), h("div", null, content));
}

export function emptyState(text) {
  return h(".card.dim", { style: { textAlign: "center", padding: "30px" } }, text);
}

/** Simple card wrapper. */
export function card(title, ...content) {
  return h(".card", null, title ? h("h2", null, title) : null, ...content);
}

/** Periodic polling while the page is alive. Returns stop(). */
export function poll(fn, ms, { immediate = true } = {}) {
  let stopped = false;
  let timer = null;
  let busy = false;
  const tick = async () => {
    if (stopped) return;
    if (!busy && !document.hidden) {
      busy = true;
      try { await fn(); } catch (e) { /* fn reports errors itself */ }
      busy = false;
    }
    if (!stopped) timer = setTimeout(tick, ms);
  };
  if (immediate) tick(); else timer = setTimeout(tick, ms);
  return () => { stopped = true; clearTimeout(timer); };
}

export { clear };
