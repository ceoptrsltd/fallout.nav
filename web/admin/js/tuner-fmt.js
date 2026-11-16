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

// ───── the spectrum: which string was plucked, and that string alone ─────

const FFT_TABLES = new Map();
function fftTables(n) {
  let t = FFT_TABLES.get(n);
  if (!t) {
    const rev = new Uint32Array(n);
    const bits = Math.log2(n);
    for (let i = 0; i < n; i++) {
      let r = 0;
      for (let b = 0; b < bits; b++) r |= ((i >> b) & 1) << (bits - 1 - b);
      rev[i] = r;
    }
    const cos = new Float64Array(n / 2), sin = new Float64Array(n / 2);
    for (let i = 0; i < n / 2; i++) { cos[i] = Math.cos((2 * Math.PI * i) / n); sin[i] = Math.sin((2 * Math.PI * i) / n); }
    t = { rev, cos, sin };
    FFT_TABLES.set(n, t);
  }
  return t;
}

/** In-place FFT of re/im (length a power of two); inverse — with the 1/n. */
export function fft(re, im, inverse = false) {
  const n = re.length;
  const { rev, cos, sin } = fftTables(n);
  for (let i = 0; i < n; i++) {
    const j = rev[i];
    if (j > i) { let t = re[i]; re[i] = re[j]; re[j] = t; t = im[i]; im[i] = im[j]; im[j] = t; }
  }
  for (let size = 2; size <= n; size <<= 1) {
    const half = size >> 1, step = n / size;
    for (let i = 0; i < n; i += size) {
      for (let j = 0, k = 0; j < half; j++, k += step) {
        const wr = cos[k], wi = inverse ? sin[k] : -sin[k];
        const xr = re[i + j + half] * wr - im[i + j + half] * wi;
        const xi = re[i + j + half] * wi + im[i + j + half] * wr;
        re[i + j + half] = re[i + j] - xr; im[i + j + half] = im[i + j] - xi;
        re[i + j] += xr; im[i + j] += xi;
      }
    }
  }
  if (inverse) for (let i = 0; i < n; i++) { re[i] /= n; im[i] /= n; }
}

/** Spectrum of a buffer (its length a power of two): {re, im, power (Hann-windowed, bins 0…n/2), n, sampleRate}. */
export function spectrum(buf, sampleRate) {
  const n = buf.length;
  if (!n || (n & (n - 1))) return null;
  const re = Float64Array.from(buf), im = new Float64Array(n);
  fft(re, im);
  const half = n >> 1;
  const power = new Float64Array(half + 1);
  for (let k = 1; k < half; k++) { // the Hann window, applied in the spectrum: X[k]/2 − (X[k−1] + X[k+1])/4
    const r = 0.5 * re[k] - 0.25 * (re[k - 1] + re[k + 1]);
    const i = 0.5 * im[k] - 0.25 * (im[k - 1] + im[k + 1]);
    power[k] = r * r + i * i;
  }
  return { re, im, power, n, sampleRate };
}

/**
 * The note of a power spectrum (of what a pluck added): {hz, clarity} or null. Its peaks are the partials of one
 * string; the note is the highest pitch whose harmonics account for nearly all of them — a lower one (the common
 * period with an octave below) accounts for them too but is not the note, and a higher one (a loud 2nd harmonic
 * taken for the note) leaves the odd partials unexplained.
 * strings (Hz, the tuning): a small microphone may hear nothing but the 2nd harmonic of a thick string — a pitch
 * that is no string, while its octave (or twelfth) below is one, is that string.
 * strict: a pitch more than `farCents` from every string is nothing (handling noise, a voice).
 */
export function nearString(hz, strings, cents) {
  return !!strings && strings.some((f) => Math.abs(centsOff(hz, f)) <= cents);
}

