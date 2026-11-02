// Shared state: WebSocket <root>ws (telemetry, settings, events) + settings.
// Liveness (conn-fmt.js): the socket is pinged and watched — an "open" socket that stays silent (an iPhone back from the
// lock screen, a half-dead tunnel) or one stuck connecting is dropped and opened again; coming back on screen probes the
// socket, a network change ("online") reconnects at once. store.online has hysteresis (linkStep). The last telemetry is
// kept in localStorage (keep / kept): the dashboard shows it greyed out with "Last seen …" while the device is away.
import { api, goLogin, ROOT } from "./api.js";
import { setDeviceZone } from "./devtime.js";
import {
  RESUME_GAP_MS, RETRY_MS, KEEP_PREFIX, KEEP_MS, wsCheck, nextRetry, linkStep, runStart, packKept, unpackKept,
} from "./conn-fmt.js";

export const store = {
  offline: false, // remote and the device is not connected: api/session answered by the server (remote.md)
  telemetry: null,
  telemetryAt: 0, // Date.now() of the last packet
  settings: null,
  wsOpen: false,
  online: null, // telemetry flows: null — not known yet, then true / false with hysteresis (conn-fmt.js linkStep)
};

// conn: (online) — the online state or store.offline changed
const subs = { telemetry: new Set(), settings: new Set(), conn: new Set(), event: new Set(), nmea: new Set() };

export function on(type, fn) {
  subs[type].add(fn);
  return () => subs[type].delete(fn);
}
function emit(type, ...args) {
  for (const fn of [...subs[type]]) {
    try { fn(...args); } catch (e) { console.error("handler", type, e); }
  }
}

let ws = null;
let wsAt = 0; // Date.now() the current socket was created
let openAt = 0; // … it opened
let msgAt = 0; // … it last delivered anything
let pingAt = 0;
let probeAt = 0; // back on screen: the socket must deliver something after this (0 — no probe)
let retry = RETRY_MS.first;
let timer = null; // the next connect()
let everOpened = false;
let started = false;
let startAt = 0;
let runAt = 0; // first packet of the current telemetry run (linkStep: steady for ON_MS)
let resumeAt = 0; // back on screen (linkStep: the time hidden does not count)
let tickAt = 0;
let keptAt = 0;
const topics = new Map(); // topic -> subscriber count

export function wsSend(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    try { ws.send(JSON.stringify(obj)); } catch { /* a dying socket: the watchdog replaces it */ }
  }
}

/** Subscribe to a topic (e.g. "nmea"); returns an unsubscribe function. */
export function subscribeTopic(topic) {
  const n = (topics.get(topic) || 0) + 1;
  topics.set(topic, n);
  if (n === 1) wsSend({ type: "subscribe", topics: [topic] });
  let done = false;
  return () => {
    if (done) return;
    done = true;
    const m = (topics.get(topic) || 1) - 1;
    if (m <= 0) {
      topics.delete(topic);
      wsSend({ type: "unsubscribe", topics: [topic] });
    } else topics.set(topic, m);
  };
}

function setSettings(s) {
  store.settings = s;
  emit("settings", s);
}
export function applySettings(s) { setSettings(s); }

/** The server answered for the device (remote: api/session {"offline": true}); cleared when telemetry comes. */
export function setOffline(v) {
  if (store.offline === !!v) return;
  store.offline = !!v;
  emit("conn", store.online);
}

function updateOnline() {
  const now = Date.now();
  const next = linkStep(store.online, {
    now, telAt: store.telemetryAt, runAt, wsOpen: store.wsOpen, hidden: document.hidden, resumeAt, startAt,
  });
  const wasOffline = store.offline;
  if (next === true) store.offline = false; // telemetry again: the device is back
  if (next !== store.online || wasOffline !== store.offline) {
    store.online = next;
    emit("conn", next);
  }
}

// ───── last known data ─────

/** Keep a value for the offline view (Date.now() it was current — default now). Never throws. */
export function keep(name, v, at = Date.now()) {
  try { localStorage.setItem(KEEP_PREFIX + name, packKept(v, at)); } catch { /* full / private mode */ }
}
/** A kept value: {at, v} or null. */
export function kept(name) {
  try { return unpackKept(localStorage.getItem(KEEP_PREFIX + name), Date.now()); } catch { return null; }
}
/** Log out: the position and the alarm state must not stay on this phone. */
export function forgetKept() {
  try {
    for (let i = localStorage.length - 1; i >= 0; i--) {
      const k = localStorage.key(i);
      if (k && k.startsWith(KEEP_PREFIX)) localStorage.removeItem(k);
    }
  } catch { /* */ }
}
function keepTelemetry() {
  if (!store.telemetry || !store.telemetryAt) return;
  keptAt = Date.now();
  keep("telemetry", store.telemetry, store.telemetryAt);
}
/** The latest telemetry seen: live (this session) or kept from an earlier one — {t, at} or null. */
export function lastTelemetry() {
  if (store.telemetry) return { t: store.telemetry, at: store.telemetryAt };
  const k = kept("telemetry");
  return k ? { t: k.v, at: k.at } : null;
}

