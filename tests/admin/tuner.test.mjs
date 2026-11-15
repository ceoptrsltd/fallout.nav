// Guitar tuner (web/admin/js/tuner-fmt.js): pitch from synthesized plucked strings, notes, strings, steadying.
import test from "node:test";
import assert from "node:assert/strict";
import {
  TUNINGS, tuningById, noteHz, centsOff, nearestNote, noteLabel, nearestString, stringNumber, tuneLevel, tuneHint,
  fmtCents, needleDeg, detectPitch, decimate2, PitchTracker, NeedleSpring, trimmedMean, clampA4, median,
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

test("tracker: waits for agreeing readings, follows a string, holds a faded note, starts over on another string", () => {
  const tr = new PitchTracker({ need: 3, holdMs: 1000 });
  assert.deepEqual(tr.feed(110, 0), { hz: null, live: false, held: false });
  tr.feed(110.2, 50);
  const a = tr.feed(109.9, 100);
  assert.ok(a.live && Math.abs(a.hz - 110) < 0.2);
  assert.equal(tr.feed(220.5, 150).live, false); // an octave jump — a single reading is not trusted
  const held = tr.feed(null, 600);
  assert.ok(held.held && !held.live && Math.abs(held.hz - 110) < 0.2);
  assert.deepEqual(tr.feed(null, 1300), { hz: null, live: false, held: false });
  tr.feed(147, 1400);
  tr.feed(147.1, 1450);
  assert.ok(Math.abs(tr.feed(146.9, 1500).hz - 147) < 0.2);
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
