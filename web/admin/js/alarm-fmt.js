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

/** Camera watch (fnav-watch) status line: {text, kind} or null when the device does not report it.
 *  A problem with the rear camera only (v.rear.error, e.g. no frames) is added to the front's line as a warning. */
export function visionStatus(v) {
  const st = visionStatusFront(v);
  const r = v && typeof v === "object" ? v.rear : null;
  if (!st || !r || typeof r !== "object" || !r.error || !v.enabled || !v.available || v.error) return st;
  return { text: `${st.text} · Rear camera: ${r.error}`, kind: st.kind === "bad" ? "bad" : "warn" };
}

function visionStatusFront(v) {
  if (!v || typeof v !== "object") return null;
  if (!v.enabled) return { text: "Off — the camera does not guard the van", kind: "" };
  // the front camera alone has no frames (not connected, its service down) while the rear one is fine: the rear is
  // still watched — a warning, not a failure of the whole watch
  const f = v.front, r = v.rear;
  if (v.error && v.available && f && f.error === v.error && r && typeof r === "object" && !r.error) {
    const base = v.watching ? "Watching the rear camera" : "Ready — starts watching the rear camera when the alarm is armed";
    return { text: `${base} · Front camera: ${v.error}`, kind: "warn" };
  }
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
  // a camera stopped sending video while armed (alarm, no siren) / sends it again (a note, not an alarm)
  video_lost: { label: "Camera lost", icon: "cam", kind: "bad" },
  video_back: { label: "Camera back", icon: "cam", kind: "" },
  test: { label: "Test", icon: "info", kind: "" },
  // armed / disarmed from the app or the keypad (data.source): a note in the log, not an alarm
  armed: { label: "Armed", icon: "lock", kind: "" },
  disarmed: { label: "Disarmed", icon: "unlock", kind: "" },
};
// where the alarm was armed / disarmed from (data.source)
const SOURCES = { keypad: "keypad", web: "app" };

/** Event kind for the log: {label, icon, kind}. */
// events of the rear camera (data.camera === "rear")
const REAR_LABELS = { approach: "Approaching behind the van", face: "Face at the rear camera", loiter: "Lingering behind the van" };
// lost / back video: data.camera "front" | "rear" | "both" (both lost at once)
const VIDEO_LABELS = {
  video_lost: { front: "Front camera lost", rear: "Rear camera lost", both: "Both cameras lost" },
  video_back: { front: "Front camera back", rear: "Rear camera back" },
};

/** Label, icon and tone of an event kind; camera "rear" — the rear camera's wording for camera events. */
export function kindInfo(kind, camera) {
  const k = KINDS[kind] || { label: kind ? String(kind) : "Event", icon: "info", kind: "" };
  const v = VIDEO_LABELS[kind] && VIDEO_LABELS[kind][camera];
  if (v) return { ...k, label: v };
  return camera === "rear" && REAR_LABELS[kind] ? { ...k, label: REAR_LABELS[kind] } : k;
}

/** How long a camera had no video: "40 s", "3 min", "1 h 20 min". */
export function gapText(s) {
  if (typeof s !== "number" || !Number.isFinite(s) || s < 0) return "";
  const n = Math.round(s);
  if (n < 60) return `${n} s`;
  const m = Math.floor(n / 60);
  if (m < 60) return `${m} min`;
  return m % 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${Math.floor(m / 60)} h`;
}

/** The moment a recording of the event is worth watching from (unix s): a lost camera — when its video stopped
 *  (data.since; the alarm comes video_loss_s later), anything else — the event itself. */
export function eventMoment(ev) {
  const since = ev && ev.data && ev.data.since;
  return ev && ev.kind === "video_lost" && typeof since === "number" && Number.isFinite(since) ? since : ev && ev.ts;
}

/** The cameras' video on the alarm page (AlarmStatus.cameras): {text, kind} — "Front ok · Rear no video"; null — not
 *  reported (an older device). */
export function camerasText(cams) {
  if (!Array.isArray(cams) || !cams.length) return null;
  const name = (c) => (c === "rear" ? "Rear" : "Front");
  const parts = cams.map((c) => `${name(c.camera)} ${c.video ? "ok" : c.lost ? "lost" : "no video"}`);
  return { text: parts.join(" · "), kind: cams.every((c) => c.video) ? "ok" : "bad" };
}

// marks: what moved while the camera watched, without an alarm (cars, headlights, passers-by) — grey ticks on the
// recordings and a folded list on the Alarm page (docs/contracts/vision.md "Marks"); "sound" — a loud sound at the
// rear camera's microphone while armed, with a clip of it
const MARK_LABELS = { motion: "Movement", vehicle: "Vehicle", person: "Person passing by", sound: "Loud sound" };
const MARK_PLURAL = { motion: ["movement", "movements"], vehicle: ["vehicle", "vehicles"], person: ["person", "people"],
  sound: ["loud sound", "loud sounds"] };

/** A loud sound's peak, "−14 dB" (dBFS of the camera's microphone); "" — not known. */
export function peakText(db) {
  return typeof db === "number" && Number.isFinite(db) ? `${Math.round(db)} dB`.replace("-", "\u2212") : "";
}

/** Label of a mark kind ("motion" | "vehicle" | "person" | "sound"). */
export function markLabel(kind) {
  return MARK_LABELS[kind] || "Movement";
}

/** "3 vehicles · 1 person · 8 movements" from {kind: count} (or a list of marks); "" — none. */
export function marksSummary(counts) {
  let c = counts;
  if (Array.isArray(counts)) {
    c = {};
    for (const m of counts) if (m && m.kind) c[m.kind] = (c[m.kind] || 0) + 1;
  }
  if (!c || typeof c !== "object") return "";
  return ["vehicle", "person", "sound", "motion"]
    .filter((k) => c[k] > 0)
    .map((k) => `${c[k]} ${MARK_PLURAL[k][c[k] === 1 ? 0 : 1]}`)
    .join(" · ");
}

/** The one figure worth showing next to an event's time (the title already says what it was): "0.18 g", "3.2°",
 *  "120 m", "8 s"; armed / disarmed — where from ("keypad", "app · during the alarm"); "" — nothing to add. */
export function eventDetail(ev) {
  const d = ev && ev.data && typeof ev.data === "object" ? ev.data : {};
  const n = (v) => typeof v === "number" && Number.isFinite(v);
  if (ev.kind === "impact" && n(d.peak_g)) return fmtG(d.peak_g);
  if (ev.kind === "tilt" && n(d.tilt_deg)) return `${d.tilt_deg.toFixed(1)}°`;
  if (ev.kind === "move" && n(d.distance_m)) return `${Math.round(d.distance_m)} m`;
  if (ev.kind === "loiter" && n(d.held_s)) return `${Math.round(d.held_s)} s`;
  if (ev.kind === "video_back" && n(d.lost_s)) return `off ${gapText(d.lost_s)}`;
  if (ev.kind === "armed" || ev.kind === "disarmed") {
    const src = SOURCES[d.source] || "";
    const during = ev.kind === "disarmed" && d.was === "triggered" ? "during the alarm" : "";
    return [src, during].filter(Boolean).join(" · ");
  }
  return "";
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
