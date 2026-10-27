// Level page: pure helpers (docs/contracts/level.md). Tests: tests/admin/level.test.mjs.

export const LEVEL_EPS = 0.3; // "level": both angles within ±0.3°
export const LEARN_NEED_S = 5; // forward learning: seconds of clean speeding up / braking
export const MEASURE_S = 3; // a level measurement averages this long

/** Board axes for the forward direction. */
export const AXES = [
  { value: "x+", label: "X+" }, { value: "x-", label: "X−" },
  { value: "y+", label: "Y+" }, { value: "y-", label: "Y−" },
  { value: "z+", label: "Z+" }, { value: "z-", label: "Z−" },
];

const num = (v) => typeof v === "number" && Number.isFinite(v);
const r1 = (v) => Math.round(v * 10) / 10;

/** "+1.3°", "-0.8°", "0.0°", "—". */
export function fmtDeg(v) {
  if (!num(v)) return "—";
  const x = r1(v);
  return x === 0 ? "0.0°" : (x > 0 ? "+" : "-") + Math.abs(x).toFixed(1) + "°";
}

/** Within the "level" tolerance (after rounding to 0.1°, as shown). */
export const nearZero = (v) => num(v) && Math.abs(r1(v)) <= LEVEL_EPS + 1e-9;

/** Pitch in words: > 0 — the front is higher. */
export function pitchText(p) {
  if (!num(p)) return "";
  if (nearZero(p)) return "level front to back";
  return p > 0 ? "front is higher" : "rear is higher";
}

/** Roll in words: > 0 — the right side is higher. */
export function rollText(r) {
  if (!num(r)) return "";
  if (nearZero(r)) return "level side to side";
  return r > 0 ? "right side is higher" : "left side is higher";
}

/** Badge of the page head: {text, kind}. */
export function levelBadge(l) {
  if (!l || l.sensor_ok === false) return { text: "no sensor", kind: "bad" };
  if (!num(l.pitch_deg) || !num(l.roll_deg)) return { text: "—", kind: "" };
  const lv = nearZero(l.pitch_deg) && nearZero(l.roll_deg);
  const base = lv ? { text: "level", kind: "ok" } : { text: "tilted", kind: "warn" };
  return l.calibrated === false ? { text: base.text + " · not calibrated", kind: lv ? "warn" : base.kind } : base;
}

/** Raw sensor vector: {parts: ["x +0.023", …], mag: "1.000 g"} or null. */
export function fmtVec(raw) {
  if (!Array.isArray(raw) || raw.length !== 3 || !raw.every(num)) return null;
  const s = (v) => (v < 0 ? "-" : "+") + Math.abs(v).toFixed(3);
  return { parts: ["x", "y", "z"].map((k, i) => `${k} ${s(raw[i])}`), mag: Math.hypot(...raw).toFixed(3) + " g" };
}

/** Wheel raise "2.5 cm" (0.5 cm steps), "0" for the highest wheel, "—" without data. */
export function fmtCm(v) {
  if (!num(v)) return "—";
  const x = Math.round(v * 2) / 2;
  return x <= 0 ? "0" : (Number.isInteger(x) ? String(x) : x.toFixed(1)) + " cm";
}

/** The calibration in words: {level, forward} or null. */
export function calibSummary(cal) {
  if (!cal) return null;
  const level = cal.method === "two_point" ? "precise, two-point" : "single, on level ground";
  const ax = AXES.find((a) => a.value === cal.axis);
  const forward = cal.forward_source === "driving" ? "learned while driving" : ax ? `board axis ${ax.label}` : "board axis";
  return { level, forward };
}

/** Forward learning progress 0…1 (wizard.learn_s of LEARN_NEED_S). */
export function learnFrac(w) {
  return w && num(w.learn_s) ? Math.max(0, Math.min(1, w.learn_s / LEARN_NEED_S)) : 0;
}

/** Calibration API error for the user (409 texts come from the device as they are). */
export function levelErrorText(e) {
  if (!e) return "Unknown error";
  const msg = String(e.message || e);
  if (e.status === 503) return msg && !/^Error \d+$/.test(msg) ? msg : "Level sensor is not connected";
  if (e.status === 404) return "The level is not available on this device yet — update the device software.";
  return msg;
}
