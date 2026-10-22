// Pure helpers for the Radio and Bluetooth pages (docs/contracts/radio.md); tested in tests/admin.

export const GENRE_GROUPS = [
  { kind: "mood", title: "Moods" },
  { kind: "genre", title: "Genres" },
  { kind: "era", title: "Eras & charts" },
];

/** Genre tiles from api/radio/genres grouped as in the contract (moods → genres → eras), order kept. */
export function groupGenres(genres) {
  const list = Array.isArray(genres) ? genres : [];
  return GENRE_GROUPS.map((g) => ({ ...g, items: list.filter((x) => x && x.kind === g.kind) })).filter((g) => g.items.length);
}

/** "MP3 · 128 kbps" / "AAC" / "" (bitrate 0 = unknown). */
export function codecText(st) {
  if (!st) return "";
  const parts = [];
  if (st.codec) parts.push(String(st.codec).toUpperCase());
  if (st.bitrate > 0) parts.push(st.bitrate + " kbps");
  return parts.join(" · ");
}

/** Up to n directory tags of a station, without empty/duplicate ones. */
export function stationTags(st, n = 3) {
  const seen = new Set();
  const out = [];
  for (const t of String((st && st.tags) || "").split(",")) {
    const v = t.trim().toLowerCase();
    if (!v || seen.has(v)) continue;
    seen.add(v);
    out.push(v);
    if (out.length >= n) break;
  }
  return out;
}

/** The same station? (uuid when both have one, otherwise the stream URL.) */
export function sameStation(a, b) {
  if (!a || !b) return false;
  if (a.uuid && b.uuid) return a.uuid === b.uuid;
  return !!a.url && a.url === b.url;
}

const STATE_TEXT = {
  playing: { text: "On air", kind: "ok" },
  connecting: { text: "Tuning in…", kind: "warn" },
  waiting_output: { text: "Waiting for the speaker", kind: "warn" },
  error: { text: "Error", kind: "bad" },
  stopped: { text: "Off", kind: "" },
};
/** Player state → badge text and kind. */
export function radioStateText(state) {
  return STATE_TEXT[state] || { text: state ? String(state) : "—", kind: "" };
}

/** One-line explanation of a non-playing state for the now-playing card. */
export function radioStateHint(st) {
  if (!st) return "";
  switch (st.state) {
    case "connecting": return "Opening the stream… (a weak mobile connection can take a few seconds)";
    case "waiting_output": return st.output && st.output.name
      ? `The speaker “${st.output.name}” is not connected. The station starts as soon as it connects.`
      : "No speaker is selected. Connect a Bluetooth speaker and the station starts by itself.";
    case "error": return st.error || "Playback failed";
    case "stopped": return st.station ? "The radio is off." : "Pick a genre or search for a station.";
    default: return "";
  }
}

/** RSSI (dBm) → 0…4 bars; null/undefined → null (not seen in the last scan). */
export function rssiBars(rssi) {
  if (typeof rssi !== "number" || !Number.isFinite(rssi)) return null;
  if (rssi >= -55) return 4;
  if (rssi >= -67) return 3;
  if (rssi >= -78) return 2;
  if (rssi >= -90) return 1;
  return 0;
}

/** BlueZ "Icon" property → icon name in dom.js. */
export function btIconName(dev) {
  const i = String((dev && dev.icon) || "");
  if (i.startsWith("audio-headset") || i.startsWith("audio-headphones")) return "headset";
  if (i.startsWith("audio")) return "speaker";
  if (i.startsWith("phone")) return "phone";
  if (i.startsWith("computer") || i.startsWith("video")) return "pc";
  return dev && dev.audio ? "speaker" : "bt";
}

/** Display name: the device name or its address. */
export function btName(dev) {
  return (dev && dev.name && String(dev.name).trim()) || (dev && dev.address) || "Unknown device";
}

/** Order: default speaker, connected, paired, audio, then signal (strong first), then name. */
export function sortDevices(devices, audioDevice) {
  const score = (d) => (d.address === audioDevice ? 16 : 0) + (d.connected ? 8 : 0) + (d.paired ? 4 : 0) + (d.audio ? 2 : 0);
  return [...(devices || [])].sort((a, b) =>
    score(b) - score(a)
    || (b.rssi ?? -999) - (a.rssi ?? -999)
    || btName(a).localeCompare(btName(b)));
}

/** Named devices and paired ones are shown; the rest (nameless beacons from a scan) are folded away. */
export function splitDevices(devices) {
  const shown = [], folded = [];
  for (const d of devices || []) (d.name || d.paired || d.connected ? shown : folded).push(d);
  return { shown, folded };
}

/** Debounce with a trailing call; .flush() runs a pending call now, .cancel() drops it. */
export function debounce(fn, ms) {
  let t = null, lastArgs = null;
  const run = () => { t = null; const a = lastArgs; lastArgs = null; if (a) fn(...a); };
  const d = (...args) => { lastArgs = args; clearTimeout(t); t = setTimeout(run, ms); };
  d.flush = () => { if (t) { clearTimeout(t); run(); } };
  d.cancel = () => { clearTimeout(t); t = null; lastArgs = null; };
  return d;
}
