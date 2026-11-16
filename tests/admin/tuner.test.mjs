// Guitar tuner (web/admin/js/tuner-fmt.js): pitch from synthesized plucked strings, notes, strings, steadying.
import test from "node:test";
import assert from "node:assert/strict";
import {
  TUNINGS, tuningById, noteHz, centsOff, nearestNote, noteLabel, nearestString, stringNumber, tuneLevel, tuneHint,
  fmtCents, needleDeg, detectPitch, pitchCurve, pickPitch, harmonicallyRelated, decimate2, PitchTracker, fft, spectrum,
  combPitch, isolate, nearString, NeedleSpring, trimmedMean, clampA4, median,
} from "../../web/admin/js/tuner-fmt.js";

/** A plucked string: the fundamental and its harmonics (the 2nd often louder than the 1st), decaying, with noise. */
function pluck(hz, sr, n, { harmonics = [0.6, 1.0, 0.5, 0.35, 0.2, 0.12], noise = 0.01, t0 = 0.15, seed = 1 } = {}) {
  const out = new Float32Array(n);
  let r = seed;
  const rnd = () => ((r = (r * 1103515245 + 12345) % 2 ** 31) / 2 ** 31) * 2 - 1;
  for (let i = 0; i < n; i++) {
    const t = t0 + i / sr;
    let v = 0;
    harmonics.forEach((a, k) => { v += a * Math.exp(-t * (1.2 + k)) * Math.sin(2 * Math.PI * hz * (k + 1) * t + k); });
    out[i] = 0.2 * v + noise * rnd();
  }
  return out;
}

test("tunings: six strings each, from the 6th to the 1st; unknown id — standard", () => {
  for (const t of TUNINGS) assert.equal(t.notes.length, 6, t.id);
  assert.deepEqual(TUNINGS[0].notes.map(noteLabel), ["E2", "A2", "D3", "G3", "B3", "E4"]);
  assert.equal(tuningById("nope").id, "standard");
  assert.deepEqual(tuningById("drop-d").notes.map(noteLabel), ["D2", "A2", "D3", "G3", "B3", "E4"]);
});

test("notes: frequencies, cents, the nearest note", () => {
  assert.equal(noteHz(69), 440);
  assert.ok(Math.abs(noteHz(40) - 82.407) < 0.001); // E2
  assert.ok(Math.abs(noteHz(64) - 329.628) < 0.001); // E4
  assert.ok(Math.abs(noteHz(69, 432) - 432) < 1e-9);
  assert.ok(Math.abs(centsOff(440 * 2 ** (10 / 1200), 440) - 10) < 1e-9);
  const n = nearestNote(110 * 2 ** (-7 / 1200));
  assert.equal(n.name + n.octave, "A2");
  assert.ok(Math.abs(n.cents + 7) < 1e-6);
  assert.equal(nearestNote(185).name, "F♯");
  assert.equal(nearestNote(0), null);
  assert.equal(nearestNote(null), null);
});

test("strings: the nearest one of the tuning; numbers as on the guitar", () => {
  const std = TUNINGS[0].notes;
  assert.equal(nearestString(83, std), 0);
  assert.equal(nearestString(107, std), 1);
  assert.equal(nearestString(250, std), 4);
  assert.equal(nearestString(320, std), 5);
  assert.equal(stringNumber(0), 6);
  assert.equal(stringNumber(5), 1);
});

test("levels, hints, cents text, needle", () => {
  assert.equal(tuneLevel(2.9), "good");
  assert.equal(tuneLevel(-8), "close");
  assert.equal(tuneLevel(30), "off");
  assert.equal(tuneLevel(null), "");
  assert.deepEqual(tuneHint(-20), { text: "Tune up", dir: "up" });
  assert.deepEqual(tuneHint(20), { text: "Tune down", dir: "down" });
  assert.equal(tuneHint(1).text, "In tune");
  assert.equal(fmtCents(-6.4), "−6 ¢");
  assert.equal(fmtCents(12.2), "+12 ¢");
  assert.equal(fmtCents(0.2), "0 ¢");
  assert.equal(fmtCents(undefined), "— ¢");
  assert.equal(fmtCents(1494), "+15 semitones");
  assert.equal(fmtCents(-104), "−1 semitone");
  assert.equal(fmtCents(99.4), "+99 ¢");
  assert.equal(needleDeg(25), 30);
  assert.equal(needleDeg(-500), -60);
  assert.equal(needleDeg(null), 0);
});

