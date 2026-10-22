// Shared state: WebSocket <root>ws (telemetry, settings, events) + settings.
import { api, goLogin, ROOT } from "./api.js";

export const store = {
  offline: false, // remote and the device is not connected: api/session answered by the server (remote.md)
  telemetry: null,
  telemetryAt: 0, // Date.now() of the last packet
  settings: null,
  wsOpen: false,
  online: false, // WS open and telemetry fresh
};

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
let retry = 500;
let everOpened = false;
const topics = new Map(); // topic -> subscriber count

export function wsSend(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
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

function updateOnline() {
  const now = store.wsOpen && Date.now() - store.telemetryAt < 3000;
  if (now !== store.online) {
    store.online = now;
    emit("conn", now);
  }
}

function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${proto}//${location.host}${ROOT}ws`);
  ws.onopen = () => {
    store.wsOpen = true;
    everOpened = true;
    retry = 500;
    if (topics.size) wsSend({ type: "subscribe", topics: [...topics.keys()] });
    updateOnline();
  };
  ws.onmessage = (ev) => {
    let m;
    try { m = JSON.parse(ev.data); } catch { return; }
    switch (m.type) {
      case "telemetry":
        store.telemetry = m;
        store.telemetryAt = Date.now();
        updateOnline();
        emit("telemetry", m);
        break;
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
  ws.onclose = async () => {
    store.wsOpen = false;
    updateOnline();
    // session expired: WS will not connect, go to login
    if (!everOpened || retry >= 2000) {
      try {
        const s = await api.get("/api/session", { noRedirect: true });
        if (s && s.authenticated === false) return goLogin();
      } catch { /* no connection: just wait */ }
    }
    setTimeout(connect, retry);
    retry = Math.min(5000, retry * 1.6);
  };
  ws.onerror = () => { try { ws.close(); } catch { /* */ } };
}

export async function start() {
  try {
    setSettings(await api.get("/api/settings"));
  } catch (e) {
    console.warn("settings", e);
  }
  connect();
  setInterval(updateOnline, 1000);
}

export async function reloadSettings() {
  const s = await api.get("/api/settings");
  setSettings(s);
  return s;
}
