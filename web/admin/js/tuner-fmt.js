// Guitar tuner (pages/tuner.js): the pitch of a plucked string from the microphone and how far it is from the string
// it should be. Pure functions — the audio graph and the DOM live in the page.
//
// Pitch: YIN (de Cheveigné & Kawahara, 2002) — the difference function of the signal with itself shifted by τ,
// normalised by its running mean; the first dip under the threshold is the period (the fundamental, not a harmonic),
// refined between samples by a parabola. PitchTracker locks on the plucked string and follows it among the others
// that still ring; calibrated by a recording of a real guitar (tests/admin/tuner.test.mjs).

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
 * The YIN curve of a buffer: {cm, tauMin, tauMax, sampleRate, level} or null (too quiet, too short). cm[τ] is the
 * normalised difference of the signal with itself shifted by τ samples: near 0 at a period, about 1 elsewhere.
 * The buffer must hold at least two periods of minHz.
 */
export function pitchCurve(buf, sampleRate, { minHz = 60, maxHz = 1100, minRms = 0.006 } = {}) {
  if (!buf || !num(sampleRate)) return null;
  const level = rms(buf);
  if (level < minRms) return null;
  const tauMin = Math.max(2, Math.floor(sampleRate / maxHz));
  const tauMax = Math.min(Math.ceil(sampleRate / minHz), Math.floor(buf.length / 2));
  if (tauMax <= tauMin + 2) return null;
  const w = buf.length - tauMax;
  const cm = new Float32Array(tauMax + 1);
  cm[0] = 1;
  let run = 0;
  for (let tau = 1; tau <= tauMax; tau++) {
    let s = 0;
    for (let i = 0; i < w; i++) {
      const x = buf[i] - buf[i + tau];
      s += x * x;
    }
    run += s;
    cm[tau] = run > 0 ? (s * tau) / run : 1; // cumulative mean normalised difference: d(τ)·τ / Σ d(1…τ)
  }
  return { cm, tauMin, tauMax, sampleRate, level };
}

/**
 * The pitch on a YIN curve: {hz, clarity 0…1} or null.
 *   The whole range: the shortest period whose dip is about as deep as the deepest one (within `slack`) — a longer
 *   period (an octave down, or the common period of several ringing strings) dips as deep but is not the note, and
 *   a shallower dip at half the period (a loud 2nd harmonic) is not it either. No dip under `floor` — nothing.
 *   near (Hz): only within ±nearCents of that pitch — following a string that is already known, however the other
 *   strings ring; the dip may be shallower (nearFloor).
 */
