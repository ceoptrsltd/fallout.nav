// "Install app", always findable: the browser's own install prompt (beforeinstallprompt — caught as early as
// index.html, kept here) or, without one, the steps for this browser (install-fmt.js). The entries hide themselves
// in the installed app. sw.js stays push-only: a fetch handler is not needed to be installable.
import { h, icon } from "./dom.js";
import { modal, toast } from "./ui.js";
import { installMode, installSteps, platformOf } from "./install-fmt.js";

const subs = new Set();
let deferred = window.fnavInstallEvent || null; // caught by the inline script in index.html
let installedNow = false;

const emit = () => {
  const m = currentMode();
  for (const fn of [...subs]) {
    try { fn(m); } catch (e) { console.error("install", e); }
  }
};

window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault(); // no automatic banner: our entry shows the prompt when asked
  deferred = e;
  emit();
});
window.addEventListener("appinstalled", () => {
  deferred = null;
  installedNow = true;
  toast("ok", "fallout.nav is installed — open it from the home screen or the app list");
  emit();
});
const standaloneMq = matchMedia("(display-mode: standalone)");
if (standaloneMq.addEventListener) standaloneMq.addEventListener("change", emit);

/** Running as the installed app. */
export function isStandalone() {
  return navigator.standalone === true || standaloneMq.matches || matchMedia("(display-mode: fullscreen)").matches;
}

const platform = () => platformOf({ ua: navigator.userAgent || "", platform: navigator.platform || "", touch: navigator.maxTouchPoints || 0 });

/** installMode() for this browser now. */
export function currentMode() {
  const p = platform();
  return installMode({ standalone: isStandalone() || installedNow, hasPrompt: !!deferred, ios: p.ios, android: p.android });
}

/** fn(mode) on every change; returns an unsubscribe function. */
export function onInstallChange(fn) {
  subs.add(fn);
  return () => subs.delete(fn);
}

const part = (p) => (typeof p === "string" ? p : p.b ? h("b", null, p.b) : p.icon ? h("span.inst-ic", null, icon(p.icon)) : "");

function showSteps(mode) {
  const { title, steps, note } = installSteps(mode, platform().browser, { secure: window.isSecureContext === true });
  const body = h(".inst", null,
    h(".inst-head", null, h("span.inst-badge", null, icon("install", 26)),
      h("div", null, h("b", null, "fallout.nav as an app"), h(".dim", null, "Its own icon, full screen, no browser bars."))),
    h("ol.inst-steps", null, steps.map((s) => h("li", null, s.map(part)))),
    note ? h(".inst-note", null, note) : null);
  const m = modal({ title, body, width: 440 });
  m.el.classList.add("inst-modal");
}

/** The entry's action: the browser's prompt if we have it, else the steps. */
export async function startInstall() {
  const mode = currentMode();
  if (mode === "prompt" && deferred) {
    const e = deferred;
    deferred = null; // a prompt can be shown only once
    emit();
    try {
      await e.prompt();
      const r = await e.userChoice;
      if (r && r.outcome === "dismissed") toast("info", "Not installed — Install app stays in the menu for later");
    } catch {
      showSteps(currentMode());
    }
    return;
  }
  showSteps(mode);
}

/** An "Install app" control (tag like "button.btn.sm"); hidden in the installed app. el.dispose() unsubscribes. */
export function installEntry(tag = "button.btn", label = "Install app") {
  const el = h(tag, { type: "button", title: "Install fallout.nav as an app on this device", onclick: () => startInstall() },
    icon("install"), h("span", null, label));
  const upd = (m) => { el.hidden = m === "installed"; };
  upd(currentMode());
  el.dispose = onInstallChange(upd);
  return el;
}
