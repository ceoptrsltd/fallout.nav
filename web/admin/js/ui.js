// UI primitives: toasts, modals (with the Back button), confirmations, switches.
import { h, icon, clear } from "./dom.js";
import { entryDepth, dialogState, popPlan } from "./hist-fmt.js";

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
// A dismissable dialog is also a history entry (same URL, hist-fmt.js): Back (Android, the browser) closes the dialog
// instead of leaving the page — or closing the installed app; a route change closes them all (closeModals, from
// shell.js route()); a page link inside a dialog and navigate() replace the dialog's entry with that page, so Back
// returns to where the dialog was opened from. Closing it here (✕, Esc, a button) takes its entry back off.
const stack = [];
const LOAD_ID = Date.now().toString(36) + Math.random().toString(36).slice(2, 6); // entries of earlier loads are dead
let seq = 0;
let backing = false; // our own history.back() is on its way: dialogs opened meanwhile get their entry after it lands
let backTimer = null;

function pushEntry(m) {
  const st = dialogState(LOAD_ID + ":" + ++seq, history.state);
  try {
    history.pushState(st, "");
    m.hid = st.fnavModal;
    m.depth = st.fnavDepth;
  } catch { /* no history (sandboxed): the dialog just has none */ }
}
function flushEntries() {
  backing = false;
  clearTimeout(backTimer);
  for (const m of stack) if (m.track && !m.hid) pushEntry(m);
}
function goBack(n = 1) {
  if (n < 1) return;
  backing = true;
  clearTimeout(backTimer);
  backTimer = setTimeout(flushEntries, 700); // no popstate came (nothing to go back to)
  history.go(-n);
}

window.addEventListener("popstate", () => {
  const plan = popPlan(stack.filter((m) => m.hid).map((m) => ({ id: m.hid, depth: m.depth })), history.state);
  for (const id of plan.close) {
    const m = stack.find((x) => x.hid === id);
    if (m) m.shut("history");
  }
  if (plan.dead) goBack(); // nobody owns this dialog entry any more: step over it
  else if (backing) flushEntries();
});
// reloaded with a dialog open ("Reload app" in More): its entries are dead — back to the page under them
goBack(entryDepth(history.state));

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && stack.length) {
    const top = stack[stack.length - 1];
    if (top.dismissable) top.close();
  }
});

// a page link inside a dialog (the More sheet): the dialog's entry becomes that page
document.addEventListener("click", (e) => {
  if (!stack.length || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  const a = e.target instanceof Element ? e.target.closest("a[href^='#/']") : null;
  if (!a || a.target || !a.closest(".overlay")) return;
  e.preventDefault();
  navigate(a.getAttribute("href"));
}, true);

/** Close every dialog: a route change (their history entries are under the new page or replaced by it). */
export function closeModals() {
  for (const m of [...stack].reverse()) m.shut("route");
}

/**
 * Open a page ("#/alarm") from code — a notification click: the open dialogs close (the recording player must not
 * keep playing over the new page); a dialog entry that is the current one is replaced by the page.
 */
export function navigate(hash) {
  const d = entryDepth(history.state);
  if (hash === location.hash) {
    closeModals();
    goBack(d); // the same page: just without the dialogs
    return;
  }
  closeModals();
  if (d) location.replace(hash);
  else location.hash = hash;
}

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
    track: dismissable, hid: null, depth: 0, // its history entry (non-dismissable dialogs have none)
    close() { api.shut("ui"); },
    /** how: "ui" — closed here; "history" — by Back; "route" — by a route change. */
    shut(how) {
      const i = stack.indexOf(api);
      if (i < 0) return;
      stack.splice(i, 1);
      ov.remove();
      // back to where the dialog was opened from, without scrolling the page to it
      try { prevFocus && prevFocus.focus && prevFocus.focus({ preventScroll: true }); } catch { /* */ }
      // its own entry goes too — only when it is the current one (a dialog above it: left behind, stepped over later)
      if (how === "ui" && api.hid && history.state && history.state.fnavModal === api.hid) goBack();
      if (onClose) onClose();
    },
  };
  stack.push(api);
  document.body.append(ov);
  if (api.track && !backing) pushEntry(api);
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