export function pickPitch(curve, { near = null, nearCents = 150, slack = 0.06, floor = 0.32, nearFloor = 0.55 } = {}) {
  if (!curve) return null;
  const { cm, tauMin, tauMax, sampleRate } = curve;
  const last = tauMax - 1; // the parabola needs a neighbour on each side
  let tau = -1;
  if (num(near) && near > 0) {
    const k = 2 ** (nearCents / 1200);
    const lo = Math.max(tauMin, Math.floor(sampleRate / (near * k)));
    const hi = Math.min(last, Math.ceil((sampleRate * k) / near));
    if (hi <= lo) return null;
    let best = lo;
    for (let t = lo + 1; t <= hi; t++) if (cm[t] < cm[best]) best = t;
    // an edge is not a dip: the period lies outside the window
    if (cm[best] > nearFloor || (best === lo && cm[lo - 1] < cm[lo]) || (best === hi && cm[hi + 1] < cm[hi])) return null;
    tau = best;
  } else {
    let g = tauMin;
    for (let t = tauMin + 1; t <= last; t++) if (cm[t] < cm[g]) g = t;
    if (cm[g] > floor) return null;
    const lim = cm[g] + slack;
    for (let t = tauMin; t <= last; t++) {
      if (cm[t] <= lim) {
        while (t + 1 <= last && cm[t + 1] < cm[t]) t++;
        tau = t;
        break;
      }
    }
    if (tau < 0) return null;
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

/** Pitch of a buffer (YIN): {hz, clarity 0…1} or null — too quiet, or no clear period between minHz and maxHz. */
export function detectPitch(buf, sampleRate, opts = {}) {
  return pickPitch(pitchCurve(buf, sampleRate, opts), opts);
}

/** Two pitches a whole number of times apart (2…6, within 60 ¢): one string's harmonics, or its "octave down". */
export function harmonicallyRelated(a, b) {
  const r = a > b ? a / b : b / a;
  const k = Math.round(r);
  return k >= 2 && k <= 6 && Math.abs(centsOff(r, k)) < 60;
}

/** Median of a few numbers. */
export function median(xs) {
  const s = xs.slice().sort((a, b) => a - b);
  const n = s.length;
  return n ? (n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2) : null;
}

/**
 * Follows one plucked string among the others that still ring.
 *
 * A guitar is never one clean tone: the strings plucked before go on ringing and the lower ones answer the higher
 * (B is the 3rd harmonic of the low E, the high E its 4th). A thin string is loud for a third of a second; after
 * that the sum of everything has a longer common period and a plain detector reads «an octave down» or another
 * string. So the tracker locks on the string that was plucked and then looks for the pitch only next to it:
 *   no lock — the whole range; `need` readings in a row that agree make the lock;
 *   locked — the pitch within ±150 ¢ of the lock; the lock follows the string as the peg is turned;
 *   another string — `need` whole-range readings in a row away from the lock; a pitch that is a harmonic relative
 *     of the lock (an octave down…) counts only right after a pluck (the level jumped);
 *   the string fades — the reading freezes at its steady value (the tail is the least exact part) and is held for
 *     holdMs, then cleared.
 * track(buf, sampleRate, nowMs) → {hz, live, held}; feed({full, near, level}, nowMs) is the same on ready readings.
 */
export class PitchTracker {
  constructor({ need = 3, keep = 9, holdMs = 1500, onsetRatio = 2.2, onsetPeak = 0.3, onsetMs = 600, sureClarity = 0.9,
    clearBand = 0.12, tailClarity = 0.3, tailLevel = 0.15, dampRatio = 0.35 } = {}) {
    Object.assign(this, { need, keep, holdMs, onsetRatio, onsetPeak, onsetMs, sureClarity, clearBand, tailClarity, tailLevel, dampRatio });
    this.reset();
  }

  reset() {
    this.lock = null; // Hz the string is followed at
    this.recent = []; // its last readings: {hz, w}
    this.cand = []; // whole-range readings that may become the (next) lock
    this.levels = [];
    this.onsetAt = null;
    this.lastAt = null;
    this.lockAt = null;
    this.best = 0; // the best clarity of this string so far
    this.peak = 0; // its loudest level
    this.env = 0; // the loudness lately (falls by half in a second): what a new pluck is measured against
    this.tickAt = null;
    this.last = { full: null, near: null, level: 0, onset: false };
  }

  track(buf, sampleRate, now) {
    const curve = pitchCurve(buf, sampleRate);
    const full = pickPitch(curve);
    const near = this.lock !== null ? pickPitch(curve, { near: this.lock }) : null;
    return this.feed({ full, near, level: curve ? curve.level : rms(buf || []) }, now);
  }

  _weight(clarity) {
    const w = Math.max(0.05, clarity - 0.45);
    return w * w; // a clear reading counts many times a murky one
  }

  _estimate() {
    const s = this.recent.slice().sort((x, y) => x.hz - y.hz);
    const k = s.length >= 4 ? 1 : 0; // without the highest and the lowest
    let sum = 0, ws = 0;
    for (let i = k; i < s.length - k; i++) { sum += s[i].hz * s[i].w; ws += s[i].w; }
    return sum / ws;
  }

  _candidate(full, now, ok, level) {
    if (!full || !ok) { this.cand = []; return false; }
    const prev = this.cand[this.cand.length - 1];
    if (prev && Math.abs(centsOff(full.hz, prev.hz)) > 60) this.cand = [];
    this.cand.push(full);
    if (this.cand.length < this.need) return false;
    // the very first reading of a pluck is sharp (the attack): the lock starts from the ones after it
    this.recent = this.cand.slice(1).map((c) => ({ hz: c.hz, w: this._weight(c.clarity) }));
    this.cand = [];
    this.lock = this._estimate();
    this.best = full.clarity;
    this.peak = Math.max(level, this.env);
    this.lastAt = now;
    this.lockAt = now;
    return true;
  }

  feed({ full = null, near = null, level = 0 } = {}, now) {
    // a pluck: the level jumped against the last ~150 ms (and is not just the tail of the string swelling a little)
    const quiet = this.levels.length ? Math.min(...this.levels) : level;
    const loud = this.levels.length ? Math.max(...this.levels) : level;
    if (this.tickAt !== null) this.env *= 0.5 ** (Math.max(0, now - this.tickAt) / 1000);
    this.tickAt = now;
    const onset = level > this.onsetRatio * quiet && level > this.onsetPeak * this.env && level > 0.008;
    if (level > this.env) this.env = level;
    if (onset) this.onsetAt = now;
    this.levels.push(level);
    if (this.levels.length > 4) this.levels.shift();
    this.last = { full, near, level, onset };
    // a pluck just now — and, with a string locked, one after that lock (not the pluck that made it)
    const plucked = this.onsetAt !== null && now - this.onsetAt <= this.onsetMs
      && (this.lock === null || this.onsetAt - this.lockAt > 150);
    const sure = full && full.clarity >= this.sureClarity; // a clean steady tone needs no pluck

    if (this.lock === null) {
      if (this._candidate(full, now, plucked || sure, level)) return { hz: this.lock, live: true, held: false };
      return { hz: null, live: false, held: false };
    }
    if (level > this.peak) this.peak = level;
    // another string?
    const away = full && Math.abs(centsOff(full.hz, this.lock)) > 150;
    if (this._candidate(full, now, away && (plucked || (sure && !harmonicallyRelated(full.hz, this.lock))), level)) {
      return { hz: this.lock, live: true, held: false };
    }
    // the same string: follow it while the reading is fairly clear and the string is not being muted by the hand
    const damped = level < this.dampRatio * loud;
    // a murky reading counts only while the string is still fairly loud: the dying tail is not the pitch any more
    const clear = near && (near.clarity >= this.best - this.clearBand
      || (near.clarity >= this.best - this.tailClarity && level >= this.tailLevel * this.peak));
    if (clear && !damped) {
      if (near.clarity > this.best) this.best = near.clarity;
      this.recent.push({ hz: near.hz, w: this._weight(near.clarity) });
      if (this.recent.length > this.keep) this.recent.shift();
      this.lock = this._estimate();
      this.lastAt = now;
      return { hz: this.lock, live: true, held: false };
    }
    if (now - this.lastAt > this.holdMs) {
      this.reset();
      return { hz: null, live: false, held: false };
    }
    return { hz: this.lock, live: false, held: true };
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
