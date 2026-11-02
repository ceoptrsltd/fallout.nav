// Dashboard: pure helpers (no DOM). Tests: tests/admin/dash.test.mjs.
import { silenceLabel, eventLink } from "./alarm-fmt.js";
import { fmtDeg, levelBadge } from "./level-fmt.js";
import { radioStateText } from "./radio-fmt.js";
import { agoText, fmtStay } from "./tracking-fmt.js";
import { compassWord } from "./dom.js";

const num = (v) => typeof v === "number" && Number.isFinite(v);

export const EVENTS_N = 5; // latest alarm events on the dashboard
export const MOVING_MPS = 1; // slower = parked (the GPS stationary filter reports 0 when the van stands)

// api/alarm poll: it carries the camera watch (people in view) and the event log (≤ 100 events), so slowly;
// a new event (telemetry alarm.seq) or a state change fetches it at once
export const POLL = { offline: 4000, active: 10000, idle: 30000 };

/** Next api/alarm poll in ms: fast without telemetry (the poll is the only source), slower while armed, slow when off. */
export function pollDelay(online, state) {
  if (!online) return POLL.offline;
  return state && state !== "off" ? POLL.active : POLL.idle;
}

// ───── alarm ─────

/** Live alarm: telemetry while it flows (t = telemetry.alarm), otherwise the last api/alarm status (st). */
export function alarmLive(t, st, telAt, stAt) {
  if (t) {
    return { state: t.state, armIn: t.arm_in_s, siren: !!t.siren, hazard: !!t.hazard, sensorOk: !!t.sensor_ok,
      at: telAt, seq: num(t.seq) ? t.seq : null };
  }
  if (st) {
    return { state: st.state, armIn: st.arm_in_s, siren: !!st.siren, hazard: !!(st.hazard && st.hazard.active),
      sensorOk: !!(st.sensor && st.sensor.ok), at: stAt, seq: null };
  }
  return null;
}

/**
 * No live telemetry (offline): the newest known alarm state — the last telemetry's (tAlarm at telAt: this session's
 * or kept from an earlier one) or the api/alarm status (st at stAt), whichever is newer. Same shape as alarmLive.
 */
export function newerAlarm(tAlarm, telAt, st, stAt) {
  const tOk = !!(tAlarm && typeof tAlarm === "object" && tAlarm.state);
  const useT = tOk && (!st || (num(telAt) ? telAt : 0) > (num(stAt) ? stAt : 0));
  return useT ? alarmLive(tAlarm, null, telAt, 0) : alarmLive(null, st, 0, stAt);
}

/** Buttons of the alarm card: [{what, label, icon, cls, title}] — what is the api/alarm/<what> action. */
export function alarmActions({ state, siren, hazard, hazardEnabled }) {
  if (!state) return [];
  const arm = { what: "arm", label: "Arm", icon: "lock", cls: "primary", title: "Turn the alarm on" };
  if (state === "off") { // the siren sounded by hand (or the hazard test) with the alarm off: Stop too
    return siren || hazard
      ? [{ what: "silence", label: silenceLabel({ siren, hazard, hazardEnabled: false }), icon: "vol", cls: "primary",
        title: "Stop the siren and the hazard lights" }, arm]
      : [arm];
  }
  const out = [];
  if (siren || hazard) {
    out.push({ what: "silence", label: silenceLabel({ siren, hazard, hazardEnabled }), icon: "vol", cls: "primary",
      title: "Stop the siren and the hazard lights, the alarm stays armed" });
  }
  out.push({ what: "disarm", label: "Disarm", icon: "unlock", cls: "danger", title: "Turn the alarm off" });
  return out;
}

// After an action's answer the new alarm buttons ignore taps for a moment: Disarm appears right where Arm was (Arm
// where Disarm was, Disarm where Stop siren was), and on the van's Wi-Fi the answer takes ~50 ms — a double tap or a
// second "did it register?" tap would hit it. Dashboard and Alarm page.
export const ACT_COOL_MS = 1500;