test("pitch: every string of every tuning within 1 cent, at 48 kHz decimated (the page's path) and at 44.1 kHz", () => {
  const notes = new Set(TUNINGS.flatMap((t) => t.notes));
  for (const sr of [48000, 44100]) {
    for (const m of notes) {
      for (const detune of [-30, 0, 17]) {
        const hz = noteHz(m) * 2 ** (detune / 1200);
        const buf = decimate2(pluck(hz, sr, 4096, { seed: m + detune }));
        const p = detectPitch(buf, sr / 2);
        assert.ok(p, `${noteLabel(m)} ${detune} ¢ at ${sr}: nothing`);
        const err = centsOff(p.hz, hz);
        assert.ok(Math.abs(err) < 1, `${noteLabel(m)} ${detune} ¢ at ${sr}: off by ${err.toFixed(2)} ¢`);
      }
    }
  }
});

test("pitch: a weak fundamental (2nd harmonic louder) is still the fundamental, not an octave up", () => {
  const hz = noteHz(40); // low E: on a small speaker / phone mic the fundamental is weak
  const p = detectPitch(decimate2(pluck(hz, 48000, 4096, { harmonics: [0.15, 1.0, 0.7, 0.4] })), 24000);
  assert.ok(p && Math.abs(centsOff(p.hz, hz)) < 1, p && p.hz);
});

test("pitch: silence and noise give nothing", () => {
  assert.equal(detectPitch(new Float32Array(2048), 24000), null);
  let r = 7;
  const noise = Float32Array.from({ length: 2048 }, () => ((r = (r * 1103515245 + 12345) % 2 ** 31) / 2 ** 31 - 0.5) * 0.4);
  assert.equal(detectPitch(noise, 24000), null);
  assert.equal(detectPitch(null, 24000), null);
});

// A guitar as the microphone hears it (calibrated by a recording of a real one): strings plucked before still ring,
// the low ones answer the high ones, and a thin string is loud only for a moment.
const SR = 24000;
function ring(parts, secs) {
  // parts: [{hz, at (s), amp, decay (1/s), harmonics}] → one signal
  const out = new Float32Array(Math.round(secs * SR));
  let r = 5;
  const rnd = () => ((r = (r * 1103515245 + 12345) % 2 ** 31) / 2 ** 31) * 2 - 1;
  for (let i = 0; i < out.length; i++) {
    const t = i / SR;
    let v = 0.0015 * rnd();
    for (const p of parts) {
      if (t < p.at) continue;
      const e = p.amp * Math.exp(-(t - p.at) * p.decay) * Math.min(1, (t - p.at) / 0.004);
      (p.harmonics || [1, 0.5, 0.3]).forEach((a, k) => { v += e * a * Math.sin(2 * Math.PI * p.hz * (k + 1) * t + k); });
    }
    out[i] = v;
  }
  return out;
}
/** What the page does: a 2048-sample window every 45 ms → the tracker. */
function follow(sig, tracker = new PitchTracker()) {
  const rows = [];
  for (let end = 2048; end <= sig.length; end += Math.round(0.045 * SR)) {
    const t = end / SR;
    rows.push({ t, ...tracker.track(sig.subarray(end - 2048, end), SR, t * 1000) });
  }
  return rows;
}

test("pitch: among other ringing strings the plain reading goes an octave or a string down — near the string it holds", () => {
  const e4 = noteHz(64) * 2 ** (4 / 1200);
  // the high E half a second after its pluck: quieter than the low E and the A that answer it
  const sig = ring([{ hz: e4, at: 0, amp: 0.08, decay: 0 }, { hz: noteHz(40), at: 0, amp: 0.03, decay: 0 }, { hz: noteHz(45), at: 0, amp: 0.02, decay: 0 }], 0.2);
  const buf = sig.subarray(0, 2048);
  const plain = detectPitch(buf, SR);
  assert.ok(!plain || Math.abs(centsOff(plain.hz, e4)) > 100, "the plain reading is not the high E");
  const near = pickPitch(pitchCurve(buf, SR), { near: noteHz(64) });
  assert.ok(near && Math.abs(centsOff(near.hz, e4)) < 12, "near the string: " + (near && centsOff(near.hz, e4)));
  assert.equal(pickPitch(pitchCurve(buf, SR), { near: 500 }), null); // nothing rings there
  assert.equal(pickPitch(null), null);
});

