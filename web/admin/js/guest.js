// Guest session (read only, docs/contracts/api.md "Guest access"): a "Guest — read only" badge in a banner above every
// page, and the controls that change something are disabled. The server refuses every change for a guest anyway
// (403 "Read-only guest access"); this only makes it visible. Loaded by index.html next to shell.js.
import { appUrl } from "./api.js";
import { h } from "./dom.js";
import { banner } from "./ui.js";

export const GUEST_LABEL = "Guest — read only";
const DENIED = "Read-only guest access";
// buttons that act (save, arm, delete, connect…), password fields; view controls (tabs, filters, Play) stay enabled
const OFF = ".btn.primary, .btn.danger, button[type=submit], input[type=password], .savebar button";

/** The session if it is a guest one, otherwise null. One request per page load (shared by every importer). */
export const guestSession = (async () => {
  try {
    const r = await fetch(appUrl("api/session"), { credentials: "same-origin", cache: "no-store" });
    const s = r.ok ? await r.json() : null;
    return s && s.authenticated && s.guest ? s : null;
  } catch {
    return null;
  }
})();

function disableIn(root) {
  if (!root || root.nodeType !== 1) return;
  const els = root.matches(OFF) ? [root] : [];
  els.push(...root.querySelectorAll(OFF));
  for (const el of els) {
    if (!el.disabled) el.disabled = true;
    if (!el.dataset.guestOff) {
      el.dataset.guestOff = "1";
      el.title = DENIED;
    }
  }
}

function untilText(s) {
  if (typeof s.guest_expires !== "number") return "";
  return " Access ends " + new Date(s.guest_expires * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) + ".";
}

function apply(s) {
  document.body.classList.add("fnav-guest");
  document.head.append(h("style", null, `
.guest-badge { display: inline-block; margin-right: 8px; padding: 0 9px; white-space: nowrap; border: 1px solid var(--amber);
  border-radius: 999px; color: var(--amber); font-size: 12px; font-weight: 700; letter-spacing: 0.03em; }
body.fnav-guest .savebar { display: none !important; }
body.fnav-guest [data-guest-off] { opacity: 0.45; cursor: not-allowed; }`));
  // above every page (the view scrolls under it); the status bar has no room left on tablets and phones
  const view = document.getElementById("view");
  if (view) {
    const b = banner("warn", [h("span.guest-badge", null, GUEST_LABEL),
      "You can look at everything, but nothing can be changed." + untilText(s)], "lock");
    b.classList.add("guest-banner");
    view.before(b);
  }
  disableIn(document.body);
  new MutationObserver((muts) => {
    for (const m of muts) {
      if (m.type === "attributes") { if (m.target.matches?.(OFF) && !m.target.disabled) m.target.disabled = true; continue; }
      for (const n of m.addedNodes) disableIn(n);
    }
  }).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["disabled"] });
}

guestSession.then((s) => { if (s) apply(s); });