/** ms the alarm buttons stay inert after the last action's answer at doneAt (Date.now()); 0 — they take taps. */
export function actCooldown(doneAt, now, ms = ACT_COOL_MS) {
  if (!num(doneAt) || !num(now)) return 0;
  const left = doneAt + ms - now;
  return left > 0 && left <= ms ? left : 0; // the clock went back: not stuck
}

// the car screen's alarm banner words (web/display/ui/alarm.js BANNER_WORD; the admin does not import display modules)
const BANNER_WORD = { impact: "IMPACT", tilt: "TILTED", move: "MOVING", approach: "PERSON", face: "FACE", loiter: "PERSON",
  video_lost: "CAMERA" };

/** Word of the hazard banner for the event that triggered the alarm: "IMPACT" … ("ALARM" for anything else). */
export function bannerWord(kind) {
  return BANNER_WORD[kind] || "ALARM";
}

/** Big state word. */
export function alarmWord(state) {
  return { off: "OFF", arming: "ARMING", armed: "ARMED", triggered: "TRIGGERED" }[state] || (state ? String(state).toUpperCase() : "—");
}

/** "Armed for 2 h 10 min" / "Armed just now"; null without a time. */
export function armedFor(armedAt, nowS) {
  if (!num(armedAt)) return null;
  const s = nowS - armedAt;
  return s < 60 ? "Armed just now" : "Armed for " + fmtStay(s);
}

/** Arm countdown bar 0…1: left seconds of total. */
export function armProgress(left, total) {
  const t = num(total) && total > 0 ? total : 1;
  if (!num(left)) return 0;
  return Math.max(0, Math.min(1, 1 - left / t));
}

/** Hint under the Arm button: "Arms 60 s after you press Arm." */
export function armHint(delay) {
  if (delay === 0) return "Arms at once.";
  return num(delay) ? `Arms ${delay} s after you press Arm.` : "";
}

/**
 * Status chips of the alarm card: [{key, icon, text, kind, title}]. The camera watch always (when reported);
 * the hazard lights when they are used or broken; the shock sensor and the server link only when something is wrong.
 * hazardActive: live from telemetry (null = use st).
 */
export function alarmChips(st, { sensorOk = null, hazardActive = null } = {}) {
  const out = [];
  if (!st || typeof st !== "object") return out;
  const v = st.vision;
  if (v && typeof v === "object") {
    let c;
    if (!v.enabled) c = { text: "Camera watch off", kind: "", title: "The camera does not guard the van (Alarm → Camera watch)" };
    else if (v.error) c = { text: "Camera watch error", kind: "bad", title: String(v.error) };
    else if (!v.available) c = { text: "Camera watch not running", kind: "bad", title: "The vision service is not running" };
    else if (v.watching) {
      const n = num(v.people) ? v.people : 0;
      c = n ? { text: n === 1 ? "A person in view" : `${n} people in view`, kind: "warn", title: "The camera sees people now" }
        : { text: "Camera watching", kind: "ok", title: "The camera watches for people coming close" };
    } else c = { text: "Camera watch ready", kind: "ok", title: "Starts watching when the alarm is armed" };
    out.push({ key: "vision", icon: "cam", ...c });
  }
  const hz = st.hazard;
  if (hz && typeof hz === "object") {
    const active = hazardActive === null ? !!hz.active : !!hazardActive;
    if (hz.error) out.push({ key: "hazard", icon: "hazard", text: "Hazard lights error", kind: "bad", title: String(hz.error) });
    else if (active) out.push({ key: "hazard", icon: "hazard", text: "Hazard lights flashing", kind: "warn", title: "Flashing now" });
    else if (hz.enabled) out.push({ key: "hazard", icon: "hazard", text: "Hazard lights ready", kind: "ok", title: "They flash when the alarm goes off" });
  }
  const sen = st.sensor;
  const ok = sensorOk === null ? !!(sen && sen.ok) : !!sensorOk;
  if (sen && !ok) {
    out.push({ key: "sensor", icon: "warn", text: "Shock sensor not responding", kind: "bad",
      title: (sen.error ? sen.error + " — " : "") + "impact and tilt are not detected; GPS movement still is" });
  }
  const rel = st.relay;
  if (rel && !rel.configured) {
    out.push({ key: "relay", icon: "bell", text: "No notifications", kind: "warn", title: "The device has no server link — notifications cannot be sent" });
  } else if (rel && rel.pending > 0) {
    out.push({ key: "relay", icon: "bell", text: `${rel.pending} waiting to send`, kind: "warn", title: "Events waiting to be sent to the server" });
  }
  return out;
}

