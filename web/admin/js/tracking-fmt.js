// Tracking page: pure helpers (no DOM) — ranges, stay durations, visit dates. Tested in tests/admin/.

export const RANGES = [
  { value: "24h", label: "24 h", s: 24 * 3600 },
  { value: "7d", label: "7 days", s: 7 * 86400 },
  { value: "30d", label: "30 days", s: 30 * 86400 },
  { value: "all", label: "All", s: null },
];

/** Start of the range in unix seconds, or null for "all time". */
export function rangeFrom(key, nowS) {
  const r = RANGES.find((x) => x.value === key);
  return r && r.s ? Math.floor(nowS - r.s) : null;
}

/** Query string for api/tracking/places|track: "?from=…" or "". */
export function rangeQuery(key, nowS) {
  const from = rangeFrom(key, nowS);
  return from === null ? "" : "?from=" + from;
}

/** Length of a stay: "< 1 min", "45 min", "2 h 10 min", "3 h", "2 d 5 h". */
export function fmtStay(s) {
  if (typeof s !== "number" || !Number.isFinite(s) || s < 0) return "—";
  if (s < 60) return "< 1 min";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min`;
  const hh = Math.floor(m / 60), mm = m % 60;
  if (hh < 24) return mm ? `${hh} h ${mm} min` : `${hh} h`;
  const d = Math.floor(hh / 24), rh = hh % 24;
  return rh ? `${d} d ${rh} h` : `${d} d`;
}

const pad = (n) => String(n).padStart(2, "0");
const WD = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Local date: "Wed 22 Oct 2026". */
export function fmtDay(ts) {
  const d = new Date(ts * 1000);
  return `${WD[d.getDay()]} ${d.getDate()} ${MON[d.getMonth()]} ${d.getFullYear()}`;
}

/** Local time "14:05". */
export function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

const sameDay = (a, b) => {
  const x = new Date(a * 1000), y = new Date(b * 1000);
  return x.getFullYear() === y.getFullYear() && x.getMonth() === y.getMonth() && x.getDate() === y.getDate();
};

/** Short "when": "today 14:05", "yesterday 09:12", "Mon 20 Oct 18:40", "22 Oct 2025 18:40". */
export function fmtWhen(ts, nowS) {
  if (sameDay(ts, nowS)) return "today " + fmtTime(ts);
  if (sameDay(ts, nowS - 86400)) return "yesterday " + fmtTime(ts);
  const d = new Date(ts * 1000), n = new Date(nowS * 1000);
  if (nowS - ts < 6 * 86400 && ts < nowS) return `${WD[d.getDay()]} ${d.getDate()} ${MON[d.getMonth()]} ${fmtTime(ts)}`;
  return `${d.getDate()} ${MON[d.getMonth()]}${d.getFullYear() !== n.getFullYear() ? " " + d.getFullYear() : ""} ${fmtTime(ts)}`;
}

/** Row of the visits table: arrival date/time, departure (with the date if it is another day), duration. */
export function fmtVisit(v) {
  const end = v.end_ts ?? v.start_ts;
  const dur = v.duration_s ?? end - v.start_ts;
  let left = fmtTime(end);
  if (!sameDay(v.start_ts, end)) {
    const d = new Date(end * 1000);
    left = `${WD[d.getDay()]} ${d.getDate()} ${MON[d.getMonth()]} ${left}`;
  }
  return { date: fmtDay(v.start_ts), arrived: fmtTime(v.start_ts), left, duration: fmtStay(dur), passing: dur < 60 };
}

/** "just now", "5 min ago", "3 h ago", "2 d ago". */
export function agoText(ts, nowS) {
  const s = Math.max(0, nowS - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

/** Visits newest first (the server already sorts; keep the page robust). */
export function sortVisits(visits) {
  return [...(visits || [])].sort((a, b) => (b.start_ts ?? 0) - (a.start_ts ?? 0));
}

/** Esri hybrid basemap URL templates (Leaflet placeholders) from settings.tiles, with the API key if set. */
export function esriTemplates(settings) {
  const base = "https://server.arcgisonline.com/ArcGIS/rest/services/";
  const t = (settings && settings.tiles) || {};
  const pick = (k, def) => {
    const u = typeof t[k] === "string" && t[k].includes("{z}") ? t[k] : base + def;
    return t.esri_api_key ? u + (u.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(t.esri_api_key) : u;
  };
  return {
    imagery: pick("imagery_url", "World_Imagery/MapServer/tile/{z}/{y}/{x}"),
    places: pick("places_url", "Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}"),
    transport: pick("transport_url", "Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}"),
  };
}

/** Google Maps link for a point (opens the app on a phone, the site on a laptop). */
export function googleMapsUrl(lat, lon) {
  return "https://www.google.com/maps/search/?api=1&query=" + encodeURIComponent(`${(+lat).toFixed(6)},${(+lon).toFixed(6)}`);
}