test("pitch: a 2nd harmonic louder than the fundamental is not read an octave up", () => {
  const hz = noteHz(50); // D3 on a laptop microphone
  const p = detectPitch(ring([{ hz, at: -1, amp: 0.1, decay: 0, harmonics: [0.35, 1, 0.2] }], 0.1).subarray(0, 2048), SR);
  assert.ok(p && Math.abs(centsOff(p.hz, hz)) < 2, p && p.hz);
  assert.ok(harmonicallyRelated(82.4, 329.6) && harmonicallyRelated(247, 82.4) && harmonicallyRelated(196, 98));
  assert.ok(!harmonicallyRelated(110, 146.8) && !harmonicallyRelated(196, 247) && !harmonicallyRelated(110, 112));
});

test("tracker on sound: the plucked thin string is kept while the low ones ring on; a new pluck takes over", () => {
  const e4 = noteHz(64) * 2 ** (5 / 1200), e2 = noteHz(40), a2 = noteHz(45), g3 = noteHz(55) * 2 ** (-8 / 1200);
  const sig = ring([
    { hz: e2, at: 0, amp: 0.012, decay: 0.2 }, { hz: a2, at: 0, amp: 0.01, decay: 0.2 }, // ringing since before
    { hz: e4, at: 1.0, amp: 0.25, decay: 6 }, // loud for a moment
    { hz: g3, at: 3.0, amp: 0.2, decay: 1.5 },
    { hz: e2, at: 5.0, amp: 0.12, decay: 0.8, harmonics: [0.5, 1, 0.6, 0.4] }, // the low E after the G: a pluck again
  ], 7);
  const rows = follow(sig);
  const span = (t0, t1) => rows.filter((r) => r.t >= t0 && r.t < t1);
  assert.ok(span(0.2, 1.0).every((r) => r.hz === null), "a faint murky ring with no pluck locks nothing");
  const high = span(1.25, 2.4);
  assert.ok(high.length > 20 && high.every((r) => r.hz !== null && Math.abs(centsOff(r.hz, e4)) < 6),
    "the high E all along: " + high.map((r) => (r.hz ? centsOff(r.hz, e4).toFixed(0) : "-")).join(" "));
  assert.ok(high.some((r) => r.held), "its tail is held, not followed into the other strings");
  const g = span(3.3, 4.2);
  assert.ok(g.every((r) => r.live && Math.abs(centsOff(r.hz, g3)) < 3), "the G within 0.3 s of its pluck: " + g.map((r) => (r.hz ? centsOff(r.hz, g3).toFixed(0) : "-") + (r.live ? "" : "h")).join(" "));
  const low = span(5.35, 6.5);
  assert.ok(low.every((r) => r.hz !== null && Math.abs(centsOff(r.hz, e2)) < 4), "the low E — a harmonic relative, taken by its pluck");
});

