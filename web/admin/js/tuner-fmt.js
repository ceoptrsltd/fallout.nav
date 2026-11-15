// Guitar tuner (pages/tuner.js): the pitch of a plucked string from the microphone and how far it is from the string
// it should be. Pure functions — the audio graph and the DOM live in the page.
//
// Pitch: YIN (de Cheveigné & Kawahara, 2002) — the difference function of the signal with itself shifted by τ,
// normalised by its running mean; the first dip under the threshold is the period (the fundamental, not a harmonic),
// refined between samples by a parabola. A plucked string is noisy for the first ~50 ms and fades out: PitchTracker
// waits for a few readings that agree before it shows a note, smooths the needle and holds the last reading a moment.

export const NOTE_NAMES = ["C", "C♯", "D", "D♯", "E", "F", "F♯", "G", "G♯", "A", "A♯", "B"];

/** Tunings: strings from the 6th (lowest) to the 1st, as MIDI notes (E2 = 40). */
export const TUNINGS = [
  { id: "standard", label: "Standard", notes: [40, 45, 50, 55, 59, 64] },
  { id: "drop-d", label: "Drop D", notes: [38, 45, 50, 55, 59, 64] },
  { id: "half-down", label: "Half step down", notes: [39, 44, 49, 54, 58, 63] },
  { id: "full-down", label: "Full step down", notes: [38, 43, 48, 53, 57, 62] },
  { id: "drop-c", label: "Drop C", notes: [36, 43, 48, 53, 57, 62] },
  { id: "dadgad", label: "DADGAD", notes: [38, 45, 50, 55, 57, 62] },
  { id: "open-g", label: "Open G", notes: [38, 43, 50, 55, 59, 62] },
  { id: "open-d", label: "Open D", notes: [38, 45, 50, 54, 57, 62] },
];
export const tuningById = (id) => TUNINGS.find((t) => t.id === id) || TUNINGS[0];

export const A4_DEFAULT = 440;
export const A4_MIN = 430;
export const A4_MAX = 450;
export const IN_TUNE_CENTS = 3; // the needle is green inside ±3 ¢ (the ear hears ~5 ¢)
export const CLOSE_CENTS = 10; // amber up to ±10 ¢
export const NEEDLE_CENTS = 50; // the scale ends at ±50 ¢ (a quarter tone): further — "tune up / down"

const num = (v) => typeof v === "number" && Number.isFinite(v);

/** Frequency of a MIDI note at the reference A4. */
export function noteHz(midi, a4 = A4_DEFAULT) {
  return a4 * 2 ** ((midi - 69) / 12);
}

/** Cents from `ref` Hz to `hz` (+ — sharp, − — flat). */
export function centsOff(hz, ref) {
  return 1200 * Math.log2(hz / ref);
}

/** Nearest note of a frequency: {midi, name, octave, cents} (cents from that note, −50…+50); null for no frequency. */
export function nearestNote(hz, a4 = A4_DEFAULT) {
  if (!num(hz) || hz <= 0) return null;
  const exact = 69 + 12 * Math.log2(hz / a4);
  const midi = Math.round(exact);
  return { midi, name: NOTE_NAMES[((midi % 12) + 12) % 12], octave: Math.floor(midi / 12) - 1, cents: (exact - midi) * 100 };
}

/** «E2», «F♯3». */
export function noteLabel(midi) {
  return NOTE_NAMES[((midi % 12) + 12) % 12] + (Math.floor(midi / 12) - 1);
}

/** Index (0 = 6th string) of the string of the tuning closest to the frequency, in cents. */
export function nearestString(hz, notes, a4 = A4_DEFAULT) {
  let best = -1;
  let bestAbs = Infinity;
  notes.forEach((m, i) => {
    const c = Math.abs(centsOff(hz, noteHz(m, a4)));
    if (c < bestAbs) { bestAbs = c; best = i; }
  });
  return best;
}

/** String number as on the guitar: index 0 (the lowest) — 6, index 5 — 1. */
export const stringNumber = (i) => 6 - i;