// ───── events ─────

/** The newest n events (the log is newest first; sorted again to be safe). */
export function recentEvents(events, n = EVENTS_N) {
  const list = Array.isArray(events) ? events.filter((e) => e && typeof e === "object") : [];
  return [...list].sort((a, b) => (num(b.ts) ? b.ts : 0) - (num(a.ts) ? a.ts : 0)).slice(0, n);
}

const segOf = (v) => (typeof v === "string" && v ? v : null);

/** What an event offers: {snapshot, segment, snapshotRear, segmentRear, link} — the rear camera's too, when it is on. */
export function eventExtras(ev) {
  const d = ev && ev.data && typeof ev.data === "object" ? ev.data : {};
  return {
    snapshot: d.snapshot === true,
    segment: segOf(d.segment),
    snapshotRear: d.snapshot_rear === true,
    segmentRear: segOf(d.segment_rear),
    link: eventLink(ev),
  };
}

/** Which camera raised the event: "rear" (data.camera — the rear camera's watch) or "front" (everything else). */
export function eventCamera(ev) {
  return ev && ev.data && ev.data.camera === "rear" ? "rear" : "front";
}

/**
 * Cameras with a snapshot of the event, the one that raised it first: a rear-camera event shows the rear's snapshot
 * (the front's is just the street ahead — at night a black square).
 */
export function snapCams(ev) {
  const x = eventExtras(ev);
  const cams = [x.snapshot ? "front" : null, x.snapshotRear ? "rear" : null].filter(Boolean);
  return eventCamera(ev) === "rear" ? cams.reverse() : cams;
}

/** Change marker of the shown events: ids plus the snapshots / recordings added a moment after the event itself. */
export function recentKey(events) {
  return (events || []).map((e) => {
    const x = eventExtras(e);
    return `${e.id}:${x.snapshot ? 1 : 0}:${x.segment ? 1 : 0}:${x.snapshotRear ? 1 : 0}:${x.segmentRear ? 1 : 0}`;
  }).join("|");
}

/** Snapshot of an event, relative to the app root; camera "rear" — the rear camera's (<id>-rear.jpg). */
export function snapPath(id, camera) {
  return `api/alarm/snapshot/${encodeURIComponent(String(id))}${camera === "rear" ? "-rear" : ""}.jpg`;
}

/** The snapshot's small copy for event lists (~5 KB instead of ~40, made once on the device). */
export function thumbPath(id, camera) {
  return snapPath(id, camera) + "?thumb=1";
}

/** "5 min ago" for an event time (unix s). */
export function eventAgo(ts, nowS) {
  return num(ts) ? agoText(ts, nowS) : "—";
}

// ───── the van ─────

/** GPS fix: {text: "3D · 9 sats", kind}. */
export function fixInfo(g) {
  const gps = g || {};
  const sats = num(gps.sats_used) ? gps.sats_used : 0;
  if (gps.fix === "3d") return { text: `3D · ${sats} sats`, kind: "ok" };
  if (gps.fix === "2d") return { text: `2D · ${sats} sats`, kind: "warn" };
  return { text: "No fix", kind: "bad" };
}