test("tracker on readings: lock by a pluck or a clean tone, relatives ignored, hold then clear", () => {
  const rd = (hz, clarity = 0.95, level) => ({ hz, clarity, ...(level === undefined ? {} : { level }) });
  let tr = new PitchTracker({ holdMs: 1000 });
  let t = 0;
  const tick = (o) => tr.feed(o, (t += 45));
  // murky and no pluck (a tail of something) — nothing
  for (let i = 0; i < 6; i++) assert.equal(tick({ full: rd(82.4, 0.75), level: 0.01 }).hz, null);
  // a pluck: the level jumps; three readings of what it added lock the string; the pitch is then the one near it
  tick({ level: 0.2 });
  tick({ flux: rd(248), level: 0.2 });
  tick({ flux: rd(247.5), level: 0.15 });
  let o = tick({ flux: rd(247), near: rd(246.9, 0.98, 0.1), level: 0.12 });
  assert.ok(o.live && Math.abs(o.hz - 246.9) < 0.01, String(o.hz));
  // the tail: the plain reading says a third and a half of the pitch — the lock stays, by the reading near it
  for (let i = 0; i < 8; i++) o = tick({ full: rd(i % 2 ? 82.3 : 123.5, 0.95), near: rd(246.9, 0.9, 0.05), level: 0.05 });
  assert.ok(o.live && Math.abs(o.hz - 246.9) < 0.3, String(o.hz));
  // noise through the string's band jumps about: not followed
  for (const c of [40, -35, 60, -50]) o = tick({ near: rd(246.9 * 2 ** (c / 1200), 0.95, 0.04), level: 0.04 });
  assert.ok(o.held && Math.abs(o.hz - 246.9) < 0.3, String(o.hz));
  // the string has died away (its band far below its loudest): frozen
  o = tick({ near: rd(250, 0.95, 0.004), level: 0.01 });
  assert.ok(o.held && Math.abs(o.hz - 246.9) < 0.3);
  // held for holdMs after the last good reading, then cleared
  for (let i = 0; i < 24; i++) o = tick({ level: 0.004 });
  assert.deepEqual(o, { hz: null, live: false, held: false });

  // a clean steady tone (a reference tone, no pluck) locks; another clean one that is no relative takes over
  tr = new PitchTracker();
  t = 0;
  const nearOf = (hz) => (lock) => (Math.abs(centsOff(hz, lock)) < 120 ? rd(hz, 0.97, 0.05) : null);
  for (let i = 0; i < 4; i++) o = tick({ full: rd(110, 0.97), near: nearOf(110), level: 0.05 });
  assert.ok(o.live && Math.abs(o.hz - 110) < 0.01);
  for (let i = 0; i < 4; i++) o = tick({ full: rd(220, 0.97), near: nearOf(110), level: 0.05 }); // its octave: a relative
  assert.ok(Math.abs(o.hz - 110) < 0.01);
  for (let i = 0; i < 4; i++) o = tick({ full: rd(146.8, 0.97), near: nearOf(146.8), level: 0.05 });
  assert.ok(o.live && Math.abs(o.hz - 146.8) < 0.01);
  // a peg turned: the lock follows the string
  let hz = 146.8;
  for (let i = 0; i < 40; i++) { hz *= 2 ** (3 / 1200); o = tick({ full: rd(hz, 0.97), near: nearOf(hz), level: 0.05 }); }
  assert.ok(o.live && Math.abs(centsOff(o.hz, hz)) < 20, "follows 120 ¢ of tuning: " + centsOff(o.hz, hz));
  // the hand mutes the string: its level collapses — the reading of that moment does not count
  o = tick({ near: rd(hz * 1.03, 0.95, 0.005), level: 0.005 });
  assert.ok(o.held);
});

test("spectrum: fft there and back; the note of what a pluck added; one string out of the mix", () => {
  const re = Float64Array.from({ length: 64 }, (_, i) => Math.sin(i * 0.7) + 0.3 * Math.cos(i * 2.1)), im = new Float64Array(64);
  const src = re.slice();
  fft(re, im);
  fft(re, im, true);
  for (let i = 0; i < 64; i++) assert.ok(Math.abs(re[i] - src[i]) < 1e-9 && Math.abs(im[i]) < 1e-9);
  assert.equal(spectrum(new Float32Array(1000), SR), null); // not a power of two

  const std = TUNINGS[0].notes.map((m) => noteHz(m));
  const pw = (parts) => spectrum(ring(parts, 0.1).subarray(0, 2048), SR).power;
  // a D whose microphone hears the 2nd harmonic almost alone: 294 Hz is no string, the D an octave below is
  const d = pw([{ hz: noteHz(50), at: -1, amp: 0.1, decay: 0, harmonics: [0.03, 1, 0.05, 0.4] }]);
  assert.ok(Math.abs(combPitch(d, SR, 2048).hz - 2 * noteHz(50)) < 3, "the spectrum alone says 294");
  assert.ok(Math.abs(centsOff(combPitch(d, SR, 2048, { strings: std, strict: true }).hz, noteHz(50))) < 20, "with the tuning — D3");
  // the high E: its harmonics are harmonics of the low E and of the A too — the highest pitch that explains them
  const e = pw([{ hz: noteHz(64), at: -1, amp: 0.1, decay: 0 }]);
  assert.ok(Math.abs(centsOff(combPitch(e, SR, 2048, { strings: std, strict: true }).hz, noteHz(64))) < 20);
  // the low E heard without its fundamental: still the low E, not the B or the high E inside it
  const lo = pw([{ hz: noteHz(40), at: -1, amp: 0.1, decay: 0, harmonics: [0.02, 0.6, 1, 0.7, 0.5, 0.4] }]);
  assert.ok(Math.abs(centsOff(combPitch(lo, SR, 2048, { strings: std, strict: true }).hz, noteHz(40))) < 25);
  // between two strings, far from both (a slack string, a knock): nothing in Auto; by hand — the pitch itself
  const v = pw([{ hz: 127, at: -1, amp: 0.1, decay: 0 }]);
  assert.equal(combPitch(v, SR, 2048, { strings: std, strict: true }), null);
  assert.ok(Math.abs(combPitch(v, SR, 2048, { strings: [noteHz(45)] }).hz - 127) < 2);
  assert.ok(nearString(250, std, 150) && !nearString(294, std, 150));

  // the high E under the low E and the A: alone again after isolate()
  const e4 = noteHz(64) * 2 ** (4 / 1200);
  const mix = ring([{ hz: e4, at: -1, amp: 0.03, decay: 0 }, { hz: noteHz(40), at: -1, amp: 0.05, decay: 0 }, { hz: noteHz(45), at: -1, amp: 0.04, decay: 0 }], 0.1).subarray(0, 2048);
  const one = isolate(spectrum(mix, SR), noteHz(64));
  const p1 = pickPitch(pitchCurve(one, SR, { minRms: 0 }), { near: noteHz(64), nearCents: 120 });
  assert.ok(p1 && p1.clarity > 0.9 && Math.abs(centsOff(p1.hz, e4)) < 12, JSON.stringify(p1));
});