// ───── socket ─────

function schedule(ms) {
  clearTimeout(timer);
  timer = null;
  if (ws) return;
  timer = setTimeout(connect, Math.max(0, ms));
}

/** Forget the current socket (closed, silent or stuck): its late events are ignored. */
function forget() {
  const sock = ws;
  ws = null;
  probeAt = 0;
  if (sock) {
    sock.onopen = sock.onmessage = sock.onclose = sock.onerror = null;
    try { sock.close(); } catch { /* */ }
  }
  if (store.wsOpen) store.wsOpen = false;
  updateOnline();
}

function connect() {
  clearTimeout(timer);
  timer = null;
  if (ws) return;
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  let sock;
  try {
    sock = new WebSocket(`${proto}//${location.host}${ROOT}ws`);
  } catch {
    schedule(retry);
    retry = nextRetry(retry);
    return;
  }
  ws = sock;
  wsAt = Date.now();
  openAt = msgAt = pingAt = probeAt = 0;
  sock.onopen = () => {
    if (sock !== ws) return;
    store.wsOpen = true;
    everOpened = true;
    openAt = Date.now();
    retry = RETRY_MS.first;
    if (topics.size) wsSend({ type: "subscribe", topics: [...topics.keys()] });
    updateOnline();
  };
  sock.onmessage = (ev) => {
    if (sock !== ws) return;
    msgAt = Date.now();
    let m;
    try { m = JSON.parse(ev.data); } catch { return; }
    switch (m.type) {
      case "telemetry": {
        const now = Date.now();
        runAt = runStart(runAt, store.telemetryAt, now);
        store.telemetry = m;
        if (m.system && m.system.timezone) setDeviceZone(m.system.timezone); // the van crossed a border
        store.telemetryAt = now;
        updateOnline();
        if (now - keptAt > KEEP_MS) keepTelemetry();
        emit("telemetry", m);
        break;
      }
      case "settings":
        setSettings(m.settings);
        break;
      case "event":
        emit("event", m);
        break;
      case "nmea":
        emit("nmea", m.lines || []);
        break;
    }
  };
  sock.onclose = () => { if (sock === ws) closed(); };
  sock.onerror = () => { try { sock.close(); } catch { /* */ } };
}

async function closed() {
  forget();
  // session expired: WS will not connect, go to login; the server may also say the device is offline
  if (!everOpened || retry >= 2000) {
    try {
      const s = await api.get("/api/session", { noRedirect: true, timeout: 8000 });
      if (s && s.authenticated === false) return goLogin();
      if (s) setOffline(!!s.offline);
    } catch { /* no connection: just wait */ }
  }
  schedule(retry);
  retry = nextRetry(retry);
}

/** Back on screen (or the page was frozen): the open socket has PROBE_MS to deliver something; a closed one — now. */
function resume() {
  resumeAt = Date.now();
  if (!started) return;
  if (!ws) {
    retry = RETRY_MS.first;
    connect();
    return;
  }
  if (ws.readyState === WebSocket.OPEN && (!probeAt || msgAt >= probeAt)) {
    probeAt = Date.now();
    pingAt = probeAt;
    wsSend({ type: "ping" });
  }
}

/** The network changed (Wi-Fi ↔ mobile): the old socket is bound to the old path — a new one at once. */
function rejoin() {
  if (!started || document.hidden) return;
  resumeAt = Date.now();
  retry = RETRY_MS.first;
  forget();
  connect();
}

function tick() {
  const now = Date.now();
  if (tickAt && now - tickAt > RESUME_GAP_MS && !document.hidden) resume(); // the page was frozen
  tickAt = now;
  const v = wsCheck({
    state: ws ? ws.readyState : -1, now, createdAt: wsAt, openedAt: openAt, msgAt, pingAt, probeAt, hidden: document.hidden,
  });
  if (v === "ping") {
    pingAt = now;
    wsSend({ type: "ping" });
  } else if (v === "drop") {
    const stuck = !!ws && ws.readyState !== 1;
    forget();
    // a silent open socket: a new one now; one that never opened (or never finished closing): the usual back-off
    schedule(stuck ? retry : 0);
    if (stuck) retry = nextRetry(retry);
  }
  updateOnline();
}

export async function start() {
  try {
    setSettings(await api.get("/api/settings"));
  } catch (e) {
    console.warn("settings", e);
  }
  started = true;
  startAt = tickAt = Date.now();
  connect();
  setInterval(tick, 1000);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) keepTelemetry();
    else resume();
  });
  window.addEventListener("pageshow", (e) => { if (e.persisted) resume(); }); // back from the bfcache
  window.addEventListener("pagehide", keepTelemetry);
  window.addEventListener("online", rejoin);
}

export async function reloadSettings() {
  const s = await api.get("/api/settings");
  setSettings(s);
  return s;
}
