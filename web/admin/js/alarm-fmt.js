// Alarm page and push notifications: pure helpers (docs/contracts/alarm.md §4, §7).
// Tests: tests/admin/alarm.test.mjs.

// ───── impact threshold: log-scale slider ─────

export const IMPACT_MIN = 0.02; // settings.alarm.impact_g limits, g
export const IMPACT_MAX = 2;
export const SLIDER_STEPS = 1000;
// vibration meter: below the lowest threshold too, so a quiet sensor still moves a little
export const METER_MIN = 0.005;
export const METER_MAX = 2;

/** Position 0…1 of v on a log scale lo…hi (clamped; no value → 0). */
export function logPos(v, lo, hi) {
  if (!(typeof v === "number" && Number.isFinite(v)) || v <= lo) return 0;
  if (v >= hi) return 1;
  return Math.log(v / lo) / Math.log(hi / lo);
}

/** Value at position 0…1 of a log scale lo…hi. */
export function logVal(p, lo, hi) {
  const c = Math.min(1, Math.max(0, Number(p) || 0));
  return lo * (hi / lo) ** c;
}

/** Two significant digits — the natural step of a log slider: 0.0234 → 0.023, 0.153 → 0.15, 1.27 → 1.3. */
export function roundG(g) {
  if (!(typeof g === "number" && Number.isFinite(g)) || g <= 0) return g;
  return Number(g.toPrecision(2));
}

// impact threshold from the vibration meter: a click / drag position and the − / + steps
export const IMPACT_STEPS = [0.02, 0.025, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.1, 0.12, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5,
  0.6, 0.8, 1, 1.2, 1.5, 2];

/** Meter position 0…1 → impact threshold (two significant digits, within the limits). */
export function thresholdAt(p) {
  return Math.min(IMPACT_MAX, Math.max(IMPACT_MIN, roundG(logVal(p, METER_MIN, METER_MAX))));
}

/** Next / previous step from g (dir +1 / −1); off-ladder values go to the nearest step in that direction. */
export function stepThreshold(g, dir) {
  const v = typeof g === "number" && Number.isFinite(g) ? g : 0.15;
  if (dir > 0) return IMPACT_STEPS.find((x) => x > v + 1e-9) ?? IMPACT_MAX;
  return [...IMPACT_STEPS].reverse().find((x) => x < v - 1e-9) ?? IMPACT_MIN;
}

/** impact_g → slider position 0…steps. */
export function impactToSlider(g, steps = SLIDER_STEPS) {
  return Math.round(logPos(Math.min(IMPACT_MAX, Math.max(IMPACT_MIN, g)), IMPACT_MIN, IMPACT_MAX) * steps);
}

/** Slider position → impact_g (two significant digits, within the limits). */
export function sliderToImpact(pos, steps = SLIDER_STEPS) {
  const g = roundG(logVal((Number(pos) || 0) / steps, IMPACT_MIN, IMPACT_MAX));
  return Math.min(IMPACT_MAX, Math.max(IMPACT_MIN, g));
}

/** Tick marks of a log scale: [{v, p, label}]. */
export function logTicks(values, lo, hi) {
  return values.filter((v) => v >= lo && v <= hi).map((v) => ({ v, p: logPos(v, lo, hi), label: String(v) }));
}

/** "0.012 g", "0.42 g", "—". */
export function fmtG(g) {
  if (!(typeof g === "number" && Number.isFinite(g))) return "—";
  return (g < 0.1 ? g.toFixed(3) : g.toFixed(2)) + " g";
}

/**
 * Peak hold of the vibration meter: a higher value replaces the peak at once, otherwise the peak is held for
 * holdS seconds and then follows the current value. prev: {g, at} | null; now in seconds.
 */
export function peakHold(prev, g, now, holdS = 30) {
  if (!(typeof g === "number" && Number.isFinite(g))) return prev;
  if (!prev || g >= prev.g || now - prev.at > holdS) return { g, at: now };
  return prev;
}

// ───── state ─────

const STATES = {
  off: { text: "Off", kind: "" },
  arming: { text: "Arming", kind: "warn" },
  armed: { text: "Armed", kind: "ok" },
  triggered: { text: "Triggered", kind: "bad" },
};

/** Alarm state wording for badges: {text, kind}. */
export function alarmStateText(state) {
  return STATES[state] || { text: state ? String(state) : "—", kind: "" };
}