export function combPitch(power, sampleRate, n, { minHz = 60, maxHz = 1100, topHz = 1600, strings = null, strict = false,
  farCents = 200 } = {}) {
  const binHz = sampleRate / n;
  const k0 = Math.max(2, Math.floor(50 / binHz)), k1 = Math.min(power.length - 2, Math.ceil(topHz / binHz));
  let top = 0;
  for (let k = k0; k <= k1; k++) if (power[k] > top) top = power[k];
  if (!(top > 0)) return null;
  const peaks = [];
  for (let k = k0; k <= k1; k++) {
    const p = power[k];
    if (p < 0.01 * top || p < power[k - 1] || p <= power[k + 1]) continue;
    const a = Math.sqrt(power[k - 1]), b0 = Math.sqrt(p), c = Math.sqrt(power[k + 1]);
    const den = a - 2 * b0 + c;
    peaks.push({ hz: (k + (den < 0 ? (0.5 * (a - c)) / den : 0)) * binHz, e: power[k - 1] + p + power[k + 1] });
  }
  if (!peaks.length) return null;
  const total = peaks.reduce((x, q) => x + q.e, 0);
  const strong = peaks.slice().sort((x, y) => y.e - x.e).slice(0, 4);
  const cands = [];
  for (const q of strong) for (let k = 1; k <= 6; k++) { const f = q.hz / k; if (f >= minHz && f <= maxHz) cands.push(f); }
  let best = 0;
  const scored = cands.map((f) => {
    let e = 0, num0 = 0, den0 = 0;
    for (const q of peaks) {
      const m = Math.round(q.hz / f);
      if (m < 1) continue;
      const tol = Math.min(0.2 * f, Math.max(0.7 * binHz, 0.02 * q.hz));
      if (Math.abs(q.hz - m * f) <= tol) { e += q.e; num0 += (q.hz / m) * q.e; den0 += q.e; }
    }
    const share = e / total;
    if (share > best) best = share;
    return { hz: den0 > 0 ? num0 / den0 : f, share };
  });
  if (best < 0.6) return null;
  let pick = null;
  for (const c of scored) if (c.share >= 0.85 * best && (!pick || c.hz > pick.hz)) pick = c;
  if (strings && !nearString(pick.hz, strings, 150)) {
    let low = null;
    for (const c of scored) {
      if (c.hz < pick.hz && harmonicallyRelated(c.hz, pick.hz) && nearString(c.hz, strings, 150) && c.share >= pick.share
        && (!low || c.hz > low.hz)) low = c;
    }
    if (low) pick = low;
  }
  if (strict && strings && !nearString(pick.hz, strings, farCents)) return null;
  return { hz: pick.hz, clarity: pick.share };
}

/**
 * One string out of the mix: the signal with only the bands of the first three partials of `hz` (±150 ¢) left —
 * the lower strings that ring along (and make the sum read an octave or a string down) are gone.
 * Returns the middle of the buffer (the ends ring after such a cut).
 */
export function isolate(spec, hz) {
  const { n, sampleRate } = spec;
  const re = new Float64Array(n), im = new Float64Array(n);
  const binHz = sampleRate / n;
  for (let k = 1; k < n / 2; k++) {
    const f = k * binHz;
    const m = Math.round(f / hz);
    if (m < 1 || m > 3) continue;
    const c = Math.abs(1200 * Math.log2(f / (m * hz)));
    const g = c <= 150 ? 1 : c >= 230 ? 0 : 0.5 * (1 + Math.cos((Math.PI * (c - 150)) / 80));
    if (g <= 0) continue;
    re[k] = spec.re[k] * g; im[k] = spec.im[k] * g;
    re[n - k] = spec.re[n - k] * g; im[n - k] = spec.im[n - k] * g;
  }
  fft(re, im, true);
  const cut = n >> 3;
  return Float32Array.from(re.subarray(cut, n - cut));
}

