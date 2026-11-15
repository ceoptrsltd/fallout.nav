// Air quality charts (docs/contracts/air.md), plain SVG: a sparkline for the Dashboard tile and the 7-day chart of
// the Air page. The line is coloured by the zone it runs through (a vertical gradient: green — fine, amber —
// raised, red — off the scale); the faint band behind it is the min…max of each time bucket.
import { bandPath, chartRange, dayMarks, linePath, seriesStats, zoneLines, zoneStops } from "./air-fmt.js";

const NS = "http://www.w3.org/2000/svg";
const COLOR = { good: "var(--green)", warn: "var(--amber)", bad: "var(--red)" };
let uid = 0;

function svg(name, attrs = {}, ...kids) {
  const el = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) if (v != null) el.setAttribute(k, v);
  el.append(...kids.filter((k) => k != null));
  return el;
}

/** A vertical gradient in user space from y=0 (hi) to y=h (lo), coloured by the metric's zones. Returns its id. */
function zoneGradient(defs, m, lo, hi, h) {
  const id = `airg${++uid}`;
  const g = svg("linearGradient", { id, gradientUnits: "userSpaceOnUse", x1: 0, y1: 0, x2: 0, y2: h });
  for (const s of zoneStops(m, lo, hi)) g.append(svg("stop", { offset: s.offset.toFixed(3), "stop-color": COLOR[s.level] }));
  defs.append(g);
  return id;
}

/** Tiny line of the last hour: values — per-minute averages (null — a gap). */
export function sparkline(m, values, w = 96, h = 26) {
  const st = seriesStats({ min: values, avg: values, max: values });
  const { lo, hi } = chartRange(m, st);
  const defs = svg("defs");
  const root = svg("svg", { class: "air-spark", viewBox: `0 0 ${w} ${h}`, width: w, height: h, "aria-hidden": "true" }, defs);
  if (!st) return root;
  const pad = 2;
  const g = svg("g", { transform: `translate(0 ${pad})` });
  const grad = zoneGradient(defs, m, lo, hi, h - 2 * pad);
  g.append(svg("path", { d: linePath(values, w, h - 2 * pad, lo, hi), fill: "none", stroke: `url(#${grad})`,
    "stroke-width": 1.8, "stroke-linecap": "round", "stroke-linejoin": "round" }));
  root.append(g);
  return root;
}

/**
 * 7-day chart: hist — AirHistory ({t0, step_s, n, <metric>: {min, avg, max}}), width in CSS px.
 * Left: the scale (top, bottom and the zone borders); bottom: days of the week at their midnights.
 */
export function weekChart(m, hist, width, height = 170) {
  const s = hist?.[m.key];
  const st = seriesStats(s);
  const L = 38, R = 8, T = 8, B = 20;
  const w = Math.max(60, width - L - R), h = height - T - B;
  const { lo, hi } = chartRange(m, st);
  const defs = svg("defs");
  const root = svg("svg", { class: "air-chart", viewBox: `0 0 ${width} ${height}`, width, height, role: "img",
    "aria-label": `${m.label}, last 7 days` }, defs);
  const plot = svg("g", { transform: `translate(${L} ${T})` });
  root.append(plot);
  const Y = (v) => h - ((v - lo) / (hi - lo || 1)) * h;

  // days: separators at midnights, weekday names under the middle of each day
  const t0 = hist?.t0 ?? 0, t1 = t0 + (hist?.n ?? 0) * (hist?.step_s ?? 1);
  const X = (t) => ((t - t0) / (t1 - t0 || 1)) * w;
  const marks = hist ? dayMarks(t0, t1) : [];
  marks.forEach((d, i) => {
    plot.append(svg("line", { x1: X(d.t), y1: 0, x2: X(d.t), y2: h, class: "air-day" }));
    const end = marks[i + 1]?.t ?? t1;
    if (X(end) - X(d.t) > 26) plot.append(svg("text", { x: (X(d.t) + X(end)) / 2, y: h + 14, class: "air-ax", "text-anchor": "middle" }, d.label));
  });
  plot.append(svg("rect", { x: 0, y: 0, width: w, height: h, class: "air-frame" }));

  // the scale: range ends and zone borders (dashed, in the colour of the zone above)
  const fmt = (v) => (Math.abs(v) >= 100 ? Math.round(v) : v.toFixed(m.digits ? 1 : 0));
  plot.append(svg("text", { x: -6, y: 4, class: "air-ax", "text-anchor": "end" }, fmt(hi)));
  plot.append(svg("text", { x: -6, y: h, class: "air-ax", "text-anchor": "end" }, fmt(lo)));
  for (const z of zoneLines(m, lo, hi)) {
    const y = Y(z.value);
    plot.append(svg("line", { x1: 0, y1: y, x2: w, y2: y, class: "air-zone", stroke: COLOR[z.level] }));
    if (y > 14 && y < h - 10) plot.append(svg("text", { x: -6, y: y + 4, class: "air-ax", "text-anchor": "end", fill: COLOR[z.level] }, fmt(z.value)));
  }
  if (!st) {
    plot.append(svg("text", { x: w / 2, y: h / 2 + 4, class: "air-none", "text-anchor": "middle" }, "no data yet"));
    return root;
  }
  const grad = zoneGradient(defs, m, lo, hi, h);
  plot.append(svg("path", { d: bandPath(s.min, s.max, w, h, lo, hi), fill: `url(#${grad})`, opacity: 0.2 }));
  plot.append(svg("path", { d: linePath(s.avg, w, h, lo, hi), fill: "none", stroke: `url(#${grad})`, "stroke-width": 2,
    "stroke-linecap": "round", "stroke-linejoin": "round" }));
  return root;
}