/** Seconds left of the arm countdown: arm_in_s received at receivedMs, now nowMs (ms) — counts down between polls. */
export function armLeft(armInS, receivedMs, nowMs) {
  if (!(typeof armInS === "number" && Number.isFinite(armInS))) return null;
  return Math.max(0, Math.ceil(armInS - Math.max(0, nowMs - receivedMs) / 1000));
}

/** Server link of the device: {text, kind}. */
export function relayText(relay) {
  if (!relay || !relay.configured) return { text: "not set up — notifications cannot be sent", kind: "warn" };
  const n = relay.pending || 0;
  if (n > 0) return { text: `${n} event${n === 1 ? "" : "s"} waiting to be sent`, kind: "warn" };
  return { text: "up to date", kind: "ok" };
}

// ───── hazard lights (docs/contracts/alarm.md, «Аварийка») ─────

/** Status line from AlarmStatus.hazard {enabled, active, error}: {text, kind}, or null (device without it). */
export function hazardStatus(hz) {
  if (!hz || typeof hz !== "object") return null;
  if (hz.error) return { text: String(hz.error), kind: "bad" };
  if (hz.active) return { text: "Flashing now", kind: "warn" };
  if (hz.enabled) return { text: "Ready — they flash when the alarm goes off", kind: "ok" };
  return { text: "Off — the alarm does not use them", kind: "" };
}

/** Camera watch (fnav-watch) status line: {text, kind} or null when the device does not report it. */
export function visionStatus(v) {
  if (!v || typeof v !== "object") return null;
  if (!v.enabled) return { text: "Off — the camera does not guard the van", kind: "" };
  if (v.error) return { text: String(v.error), kind: "bad" };
  if (!v.available) return { text: "The vision service is not running", kind: "bad" };
  if (v.watching) {
    const n = Number.isFinite(v.people) ? v.people : 0;
    return { text: n ? `Watching — ${n === 1 ? "a person" : n + " people"} in view` : "Watching", kind: n ? "warn" : "ok" };
  }
  return { text: "Ready — starts watching when the alarm is armed", kind: "ok" };
}

/** The button that stops the siren and the hazard lights (the alarm stays armed); short enough for a phone. */
export function silenceLabel({ siren, hazard, hazardEnabled }) {
  if (hazard && !siren) return "Stop the lights";
  if (hazard || hazardEnabled) return "Stop siren & lights";
  return "Stop the siren";
}

const KINDS = {
  impact: { label: "Impact", icon: "warn", kind: "bad" },
  tilt: { label: "Tilt", icon: "level", kind: "warn" },
  move: { label: "Moving", icon: "pin", kind: "warn" },
  approach: { label: "Person approaching", icon: "cam", kind: "bad" },
  face: { label: "Face at the windshield", icon: "cam", kind: "bad" },
  loiter: { label: "Lingering at the van", icon: "cam", kind: "bad" },
  test: { label: "Test", icon: "info", kind: "" },
};

/** Event kind for the log: {label, icon, kind}. */
export function kindInfo(kind) {
  return KINDS[kind] || { label: kind ? String(kind) : "Event", icon: "info", kind: "" };
}

/** Change marker of the event list (re-render the log only when it changed). */
export function eventsKey(events) {
  const list = Array.isArray(events) ? events : [];
  // the snapshots and the kept recordings (front, rear) are attached a moment after the event: they change the key too
  const extras = list.map((e) => {
    const d = (e && e.data) || {};
    return (d.snapshot ? "s" : "") + (d.segment ? "r" : "") + (d.snapshot_rear ? "S" : "") + (d.segment_rear ? "R" : "");
  }).join(",");
  return `${list.length}:${list.length ? list[0].id : ""}:${list.length ? list[list.length - 1].id : ""}:${extras}`;
}