/**
 * Follows one plucked string among the others that still ring.
 *
 * A guitar is never one clean tone: the strings plucked before go on ringing and the lower ones answer the higher
 * (B is the 3rd harmonic of the low E, the high E its 4th), and a small microphone hears the 2nd or 3rd harmonic of
 * a thick string louder than its fundamental. The period of that sum is not the note. So:
 *   a pluck (the level jumps) — the note is read from what the pluck ADDED to the spectrum (combPitch of the
 *     difference with the spectrum just before it); `need` such readings in a row that agree lock the string;
 *   locked — the pitch is measured on that string alone (isolate), within ±120 ¢ of the lock; the lock follows
 *     the string as the peg is turned;
 *   no pluck — only a clean steady tone (a reference tone) locks or takes over, and never a harmonic relative;
 *   the string fades or is muted — the reading freezes at its steady value and is held for holdMs, then cleared.
 * track(buf, sampleRate, nowMs) → {hz, live, held}; feed({flux, full, near, level, band}, nowMs) is the same on
 * ready readings (near: (hz) => {hz, clarity, level} | null — the reading on the string isolated at hz).
 */
export class PitchTracker {
  constructor({ need = 3, keep = 9, holdMs = 1500, onsetRatio = 2.2, onsetPeak = 0.3, onsetMs = 600, sureClarity = 0.9,
    clearBand = 0.12, tailClarity = 0.3, tailLevel = 0.2, dampRatio = 0.35, fluxShare = 0.3 } = {}) {
    Object.assign(this, { need, keep, holdMs, onsetRatio, onsetPeak, onsetMs, sureClarity, clearBand, tailClarity, tailLevel,
      dampRatio, fluxShare });
    this.strings = null; // Hz of the strings of the tuning (the page sets them): see combPitch
    this.strict = false; // only pitches near those strings (Auto)
    this.floor = null; // the quietest the room has been: readings at that level are noise
    this.reset();
  }

  reset() {
    this.lock = null; // Hz the string is followed at
    this.recent = []; // its last readings: {hz, w}
    this.cand = []; // readings that may become the (next) lock
    this.levels = [];
    this.bands = [];
    this.spectra = []; // the last few power spectra: what was there before a pluck
    this.base = null; // the spectrum just before the last pluck
    this.onsetAt = null;
    this.lastAt = null;
    this.best = 0; // the best clarity of this string so far
    this.peak = 0; // the loudest its own band has been
    this.env = 0; // the loudness lately (falls by half in a second): what a new pluck is measured against
    this.tickAt = null;
    this.last = { full: null, flux: null, near: null, level: 0, onset: false };
  }

  track(buf, sampleRate, now) {
    const level = rms(buf || []);
    this.floor = this.floor === null ? level : Math.min(this.floor * 1.003, level); // rises slowly, falls at once
    const spec = buf && level >= 0.003 ? spectrum(buf, sampleRate) : null;
    const onset = this._onset(level, now);
    // what the pluck added: the spectrum now less (twice) the one just before it
    if (onset && this.base === null) this.base = this.spectra[0] || new Float64Array(spec ? spec.power.length : 0);
    if (this.onsetAt === null || now - this.onsetAt > this.onsetMs) this.base = null;
    let flux = null;
    if (spec && this.base !== null) {
      const d = new Float64Array(spec.power.length);
      let add = 0, all = 0;
      for (let k = 0; k < d.length; k++) {
        const v = spec.power[k] - 2 * (this.base[k] || 0);
        if (v > 0) { d[k] = v; add += v; }
        all += spec.power[k];
      }
      if (all > 0 && add >= this.fluxShare * all) flux = combPitch(d, sampleRate, spec.n, { strings: this.strings, strict: this.strict });
    }
    if (spec) {
      this.spectra.push(spec.power);
      if (this.spectra.length > 4) this.spectra.shift();
    }
    let full = spec ? detectPitch(buf, sampleRate) : null;
    if (full && this.strict && this.strings && !nearString(full.hz, this.strings, 200)) full = null;
    const near = (hz) => {
      if (!spec) return null;
      const one = isolate(spec, hz);
      const p = pickPitch(pitchCurve(one, sampleRate, { minHz: hz / 1.2, minRms: 0 }), { near: hz, nearCents: 120 });
      return p ? { ...p, level: rms(one) } : null;
    };
    return this.feed({ flux, full, near, level, onset, noise: level < 2 * this.floor }, now);
  }

