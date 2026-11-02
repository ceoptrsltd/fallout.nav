// Connection to the device: pure helpers (no DOM) — WebSocket liveness, the online state with hysteresis, the banner
// over the pages, the last data kept for the offline view. Used by store.js and shell.js; tests: tests/admin/conn.test.mjs.
import { agoText } from "./tracking-fmt.js";

// The device sends telemetry at 10 Hz (2 Hz through the tunnel), so an "open" socket that stays silent for seconds is
// dead: an iPhone back from the lock screen keeps such a zombie until iOS kills the TCP connection, a half-dead tunnel
// (the server's ssh has not timed out yet) keeps it ~90 s, and the browser reports it open all that time.
export const PING_MS = 5000; // client ping (fnav/core/ws.py answers pong): keeps the path warm, a dead one fails sooner
export const SILENCE_MS = 4000; // an open socket without a single message this long is dropped and opened again
export const CONNECT_MS = 10000; // a socket still connecting after this is given up (the tunnel took it, nobody answers)
export const PROBE_MS = 1500; // back on screen: the open socket must deliver something within this, or it is replaced
export const RESUME_GAP_MS = 3000; // the 1 s watchdog tick came this late: the page was frozen (lock screen) — as back on screen
export const FRESH_MS = 2000; // telemetry this recent is flowing; a longer gap between packets starts a new run
export const OFF_MS = 6000; // online → offline: no telemetry this long while on screen (was 3 s, no hysteresis)
export const ON_MS = 1500; // offline → online: telemetry has been flowing this long (one packet on a weak link is not back)
export const RETRY_MS = { first: 500, max: 5000, factor: 1.6 }; // reconnect back-off

/**
 * What to do with the socket now: "ok" | "ping" | "drop".
 * s: {state: WebSocket readyState (-1 — none), now, createdAt, openedAt, msgAt (last message), pingAt (last ping sent),
 *     probeAt (a probe after coming back on screen; 0 — none), hidden}
 */
export function wsCheck(s) {
  const now = s.now;
  if (s.state === 0) return now - (s.createdAt || 0) > CONNECT_MS ? "drop" : "ok";
  const heard = Math.max(s.msgAt || 0, s.openedAt || 0, s.createdAt || 0);
  if (s.state === 2 || s.state === 3) return now - heard > SILENCE_MS ? "drop" : "ok"; // its close event never came
  if (s.state !== 1) return "ok";
  // a probe decides on its own (the page was frozen: the silence before it says nothing)
  if (s.probeAt && heard < s.probeAt) return now - s.probeAt > PROBE_MS ? "drop" : "ok";
  // hidden: timers are throttled and the OS may suspend the page — judged again when it is back on screen
  if (!s.hidden && now - heard > SILENCE_MS) return "drop";
  return now - (s.pingAt || 0) >= PING_MS ? "ping" : "ok";
}

/** Next reconnect delay after `ms` failed (back-off). */
export function nextRetry(ms) {
  return Math.min(RETRY_MS.max, Math.max(RETRY_MS.first, (ms || RETRY_MS.first) * RETRY_MS.factor));
}

/**
 * The online state with hysteresis: null (not known yet) | true | false.
 * i: {now, telAt (last telemetry, 0 — none), runAt (first packet of the current run), wsOpen, hidden,
 *     resumeAt (back on screen, 0 — never), startAt (connecting started)}
 * Off only after OFF_MS without telemetry while on screen (the time hidden does not count: back from the lock screen
 * the socket gets a moment to show it is alive); on again after ON_MS of steady telemetry — except the very first
 * time, which is at once. A weak mobile link no longer toasts "Connection lost" / "restored" in pairs.
 */
export function linkStep(prev, i) {
  const now = i.now;
  const since = (t) => now - Math.max(t || 0, i.resumeAt || 0);
  if (prev === true) {
    if (i.hidden) return true; // nobody looks; decided when back on screen
    return since(i.telAt) > OFF_MS ? false : true;
  }
  const fresh = !!i.wsOpen && i.telAt > 0 && now - i.telAt < FRESH_MS;
  if (fresh && (prev === null || now - (i.runAt || now) >= ON_MS)) return true;
  if (prev === null) return !i.hidden && since(i.startAt) > OFF_MS ? false : null;
  return false;
}

/** Start of the telemetry run a packet arriving at `now` belongs to (prevAt — the previous packet, 0 — none). */
export function runStart(runAt, prevAt, now) {
  return prevAt > 0 && now - prevAt < FRESH_MS && runAt > 0 ? runAt : now;
}

/**
 * The banner over the pages, or null. offline: the server answered for the device (remote.md §1) — the journal is
 * still there; online === false: the link is lost (with hysteresis), the pages show the last known data.
 */
export function linkBanner({ offline, online }) {
  if (offline) return { kind: "warn", text: "Device is offline — showing data from the server" };
  if (online === false) return { kind: "warn", text: "No connection to the device — reconnecting…" };
  return null;
}

// ───── last known data (localStorage): shown greyed out with "Last seen …" while the device is unreachable ─────

export const KEEP_PREFIX = "fnav.last."; // + "telemetry", "alarm"; cleared on Log out
export const KEEP_MS = 15000; // telemetry is written at most this often (and when the page is hidden)

/** The stored text of a kept value: {at, v}. */
export function packKept(v, at) {
  return JSON.stringify({ at, v });
}

/** A kept value back: {at, v} or null (missing, malformed, from the future by more than a day — a wrong clock). */
export function unpackKept(raw, now) {
  if (typeof raw !== "string" || !raw) return null;
  let o;
  try { o = JSON.parse(raw); } catch { return null; }
  if (!o || typeof o !== "object" || typeof o.at !== "number" || !Number.isFinite(o.at) || o.at <= 0) return null;
  if (!o.v || typeof o.v !== "object") return null;
  if (typeof now === "number" && o.at > now + 86400000) return null;
  return { at: o.at, v: o.v };
}

/** "Last seen 12 min ago" for a Date.now() time; "" without one. */
export function lastSeenText(at, now) {
  if (typeof at !== "number" || !Number.isFinite(at) || at <= 0) return "";
  return "Last seen " + agoText(at / 1000, now / 1000);
}