test("tracker on sound: a D heard by its 2nd harmonic, plucked again and again, never becomes another string", () => {
  const d3 = noteHz(50) * 2 ** (4 / 1200);
  const hs = [0.04, 1, 0.08, 0.35];
  const sig = ring([0.5, 1.6, 2.7, 3.8].map((at) => ({ hz: d3, at, amp: 0.2, decay: 2.5, harmonics: hs })), 5);
  const tr = new PitchTracker();
  tr.strings = TUNINGS[0].notes.map((m) => noteHz(m));
  tr.strict = true;
  const rows = follow(sig, tr).filter((r) => r.hz !== null);
  assert.ok(rows.length > 60, "shown most of the time: " + rows.length);
  assert.ok(rows.every((r) => Math.abs(centsOff(r.hz, d3)) < 8), "always the D: " + rows.map((r) => centsOff(r.hz, d3).toFixed(0)).join(" "));
});

test("small helpers: median, smoothing, A4 bounds", () => {
  assert.equal(median([3, 1, 2]), 2);
  assert.equal(median([4, 1, 2, 3]), 2.5);
  assert.equal(median([]), null);
  assert.equal(trimmedMean([10, 1, 2, 3]), 2.5); // without 1 and 10
  assert.equal(trimmedMean([4, 6]), 5);
  assert.equal(trimmedMean([]), null);
  assert.equal(clampA4("442"), 442);
  assert.equal(clampA4(500), 450);
  assert.equal(clampA4("x"), 440);
});

test("needle: swings to a new value smoothly, without overshoot, the same at 30 and 60 frames a second", () => {
  const run = (fps, secs) => {
    const n = new NeedleSpring();
    const path = [];
    for (let i = 0; i < fps * secs; i++) path.push(n.step(40, 1 / fps));
    return path;
  };
  const p60 = run(60, 1.2);
  assert.ok(p60[0] < 1, "no jump on the first frame: " + p60[0]);
  for (let i = 1; i < p60.length; i++) assert.ok(p60[i] >= p60[i - 1] - 1e-9 && p60[i] <= 40 + 1e-6, "monotonic, no overshoot");
  const half = p60.findIndex((x) => x >= 20) / 60;
  assert.ok(half > 0.12 && half < 0.3, "half way in " + half + " s");
  assert.ok(p60[p60.length - 1] > 39, "settled within ~0.6 s");
  const p30 = run(30, 1.2);
  assert.ok(Math.abs(p30[14] - p60[29]) < 0.5, "frame-rate independent");
  const n = new NeedleSpring();
  n.step(40, 10); // a long stall moves it at most a quarter second's worth
  assert.ok(n.x < 30);
  n.reset(0);
  assert.equal(n.step(null, 0.1), 0);
});