/** good (in tune) / close / off by the cents. */
export function tuneLevel(cents) {
  if (!num(cents)) return "";
  const a = Math.abs(cents);
  return a <= IN_TUNE_CENTS ? "good" : a <= CLOSE_CENTS ? "close" : "off";
}

/** What to do with the peg: {text, dir}; dir: "up" (tighten), "down" (loosen), "" — in tune. */
export function tuneHint(cents) {
  if (!num(cents)) return { text: "", dir: "" };
  if (Math.abs(cents) <= IN_TUNE_CENTS) return { text: "In tune", dir: "" };
  return cents < 0 ? { text: "Tune up", dir: "up" } : { text: "Tune down", dir: "down" };
}

/** «−6 ¢», «+12 ¢», «0 ¢»; a semitone and more (a string far out, or the wrong one picked) — «+3 semitones». */
export function fmtCents(c) {
  if (!num(c)) return "— ¢";
  if (Math.abs(c) >= 99.5) {
    const st = Math.round(Math.abs(c) / 100);
    return (c > 0 ? "+" : "−") + st + (st === 1 ? " semitone" : " semitones");
  }
  const r = Math.round(c);
  return (r > 0 ? "+" : r < 0 ? "−" : "") + Math.abs(r) + " ¢";
}

/** Needle angle, degrees: ±50 ¢ → ±60°, clamped. */
export function needleDeg(cents, span = 60) {
  if (!num(cents)) return 0;
  return Math.max(-1, Math.min(1, cents / NEEDLE_CENTS)) * span;
}

/** Root mean square of a buffer. */
export function rms(buf) {
  let s = 0;
  for (let i = 0; i < buf.length; i++) s += buf[i] * buf[i];
  return Math.sqrt(s / (buf.length || 1));
}

/** Every 2nd sample, averaged with its neighbour (the page low-passes the signal first): half the work for YIN. */
export function decimate2(buf, out) {
  const n = buf.length >> 1;
  const o = out && out.length === n ? out : new Float32Array(n);
  for (let i = 0; i < n; i++) o[i] = (buf[2 * i] + buf[2 * i + 1]) * 0.5;
  return o;
}

/**
 * Pitch of a buffer (YIN): {hz, clarity 0…1} or null — too quiet, or no clear period between minHz and maxHz.
 * The buffer must hold at least two periods of minHz.
 */
export function detectPitch(buf, sampleRate, { minHz = 60, maxHz = 1100, threshold = 0.12, minRms = 0.006 } = {}) {
  if (!buf || !num(sampleRate) || rms(buf) < minRms) return null;
  const tauMin = Math.max(2, Math.floor(sampleRate / maxHz));
  const tauMax = Math.min(Math.ceil(sampleRate / minHz), Math.floor(buf.length / 2));
  if (tauMax <= tauMin + 2) return null;
  const w = buf.length - tauMax;
  const d = new Float32Array(tauMax + 1);
  for (let tau = 1; tau <= tauMax; tau++) {
    let s = 0;
    for (let i = 0; i < w; i++) {
      const x = buf[i] - buf[i + tau];
      s += x * x;
    }
    d[tau] = s;
  }
  // cumulative mean normalised difference: d'(τ) = d(τ)·τ / Σ d(1…τ)
  const cm = new Float32Array(tauMax + 1);
  cm[0] = 1;
  let run = 0;
  for (let tau = 1; tau <= tauMax; tau++) {
    run += d[tau];
    cm[tau] = run > 0 ? (d[tau] * tau) / run : 1;
  }
  const last = tauMax - 1; // the parabola needs a neighbour on each side
  let tau = -1;
  for (let t = tauMin; t <= last; t++) {
    if (cm[t] < threshold) {
      while (t + 1 <= last && cm[t + 1] < cm[t]) t++;
      tau = t;
      break;
    }
  }
  if (tau < 0) { // nothing under the threshold: the deepest dip, if it is still a fair one
    let best = tauMin;
    for (let t = tauMin + 1; t <= last; t++) if (cm[t] < cm[best]) best = t;
    if (cm[best] > 0.3) return null;
    tau = best;
  }
  // between samples: the vertex of the parabola through the dip and its neighbours
  let t = tau;
  if (tau > 1 && tau < tauMax) {
    const a = cm[tau - 1], b = cm[tau], c = cm[tau + 1];
    const den = a - 2 * b + c;
    if (den > 0) t = tau + (0.5 * (a - c)) / den;
  }
  return { hz: sampleRate / t, clarity: Math.max(0, Math.min(1, 1 - cm[tau])) };
}

