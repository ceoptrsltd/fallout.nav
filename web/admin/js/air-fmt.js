// Air quality sensor (docs/contracts/air.md): what the numbers mean and how a chart is laid out. Pure functions —
// the DOM lives in air-chart.js, pages/air.js and the Dashboard tile.

/** Zones go up the scale: [upper bound, level]; the last one is open. Levels: good / warn / bad. */
export const METRICS = [
  { key: "co2_ppm", label: "CO₂", unit: "ppm", digits: 0, span: 300,
    zones: [[1000, "good"], [1500, "warn"], [Infinity, "bad"]], hint: "above 1000 — air out, above 1500 — stuffy" },
  { key: "temp_c", label: "Temperature", short: "Temp", unit: "°C", digits: 1, span: 4,
    zones: [[15, "bad"], [18, "warn"], [26, "good"], [30, "warn"], [Infinity, "bad"]], hint: "comfortable at 18–26 °C" },
  { key: "humidity_pct", label: "Humidity", unit: "%", digits: 0, span: 15,
    zones: [[20, "bad"], [30, "warn"], [60, "good"], [70, "warn"], [Infinity, "bad"]], hint: "comfortable at 30–60 %" },
  { key: "pm25", label: "PM2.5", unit: "µg/m³", digits: 0, span: 20,
    zones: [[15, "good"], [35, "warn"], [Infinity, "bad"]], hint: "fine dust: up to 15 is clean air" },
  { key: "pm10", label: "PM10", unit: "µg/m³", digits: 0, span: 40,
    zones: [[45, "good"], [100, "warn"], [Infinity, "bad"]], hint: "coarse dust: up to 45 is clean air" },
];
export const metric = (key) => METRICS.find((m) => m.key === key);

const num = (v) => typeof v === "number" && Number.isFinite(v);

/** good / warn / bad for a value; "" when there is none. */
export function levelOf(m, v) {
  if (!num(v)) return "";
  for (const [hi, lvl] of m.zones) if (v < hi) return lvl;
  return m.zones[m.zones.length - 1][1];
}

/** «812», «21.5»; no value — «—». */
export function fmtValue(m, v) {
  return num(v) ? v.toFixed(m.digits) : "—";
}

/** Min / average / max of a history series {min[], avg[], max[]} over the buckets that have data; null — no data. */
export function seriesStats(s) {
  const lo = (s?.min || []).filter(num), hi = (s?.max || []).filter(num), av = (s?.avg || []).filter(num);
  if (!av.length) return null;
  return { min: Math.min(...lo), max: Math.max(...hi), avg: av.reduce((a, b) => a + b, 0) / av.length };
}

/**
 * The value range a chart shows: the data with a little room, never narrower than the metric's `span`
 * (a flat line in the middle instead of noise blown up to full height).
 */
export function chartRange(m, stats) {
  if (!stats) return { lo: 0, hi: m.span };
  let lo = stats.min, hi = stats.max;
  const pad = Math.max((hi - lo) * 0.1, m.span * 0.05);
  lo -= pad;
  hi += pad;
  if (hi - lo < m.span) {
    const mid = (hi + lo) / 2;
    lo = mid - m.span / 2;
    hi = mid + m.span / 2;
  }
  if (m.key !== "temp_c") lo = Math.max(0, lo);
  return { lo, hi };
}

/**
 * Colour stops of a vertical gradient over the range lo…hi (offset 0 — the top, hi): each zone has its level, the
 * colours blend over `blend` of the range around a zone border. → [{offset, level}], offsets ascending.
 */
export function zoneStops(m, lo, hi, blend = 0.04) {
  const span = hi - lo || 1;
  const off = (v) => Math.max(0, Math.min(1, (hi - v) / span));
  const stops = [];
  let below = -Infinity;
  for (const [top, lvl] of m.zones) {
    if (top > lo && below < hi) { // the zone is (partly) in view: from its top border down to its bottom border
      const a = off(Math.min(hi, top)) + (top < hi ? blend / 2 : 0);
      const b = off(Math.max(lo, below)) - (below > lo ? blend / 2 : 0);
      stops.push({ offset: Math.min(a, b), level: lvl }, { offset: Math.max(a, b), level: lvl });
    }
    below = top;
  }
  return stops.sort((x, y) => x.offset - y.offset);
}

/** Zone borders inside the range: [{value, level}] — the level is the zone above the border. */
export function zoneLines(m, lo, hi) {
  const out = [];
  for (let i = 0; i < m.zones.length - 1; i++) {
    const v = m.zones[i][0];
    if (v > lo && v < hi) out.push({ value: v, level: m.zones[i + 1][1] });
  }
  return out;
}

/**
 * SVG path of a line through values (null — a gap) in a box w×h: x spreads evenly, y from lo (bottom) to hi (top).
 * A lone point between two gaps becomes a short dash so it is still visible.
 */
export function linePath(values, w, h, lo, hi) {
  const n = values.length;
  if (!n) return "";
  const X = (i) => (n === 1 ? w / 2 : (i * w) / (n - 1));
  const Y = (v) => h - ((v - lo) / (hi - lo || 1)) * h;
  let d = "";
  let open = false;
  for (let i = 0; i < n; i++) {
    const v = values[i];
    if (!num(v)) {
      open = false;
      continue;
    }
    const x = X(i).toFixed(1), y = Y(v).toFixed(1);
    if (open) d += `L${x} ${y}`;
    else if (!num(values[i + 1])) d += `M${(X(i) - 1).toFixed(1)} ${y}L${(X(i) + 1).toFixed(1)} ${y}`;
    else d += `M${x} ${y}`;
    open = num(values[i + 1]);
  }
  return d;
}

/** SVG path of the band between two series (min below, max above); gaps split it into pieces. */
export function bandPath(mins, maxs, w, h, lo, hi) {
  const n = Math.min(mins.length, maxs.length);
  const X = (i) => (n <= 1 ? w / 2 : (i * w) / (n - 1));
  const Y = (v) => h - ((v - lo) / (hi - lo || 1)) * h;
  let d = "";
  let run = [];
  const flush = () => {
    if (run.length >= 2) {
      d += "M" + run.map((i) => `${X(i).toFixed(1)} ${Y(maxs[i]).toFixed(1)}`).join("L")
        + "L" + run.slice().reverse().map((i) => `${X(i).toFixed(1)} ${Y(mins[i]).toFixed(1)}`).join("L") + "Z";
    }
    run = [];
  };
  for (let i = 0; i < n; i++) {
    if (num(mins[i]) && num(maxs[i])) run.push(i);
    else flush();
  }
  flush();
  return d;
}

/** Midnights (local time of the browser) inside t0…t1 (unix s): [{t, label}] — «MON», the first of a month «1 DEC». */
export function dayMarks(t0, t1) {
  const out = [];
  const d = new Date(t0 * 1000);
  d.setHours(24, 0, 0, 0);
  for (; d.getTime() / 1000 < t1; d.setDate(d.getDate() + 1)) {
    out.push({ t: d.getTime() / 1000, label: d.toLocaleDateString("en-GB", { weekday: "short" }).toUpperCase() });
  }
  return out;
}

/** «2 min ago», «just now»; no reading — «no data». */
export function fmtAge(s) {
  if (!num(s)) return "no data";
  if (s < 90) return "just now";
  return s < 5400 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
}