  /** A pluck: the level jumped against the last ~150 ms (and is not just the tail of a string swelling a little). */
  _onset(level, now) {
    const quiet = this.levels.length ? Math.min(...this.levels) : level;
    if (this.tickAt !== null) this.env *= 0.5 ** (Math.max(0, now - this.tickAt) / 1000);
    this.tickAt = now;
    const onset = level > this.onsetRatio * quiet && level > this.onsetPeak * this.env && level > 0.008
      && level > 3 * (this.floor ?? 0);
    if (level > this.env) this.env = level;
    if (onset) this.onsetAt = now;
    this.levels.push(level);
    if (this.levels.length > 4) this.levels.shift();
    return onset;
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

  /** A reading that may become the lock: `need` in a row that agree. */
  _candidate(r, now) {
    if (!r) { this.cand = []; return false; }
    const prev = this.cand[this.cand.length - 1];
    if (prev && Math.abs(centsOff(r.hz, prev)) > 60) this.cand = [];
    this.cand.push(r.hz);
    if (this.cand.length < this.need) return false;
    this.lock = median(this.cand);
    this.cand = [];
    this.recent = [];
    this.bands = [];
    this.best = 0;
    this.peak = 0;
    this.prev = null;
    this.steady = 0;
    this.lastAt = now;
    return true;
  }

  feed({ flux = null, full = null, near = null, level = 0, onset, noise = false } = {}, now) {
    if (onset === undefined) onset = this._onset(level, now);
    const plucked = this.onsetAt !== null && now - this.onsetAt <= this.onsetMs;
    const sure = full && full.clarity >= this.sureClarity ? full : null; // a clean steady tone needs no pluck
    let fresh = false;
    if (this.lock === null) {
      fresh = this._candidate(plucked ? flux : sure, now);
      if (!fresh) { this.last = { full, flux, near: null, level, onset }; return { hz: null, live: false, held: false }; }
    } else {
      // another string? by a pluck — whatever it is; with no pluck — only a clean tone that is no relative of this one
      const r = plucked ? flux : sure && !harmonicallyRelated(sure.hz, this.lock) ? sure : null;
      fresh = this._candidate(r && Math.abs(centsOff(r.hz, this.lock)) > 150 ? r : null, now);
    }
    const nr = typeof near === "function" ? near(this.lock) : near;
    this.last = { full, flux, near: nr, level, onset };
    if (nr) {
      const band = nr.level ?? level;
      const loud = this.bands.length ? Math.max(...this.bands) : band;
      this.bands.push(band);
      if (this.bands.length > 4) this.bands.shift();
      if (band > this.peak) this.peak = band;
      // a reading counts while the string still sounds: its band well above what it fell to (the room's noise through
      // the same band looks like a tone too), fairly clear, and not while the hand mutes it (its level collapses)
      const clear = nr.clarity >= this.best - this.tailClarity && band >= this.tailLevel * this.peak && !noise;
      // and it is where the string was, or moves steadily (the peg) — noise through the band jumps about
      this.steady = this.prev !== null && Math.abs(centsOff(nr.hz, this.prev)) <= 6 ? this.steady + 1 : 0;
      this.prev = nr.hz;
      const near0 = Math.abs(centsOff(nr.hz, this.lock)) <= (this.recent.length ? 20 : 50) || this.steady >= 2;
      if (clear && near0 && band >= this.dampRatio * loud) {
        if (nr.clarity > this.best) this.best = nr.clarity;
        this.recent.push({ hz: nr.hz, w: this._weight(nr.clarity) });
        if (this.recent.length > this.keep) this.recent.shift();
        this.lock = this._estimate();
        this.lastAt = now;
        return { hz: this.lock, live: true, held: false };
      }
    }
    if (!fresh && now - this.lastAt > this.holdMs) {
      this.lock = null;
      this.recent = [];
      this.cand = [];
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