/** Median of a few numbers. */
export function median(xs) {
  const s = xs.slice().sort((a, b) => a - b);
  const n = s.length;
  return n ? (n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2) : null;
}

/**
 * Steadies the raw readings of a plucked string. feed(hz | null, nowMs) → {hz, live, held}:
 *   hz — the median of the last readings that agree (null — nothing to show);
 *   live — a fresh reading now; held — the string has faded: the last note is held for holdMs, then cleared.
 * A reading that jumps more than a semitone away from the recent ones starts over (a new string was plucked).
 */
export class PitchTracker {
  constructor({ need = 3, keep = 7, holdMs = 1500 } = {}) {
    this.need = need;
    this.keep = keep;
    this.holdMs = holdMs;
    this.reset();
  }

  reset() {
    this.recent = [];
    this.hz = null;
    this.lastAt = null;
  }

  feed(hz, now) {
    if (num(hz) && hz > 0) {
      const last = this.recent[this.recent.length - 1];
      if (last && Math.abs(centsOff(hz, last)) > 100) this.recent = []; // another string
      this.recent.push(hz);
      if (this.recent.length > this.keep) this.recent.shift();
      if (this.recent.length >= this.need || (this.hz && Math.abs(centsOff(hz, this.hz)) < 100)) {
        this.hz = trimmedMean(this.recent);
        this.lastAt = now;
        return { hz: this.hz, live: true, held: false };
      }
      return { hz: this.hz, live: false, held: this.hz !== null };
    }
    if (this.hz !== null && this.lastAt !== null && now - this.lastAt > this.holdMs) {
      this.reset();
      return { hz: null, live: false, held: false };
    }
    if (this.recent.length && this.lastAt !== null && now - this.lastAt > 250) this.recent = [];
    return { hz: this.hz, live: false, held: this.hz !== null };
  }
}

/**
 * Mean of the readings without the highest and the lowest (from 4 on): steadier than the median, which jumps from
 * one reading to another, and a single wild reading still does not count.
 */
export function trimmedMean(xs) {
  if (!xs.length) return null;
  const s = xs.slice().sort((a, b) => a - b);
  const k = s.length >= 4 ? 1 : 0;
  const mid = s.slice(k, s.length - k);
  return mid.reduce((a, b) => a + b, 0) / mid.length;
}

/**
 * The needle as on a meter: a critically damped spring — it swings to a new value without overshoot and settles in
 * about 4.7 / omega seconds, the same at any frame rate. step(target, dt s) → the position.
 * omega 7.5 — ~0.6 s to a new note; the page lets it fall back to the middle slower when the string has died away.
 */
export class NeedleSpring {
  constructor(omega = 7.5) {
    this.omega = omega;
    this.x = 0;
    this.v = 0;
  }

  reset(x = 0) {
    this.x = x;
    this.v = 0;
  }

  step(target, dt) {
    if (!num(target)) return this.x;
    let left = Math.max(0, Math.min(0.25, num(dt) ? dt : 0)); // a stall (a hidden tab) does not throw the needle
    while (left > 0) {
      const h = Math.min(left, 1 / 120); // small steps keep the integration stable
      const a = this.omega * this.omega * (target - this.x) - 2 * this.omega * this.v;
      this.v += a * h;
      this.x += this.v * h;
      left -= h;
    }
    return this.x;
  }
}

/** The reference A4 from the input, kept within A4_MIN…A4_MAX; nonsense — the default. */
export function clampA4(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return A4_DEFAULT;
  return Math.round(Math.max(A4_MIN, Math.min(A4_MAX, n)));
}