/** "52.22970, 21.01220" or null. */
export function fmtLatLon(lat, lon) {
  if (!num(lat) || !num(lon)) return null;
  return `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
}

/** Position tile value: {text, moving}: "54 km/h NE" / "Parked" / "No position". */
export function motionInfo(g, heading) {
  const gps = g || {};
  const fixed = gps.fix === "2d" || gps.fix === "3d";
  const v = gps.speed_mps;
  // without a fix the speed is unknown, even if a packet still carries the last one
  if (fixed && num(v) && v >= MOVING_MPS) {
    const deg = heading && num(heading.deg) ? heading.deg : null;
    return { text: `${Math.round(v * 3.6)} km/h${deg !== null ? " " + compassWord(deg) : ""}`, moving: true };
  }
  if (fixed) return { text: "Parked", moving: false };
  return { text: "No position", moving: false };
}

/** Speed limit sign under the wheels: "50" or null. */
export function limitText(sl) {
  return sl && num(sl.kmh) && sl.kmh > 0 ? String(Math.round(sl.kmh)) : null;
}

/** Camera tile value: {text, kind, rec}. */
export function camInfo(cam) {
  const c = cam || {};
  if (c.recording) return { text: "Recording", kind: "ok", rec: true };
  if (c.state === "error") return { text: "Camera error", kind: "bad", rec: false };
  if (c.state === "starting") return { text: "Starting…", kind: "warn", rec: false };
  if (c.state === "live") return { text: "Live, not recording", kind: "ok", rec: false };
  if (c.state === "disabled") return { text: "Off", kind: "", rec: false };
  return { text: "Not recording", kind: "warn", rec: false };
}

/** Rear camera line of the camera tile (telemetry camera_rear; null — off or not reported yet): {text, kind, rec}. */
export function rearInfo(cam) {
  const c = cam || {};
  if (c.recording) return { text: "Recording", kind: "ok", rec: true };
  if (c.state === "live") return { text: "Live", kind: "ok", rec: false };
  if (c.state === "error") return { text: "Error", kind: "bad", rec: false };
  if (c.state === "disabled") return { text: "Off", kind: "", rec: false };
  if (c.state === "stopped") return { text: "Not recording", kind: "warn", rec: false };
  return { text: "Starting…", kind: "warn", rec: false };
}

/** The car screen button of the camera tile (telemetry display): show the rear there, or go back to the front. */
export function screenAction(display) {
  if (display && display.camera === "rear") {
    return { to: "front", label: "Back to front", onScreen: true, title: "The car screen shows the rear camera: switch it back (like key 4)" };
  }
  return { to: "rear", label: "Show on screen", onScreen: false, title: "Show the rear camera on the car screen (like key 4)" };
}

const fmtMs = (ms) => (ms < 10 ? ms.toFixed(1) : ms.toFixed(0));
const fmtGain = (g) => (g < 10 ? g.toFixed(1) : g.toFixed(0));

/**
 * Exposure line of the camera tile: {text: "Night · 125 ms · 16×", night, title} or null (not reported).
 * Night: the exposure reached (80 % of) its limit — it is dark, the camera adds gain.
 */
export function exposureInfo(cam) {
  const c = cam || {};
  if (!num(c.exposure_ms)) return null;
  const night = num(c.max_exposure_ms) && c.max_exposure_ms > 0 && c.exposure_ms >= 0.8 * c.max_exposure_ms;
  const parts = [night ? "Night" : "Day", `${fmtMs(c.exposure_ms)} ms`];
  if (num(c.gain)) parts.push(`${fmtGain(c.gain)}×`);
  const full = [`exposure ${fmtMs(c.exposure_ms)} ms`];
  if (num(c.gain)) full.push(`gain ${fmtGain(c.gain)}×`);
  if (num(c.lux)) full.push(`${c.lux.toFixed(1)} lux`);
  if (num(c.max_exposure_ms)) {
    const why = c.moving === true ? "driving" : c.moving === false ? "parked" : "speed unknown";
    full.push(`limit ${fmtMs(c.max_exposure_ms)} ms (${why})`);
  }
  return { text: parts.join(" · "), night: !!night, title: full.join(" · ") };
}

/** CPU temperature: {text: "52 °C", kind}. */
export function tempInfo(c) {
  if (!num(c)) return { text: "—", kind: "" };
  return { text: `${c.toFixed(0)} °C`, kind: c >= 80 ? "bad" : c >= 70 ? "warn" : "" };
}

/** vcgencmd get_throttled → {text, kind}: kind "bad" now, "warn" happened since boot, "ok", "" unknown. */
export function throttleInfo(hex) {
  if (hex == null || hex === "") return { text: "—", kind: "" };
  const v = parseInt(String(hex), 16);
  if (!Number.isFinite(v)) return { text: String(hex), kind: "" };
  const now = [];
  if (v & 0x1) now.push("under-voltage");
  if (v & 0x2) now.push("frequency capped");
  if (v & 0x4) now.push("throttled");
  if (v & 0x8) now.push("temperature limit");
  if (now.length) return { text: now.join(", ").replace(/^./, (c) => c.toUpperCase()), kind: "bad" };
  const past = [];
  if (v & 0x10000) past.push("under-voltage");
  if (v & 0x20000) past.push("frequency cap");
  if (v & 0x40000) past.push("throttling");
  if (v & 0x80000) past.push("temperature limit");
  if (past.length) return { text: (past.join(", ") + " since boot").replace(/^./, (c) => c.toUpperCase()), kind: "warn" };
  return { text: "OK", kind: "ok" };
}

/** Free disk: {text: "12.3 GB free", kind}. */
export function diskInfo(disk, cam) {
  const free = disk && num(disk.free_gb) ? disk.free_gb : cam && num(cam.free_gb) ? cam.free_gb : null;
  if (free === null) return { text: "—", kind: "" };
  return { text: `${free.toFixed(free < 10 ? 1 : 0)} GB free`, kind: free < 2 ? "bad" : free < 5 ? "warn" : "" };
}

/** Network line: {text: "Online · Pixel 72%", kind}. */
export function netInfo(sys) {
  const s = sys || {};
  const wifi = s.wifi_ssid ? `${s.wifi_ssid}${num(s.wifi_signal_pct) ? " " + s.wifi_signal_pct + "%" : ""}` : null;
  if (s.internet === true) return { text: wifi ? `Online · ${wifi}` : "Online", kind: "ok" };
  if (s.internet === false) return { text: wifi ? `No internet · ${wifi}` : "No internet", kind: "warn" };
  return { text: wifi || "—", kind: "" };
}

/** Level tile: {text, kind, sub, note} or null (no level in telemetry). */
export function levelInfo(l) {
  if (!l || typeof l !== "object") return null;
  if (l.sensor_ok === false) return { text: "No sensor", kind: "bad", sub: "", note: "" };
  const b = levelBadge(l);
  const lv = b.text.startsWith("level");
  const text = !num(l.pitch_deg) || !num(l.roll_deg) ? "—" : lv ? "Level" : "Tilted";
  return {
    text, kind: text === "—" ? "" : lv ? "ok" : "warn",
    sub: `Pitch ${fmtDeg(l.pitch_deg)} · Roll ${fmtDeg(l.roll_deg)}`,
    note: l.calibrated === false ? "not calibrated" : "",
  };
}

const RADIO_ACTIVE = new Set(["playing", "connecting", "waiting_output"]);

/** Radio tile: {active, playing, title, off, line, lineKind, canPlay, volume}. */
/** Volume step of the dashboard radio buttons — the same as the keypad knob (5 %). */
export const VOL_STEP = 5;

/** Next radio volume after a −/+ tap: from the value just sent (taps faster than telemetry) or the reported one;
 * clamped to 0–100; null — nothing to step from yet. */
export function nextVolume(pending, reported, delta) {
  const cur = Number.isFinite(pending) ? pending : Number.isFinite(reported) ? reported : null;
  if (cur === null) return null;
  return Math.max(0, Math.min(100, Math.round(cur + delta)));
}

export function radioInfo(r) {
  if (!r || typeof r !== "object") return { active: false, playing: false, title: "Radio", off: true, line: "Not reported", lineKind: "", canPlay: false, volume: null };
  const state = r.state || "stopped";
  const active = RADIO_ACTIVE.has(state);
  const st = radioStateText(state);
  let line = st.text;
  let lineKind = st.kind === "ok" ? "" : st.kind;
  if (state === "playing") line = r.track || "On air";
  if (state === "stopped") { line = r.station ? "Off" : "Off — pick a station on the Radio page"; lineKind = ""; }
  return {
    active, playing: state === "playing", title: r.station || "No station", off: !active,
    line, lineKind, track: state === "playing" && !!r.track, canPlay: !!r.station, volume: num(r.volume) ? r.volume : null,
  };
}

/** Bluetooth speaker of the radio: {text, connected, kind}. */
export function speakerInfo(r) {
  if (!r || !r.output) return { text: "No speaker", connected: false, kind: "" };
  return r.output_connected ? { text: r.output, connected: true, kind: "ok" } : { text: r.output, connected: false, kind: "warn" };
}

// ───── offline: the last known data ─────

/**
 * The server's last point of the van (api/tracking/last, remote — fnav-relay keeps it while the device is away) when it
 * is newer than the last telemetry this phone saw (keptAt, Date.now() ms): {lat, lon, speed_mps, course_deg, ts} or null.
 */
export function serverPos(last, keptAt) {
  if (!last || !num(last.lat) || !num(last.lon)) return null;
  const ts = num(last.ts) ? last.ts : num(last.received_ts) ? last.received_ts : null;
  if (ts === null || (num(keptAt) && keptAt > 0 && ts * 1000 <= keptAt)) return null;
  return { lat: last.lat, lon: last.lon, speed_mps: num(last.speed_mps) ? last.speed_mps : null,
    course_deg: num(last.course_deg) ? last.course_deg : null, ts };
}

// ───── quick actions: protect the recording, the car monitor, the car screen ─────

/** The car monitor (api/display/output): state word and the button — {to, label, state, kind, title}; on: bool / null. */
export function videoAction(on) {
  if (on === true) return { to: false, label: "Turn off", state: "On", kind: "ok", title: "Turn the picture on the car monitor off (like the video key)" };
  if (on === false) return { to: true, label: "Turn on", state: "Off", kind: "warn", title: "Turn the picture on the car monitor back on" };
  return { to: null, label: "On / off", state: "—", kind: "", title: "Switch the picture on the car monitor on or off" };
}

/** Car screen modes (api/display/force, settings.display.force_mode). */
export const SCREEN_MODES = [
  { value: "auto", label: "Auto" },
  { value: "map", label: "Map" },
  { value: "camera", label: "Camera" },
];

/** What the car screen shows now (telemetry display): "Showing the map · by speed"; "" — not reported. */
export function screenNowText(d) {
  if (!d || (d.mode !== "map" && d.mode !== "camera")) return "";
  if (d.mode === "camera" && d.camera === "rear") return "Showing the rear camera";
  const why = { speed: "by speed", no_fix: "no GPS fix", forced: "forced", startup: "starting" }[d.reason];
  return `Showing ${d.mode === "map" ? "the map" : "the camera"}${why ? " · " + why : ""}`;
}

/**
 * Confirmation for a quick action ({title, text, okText, danger}) or null when it is not disruptive: the car monitor
 * going dark always asks (also a toggle of an unknown state, which may turn it off); forcing the map or the camera
 * asks while the van is moving — the driver's screen changes under them.
 */
export function quickConfirm(kind, { to = null, mode = null, moving = false } = {}) {
  if (kind === "video" && to !== true) {
    return {
      title: to === false ? "Turn the car monitor off?" : "Switch the car monitor?",
      text: (to === false ? "The car monitor goes dark" : "If the car monitor is on, it goes dark")
        + " — no map and no camera for the driver until it is turned back on here or with the video key.",
      okText: to === false ? "Turn off" : "Switch", danger: true,
    };
  }
  if (kind === "screen" && moving && (mode === "map" || mode === "camera")) {
    return {
      title: mode === "map" ? "Always show the map?" : "Always show the camera?",
      text: `The van is moving: the driver's screen switches to the ${mode} and stays there until set back to Auto.`,
      okText: mode === "map" ? "Show the map" : "Show the camera", danger: false,
    };
  }
  return null;
}

/** Note after api/recordings/protect-current ({segment, segment_rear}): "Front and rear recordings protected". */
export function protectedText(r) {
  const f = !!(r && r.segment), b = !!(r && r.segment_rear);
  if (f && b) return "Front and rear recordings protected";
  if (b) return "Rear recording protected";
  return f ? "Recording protected" : "Protected";
}