/** Link of an event other than the alarm page itself (e.g. "#/tracking" for move), or null. */
export function eventLink(ev) {
  const u = ev && typeof ev.url === "string" ? ev.url.trim() : "";
  if (!u || u === "#/alarm" || !/^#\/[\w-]+/.test(u)) return null;
  const id = u.match(/^#\/([\w-]+)/)[1];
  return { href: u, text: id === "tracking" ? "Show on the map" : "Open " + id };
}

// ───── push notifications: device, mode, keys, errors ─────

/** iPhone / iPad (iPadOS reports itself as a Mac with a touch screen). */
export function isIos({ ua = "", platform = "", touch = 0 } = {}) {
  return /iPhone|iPad|iPod/.test(ua) || (platform === "MacIntel" && touch > 1);
}

/** Device name for the subscription list: "iPhone · Safari", "Android · Chrome", "Windows · Edge". */
export function deviceName({ ua = "", platform = "", touch = 0 } = {}) {
  const ios = isIos({ ua, platform, touch });
  const os = /iPad/.test(ua) || (ios && !/iPhone|iPod/.test(ua)) ? "iPad"
    : /iPhone|iPod/.test(ua) ? "iPhone"
    : /Android/.test(ua) ? "Android"
    : /CrOS/.test(ua) ? "ChromeOS"
    : /Windows/.test(ua) ? "Windows"
    : /Mac OS X|Macintosh/.test(ua) ? "Mac"
    : /Linux/.test(ua) ? "Linux"
    : "Device";
  const br = /EdgA?\/|EdgiOS\/|Edg\//.test(ua) ? "Edge"
    : /OPR\/|OPiOS\//.test(ua) ? "Opera"
    : /SamsungBrowser\//.test(ua) ? "Samsung Internet"
    : /YaBrowser\//.test(ua) ? "Yandex"
    : /Firefox\/|FxiOS\//.test(ua) ? "Firefox"
    : /Chrome\/|CriOS\//.test(ua) ? "Chrome"
    : /Safari\//.test(ua) || ios || os === "Mac" ? "Safari"
    : "Browser";
  return `${os} · ${br}`;
}

/**
 * What the notifications card can offer here:
 *   "insecure"     plain http (the device itself): no service worker / Push API — use the remote admin (https);
 *   "ios-install"  iPhone/iPad in a Safari tab: push only for the Home Screen app;
 *   "unsupported"  no service worker / PushManager / Notification;
 *   "ok"           can subscribe.
 */
export function pushMode({ secure, sw, push, notification, ios, standalone }) {
  if (!secure) return "insecure";
  if (ios && !standalone) return "ios-install";
  if (!sw || !push || !notification) return "unsupported";
  return "ok";
}

/** base64url → bytes (the VAPID public key for pushManager.subscribe). */
export function b64uToBytes(s) {
  const b = String(s || "").trim().replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(b + "=".repeat((4 - (b.length % 4)) % 4));
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
}

/** bytes (ArrayBuffer / view) → base64url without padding. */
export function bytesToB64u(buf) {
  if (!buf) return "";
  const u = buf instanceof Uint8Array ? buf : ArrayBuffer.isView(buf) ? new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength) : new Uint8Array(buf);
  let s = "";
  for (const x of u) s += String.fromCharCode(x);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** Readable text of a push error (ApiError from api.js or a DOMException from the browser). */
export function pushErrorText(e) {
  if (!e) return "Unknown error";
  const msg = String((e && e.message) || e);
  if (typeof e.status === "number") {
    if (e.status === 0) return "No connection to the server";
    if (e.status === 404 && /subscription/i.test(msg)) return "This device is no longer subscribed on the server — enable notifications again.";
    if (e.status === 404 || e.status === 405) return "The server does not handle notifications yet — update fnav-relay on the server.";
    if (e.status === 503) return "Notifications are not available on the server" + (msg && !/^Error \d+$/.test(msg) ? ": " + msg : ".");
    if (e.status === 401) return "Login required";
    return msg || "Error " + e.status;
  }
  switch (e.name) {
    case "NotAllowedError":
      return "Notifications are blocked for this site. Allow them in the browser's site settings and try again.";
    case "AbortError":
      return "The browser could not register with its push service" + (msg ? ": " + msg : "") + ".";
    case "InvalidStateError":
    case "InvalidAccessError":
      return "The browser rejected the subscription" + (msg ? ": " + msg : "") + ".";
    default:
      return msg;
  }
}

/** Time from the push API (unix seconds, ms or an ISO string) → unix seconds, or null. */
export function toUnix(v) {
  if (typeof v === "number" && Number.isFinite(v)) return v > 1e11 ? v / 1000 : v;
  if (typeof v === "string" && v) {
    const t = Date.parse(v);
    return Number.isFinite(t) ? t / 1000 : null;
  }
  return null;
}
