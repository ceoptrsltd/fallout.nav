// Уровень на экране: web/display/ui/level.js, docs/contracts/level.md («Экран»).
import test from "node:test";
import assert from "node:assert/strict";

import {
  HIDE_ABOVE_KMH, LEVEL_BOX, SHOW_AFTER_S, axisLevel, exaggerate, fmtAngle, fmtCm, isLevel, levelInputs,
  levelVisibility, newLevelTrack, pitchWord, rollWord, wheelRaises,
} from "../../web/display/ui/level.js";
import { SPEED_BOX } from "../../web/display/ui/speed.js";
import { RADIO_H } from "../../web/display/ui/radio.js";

test("fmtAngle: signed, one decimal, no negative zero, dashes without data", () => {
  assert.equal(fmtAngle(2.44), "+2.4°");
  assert.equal(fmtAngle(-1.25), "-1.2°");
  assert.equal(fmtAngle(-0.04), "0.0°");
  assert.equal(fmtAngle(0), "0.0°");
  assert.equal(fmtAngle(12.5), "+12.5°");
  assert.equal(fmtAngle(null), "--.-°");
  assert.equal(fmtAngle(NaN), "--.-°");
});

test("isLevel: both angles within 0.3° (as shown, rounded to 0.1°)", () => {
  assert.equal(isLevel(0.3, -0.3), true);
  assert.equal(isLevel(0.34, 0), true, "shown as 0.3");
  assert.equal(isLevel(0.35, 0), false, "shown as 0.4");
  assert.equal(isLevel(0, -0.5), false);
  assert.equal(isLevel(null, 0), false);
  assert.equal(axisLevel(-0.2), true);
});

test("words: which side is higher", () => {
  assert.equal(pitchWord(1.8), "NOSE UP");
  assert.equal(pitchWord(-0.6), "NOSE DOWN");
  assert.equal(pitchWord(0.2), "LEVEL");
  assert.equal(pitchWord(null), "PITCH");
  assert.equal(rollWord(1.2), "RIGHT HIGH");
  assert.equal(rollWord(-1.2), "LEFT HIGH");
  assert.equal(rollWord(-0.1), "LEVEL");
  assert.equal(rollWord(undefined), "ROLL");
});

test("exaggerate: small angles visible, monotonic, saturating, odd", () => {
  assert.ok(exaggerate(1) >= 4 && exaggerate(1) <= 6, `1° → ${exaggerate(1)}`);
  assert.ok(exaggerate(2) >= 8, `2° → ${exaggerate(2)}`);
  let prev = -Infinity;
  for (let a = -40; a <= 40; a += 0.5) {
    const v = exaggerate(a);
    assert.ok(v > prev, "monotonic");
    assert.ok(Math.abs(v) < 22.001, "bounded");
    prev = v;
  }
  assert.equal(exaggerate(-3), -exaggerate(3));
  assert.equal(exaggerate(0), 0);
  assert.equal(exaggerate(null), 0);
});

test("wheel raises: non-zero only, 0.5 cm steps", () => {
  assert.deepEqual(wheelRaises({ fl: 0, fr: 3.5, rl: 11, rr: 14.5 }), { fr: "3.5", rl: "11", rr: "14.5" });
  assert.deepEqual(wheelRaises({ fl: 0.2, fr: 0.3, rl: null, rr: 2.26 }), { fr: "0.5", rr: "2.5" });
  assert.deepEqual(wheelRaises(null), {});
  assert.equal(fmtCm(4), "4");
  assert.equal(fmtCm(4.74), "4.5");
});

test("visibility: < 1 km/h for 3 s shows, > 3 km/h hides at once, 1…3 keeps", () => {
  const t = newLevelTrack();
  const on = { enabled: true, sensorOk: true };
  assert.equal(levelVisibility(t, { ...on, now: 0, speedKmh: 0 }).shown, false);
  assert.equal(levelVisibility(t, { ...on, now: SHOW_AFTER_S - 0.1, speedKmh: 0.5 }).shown, false);
  assert.equal(levelVisibility(t, { ...on, now: SHOW_AFTER_S, speedKmh: 0.2 }).shown, true);
  assert.equal(levelVisibility(t, { ...on, now: 4, speedKmh: 2.5 }).shown, true, "creeping at 2.5 km/h keeps it");
  assert.equal(levelVisibility(t, { ...on, now: 4.1, speedKmh: HIDE_ABOVE_KMH + 0.1 }).shown, false, "gone at once");
  assert.equal(levelVisibility(t, { ...on, now: 4.2, speedKmh: 2 }).shown, false, "1…3 km/h does not bring it back");
  assert.equal(levelVisibility(t, { ...on, now: 5, speedKmh: 0 }).shown, false);
  assert.equal(levelVisibility(t, { ...on, now: 7.5, speedKmh: 1.5 }).shown, false, "the 3 s restart after ≥ 1 km/h");
  assert.equal(levelVisibility(t, { ...on, now: 8, speedKmh: 0 }).shown, false);
  assert.equal(levelVisibility(t, { ...on, now: 11, speedKmh: 0 }).shown, true);
});

