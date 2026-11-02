// New version of the admin: pure helpers (no DOM). Tests: tests/admin/version.test.mjs.
// The deploy writes web/admin/version.json (tools/web-version.sh) into both copies of the UI — the device's and the
// server's (/fnadmin/). Modules loaded at startup (dom.js, api.js, alarm-fmt.js, …) stay in the page until it reloads,
// pages are loaded later from the server: after a deploy an old page with a new page module breaks. shell.js reads the
// marker at startup and again later; a changed "version" → a banner with Reload.

export const VERSION_CHECK_MS = 5 * 60 * 1000; // re-check while the app is open (and when it comes back to the screen)
export const VERSION_GAP_MS = 15 * 1000; // not more often (visibilitychange comes in bursts when switching apps)

/** "version" of a version.json body; null without one. */
export function versionOf(j) {
  const v = j && typeof j === "object" ? j.version : null;
  if (typeof v === "number" && Number.isFinite(v)) return String(v);
  return typeof v === "string" && v.trim() ? v.trim() : null;
}

/**
 * One check. base: the version this page started with — undefined: not known yet, null: no marker on this server
 * (dev, the mock) — the check is off, a string — known. res: {status (HTTP; 0 — no connection), version (versionOf)}.
 * Returns {base, changed}: changed — a deploy happened since the page loaded.
 */
export function versionStep(base, res) {
  if (base === null) return { base: null, changed: false };
  const status = res && Number.isFinite(res.status) ? res.status : 0;
  const v = res && typeof res.version === "string" && res.version ? res.version : null;
  if (status >= 200 && status < 300 && v) {
    if (base === undefined) return { base: v, changed: false };
    return { base, changed: v !== base };
  }
  // no marker (404, a body without "version"): off if it was never there; a deploy in progress — try again later
  const missing = status === 404 || (status >= 200 && status < 300);
  if (missing && base === undefined) return { base: null, changed: false };
  // no connection, 5xx: the next check decides
  return { base, changed: false };
}

/** Time for a periodic check: the page is visible and the last one was long enough ago (force: right now). */
export function versionDue(lastAt, now, { hidden = false, force = false } = {}) {
  if (force) return true;
  if (hidden) return false;
  return !Number.isFinite(lastAt) || now - lastAt >= VERSION_GAP_MS || now < lastAt;
}
