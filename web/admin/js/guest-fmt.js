// Guest read-only links (docs/contracts/api.md, "Guest access"): pure helpers for the login page (token in the link),
// the System page (create / list / revoke) and the guest badge (guest.js). No DOM here: tests/admin/guest.test.mjs.

/** The link is ROOT + "admin/login.html#guest=<token>": the token rides in the fragment, never sent in a URL. */
export const GUEST_HASH_RE = /^#guest=([A-Za-z0-9_-]{16,256})$/;

/** location.hash → the token, or null. */
export function guestToken(hash) {
  const m = GUEST_HASH_RE.exec(String(hash || ""));
  return m ? m[1] : null;
}

export const MIN_TTL_S = 10 * 60;
export const MAX_TTL_S = 365 * 86400;
export const EXPIRY_CHOICES = [
  { value: "1d", label: "1 day", s: 86400 },
  { value: "7d", label: "7 days", s: 7 * 86400 },
  { value: "custom", label: "Custom…" },
];

/** Remote admin prefix on the server (docs/contracts/remote.md: <server>/fnadmin/). */
export const REMOTE_ROOT = "/fnadmin/";

/**
 * How long the new link lasts, seconds. custom: a datetime-local value ("2026-11-05T18:00", the browser's time).
 * → { s } or { error }.
 */
export function ttlSeconds(choice, customLocal, nowMs) {
  const preset = EXPIRY_CHOICES.find((c) => c.value === choice && c.s);
  if (preset) return { s: preset.s };
  if (choice !== "custom") return { error: "Choose how long the link lasts" };
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(String(customLocal || ""));
  if (!m) return { error: "Pick the date and time the link stops working" };
  const at = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]).getTime();
  const s = Math.round((at - nowMs) / 1000);
  if (!(s >= MIN_TTL_S)) return { error: "The link must last at least 10 minutes" };
  if (s > MAX_TTL_S) return { error: "The link can last at most a year" };
  return { s };
}

/** datetime-local value for a moment (the browser's time): the default custom expiry. */
export function localInputValue(ms) {
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

/**
 * Full addresses of a new link: here (the address this admin is open at), and through the server when the admin is
 * open on the van's Wi-Fi and the device knows the server's address (remote_origin: once the remote admin was used).
 */
export function guestUrls(path, { origin, root = "/", remoteOrigin = null } = {}) {
  const p = String(path || "").replace(/^\/+/, "");
  const out = [{ where: root === "/" ? "On the van's network" : "Through the server", url: origin + root + p }];
  if (root === "/" && remoteOrigin) out.push({ where: "Through the server", url: remoteOrigin + REMOTE_ROOT + p });
  return out;
}

/** "in 5 min" / "in 3 h" / "in 6 days" / "expired". */
export function fmtLeft(sec) {
  if (!(sec > 0)) return "expired";
  if (sec < 3600) return `in ${Math.max(1, Math.round(sec / 60))} min`;
  if (sec < 2 * 86400) return `in ${Math.round(sec / 3600)} h`;
  return `in ${Math.round(sec / 86400)} days`;
}

/** "never" / "just now" / "5 min ago" / "3 h ago" / "2 days ago". */
export function fmtAgo(ts, nowTs) {
  if (typeof ts !== "number" || !Number.isFinite(ts)) return "never";
  const s = nowTs - ts;
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 2 * 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}