test("visibility: unknown speed counts as stopped; disabled or no sensor hides", () => {
  const t = newLevelTrack();
  const on = { enabled: true, sensorOk: true };
  levelVisibility(t, { ...on, now: 0, speedKmh: null });
  assert.equal(levelVisibility(t, { ...on, now: 3, speedKmh: null }).shown, true);
  assert.equal(levelVisibility(t, { ...on, now: 3.1, speedKmh: 0, sensorOk: false }).shown, false);
  assert.equal(levelVisibility(t, { ...on, now: 3.2, speedKmh: 0 }).shown, false, "sensor back: wait 3 s again");
  assert.equal(levelVisibility(t, { ...on, now: 6.2, speedKmh: 0 }).shown, true);
  assert.equal(levelVisibility(t, { ...on, now: 6.3, speedKmh: 0, enabled: false }).shown, false);
});

test("levelInputs: from the HUD state (km/h, settings.level.show_on_screen default on)", () => {
  const st = (level, settings = {}, speedMps = 0.1) => ({ now: 5, telemetry: { level }, settings, view: { speedMps } });
  const i = levelInputs(st({ sensor_ok: true }));
  assert.deepEqual({ ...i, speedKmh: Math.round(i.speedKmh * 100) / 100 }, { now: 5, speedKmh: 0.36, enabled: true, sensorOk: true, dr: false });
  assert.equal(levelInputs(st({ sensor_ok: true }, { level: { show_on_screen: false } })).enabled, false);
  assert.equal(levelInputs(st(null)).sensorOk, false);
  assert.equal(levelInputs(st({ sensor_ok: true }, {}, null)).speedKmh, null);
});

test("levelInputs: no link to fnav-core — the level data is stale too, the speedometer stays (with \"--\")", () => {
  const st = (connected) => ({ now: 5, telemetry: { level: { sensor_ok: true } }, settings: {}, view: { speedMps: null, connected } });
  assert.equal(levelInputs(st(true)).sensorOk, true);
  assert.equal(levelInputs(st(undefined)).sensorOk, true, "gallery / older states without the flag");
  assert.equal(levelInputs(st(false)).sensorOk, false);
  const t = newLevelTrack();
  levelVisibility(t, levelInputs(st(false)));
  assert.equal(levelVisibility(t, { ...levelInputs(st(false)), now: 10 }).shown, false);
});

test("levelInputs: in a tunnel (dead reckoning) the speed is unknown but we are moving — the speedometer stays", () => {
  const st = (deadReckoning) => ({ now: 5, telemetry: { level: { sensor_ok: true } }, settings: {}, view: { speedMps: null, deadReckoning } });
  assert.equal(levelInputs(st(true)).dr, true);
  assert.equal(levelInputs(st(undefined)).dr, false);
  const t = newLevelTrack();
  levelVisibility(t, levelInputs(st(true)));
  assert.equal(levelVisibility(t, { ...levelInputs(st(true)), now: 30 }).shown, false);
  levelVisibility(t, { ...levelInputs(st(false)), now: 30 });
  assert.equal(levelVisibility(t, { ...levelInputs(st(false)), now: 33 }).shown, true, "window over, still no speed: as before");
});

test("LEVEL_BOX: as wide as the speedometer (the radio panel does not move), taller than it", () => {
  assert.equal(LEVEL_BOX.w, SPEED_BOX.w);
  assert.ok(LEVEL_BOX.h > SPEED_BOX.h && LEVEL_BOX.h > RADIO_H);
  assert.ok(LEVEL_BOX.h <= 260, "leaves the left column above it to the clock and warnings");
});
