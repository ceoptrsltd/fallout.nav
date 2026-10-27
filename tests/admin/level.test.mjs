// Level page: pure helpers (web/admin/js/level-fmt.js), docs/contracts/level.md.
import test from "node:test";
import assert from "node:assert/strict";

import {
  AXES, fmtDeg, nearZero, pitchText, rollText, levelBadge, fmtVec, fmtCm, calibSummary, learnFrac, levelErrorText,
  fmtSigned, signMeaning, parseDeg, toggleSign, degError, hasLevelRef,
} from "../../web/admin/js/level-fmt.js";

test("fmtDeg: signed, 0.1°, no negative zero", () => {
  assert.equal(fmtDeg(1.26), "+1.3°");
  assert.equal(fmtDeg(-0.84), "-0.8°");
  assert.equal(fmtDeg(-0.04), "0.0°");
  assert.equal(fmtDeg(0), "0.0°");
  assert.equal(fmtDeg(null), "—");
});

test("level tolerance ±0.3° after rounding, wording of the sides", () => {
  assert.equal(nearZero(0.3), true);
  assert.equal(nearZero(-0.34), true); // shown as -0.3°
  assert.equal(nearZero(0.36), false);
  assert.equal(nearZero(null), false);
  assert.equal(pitchText(1.3), "front is higher");
  assert.equal(pitchText(-2), "rear is higher");
  assert.equal(pitchText(0.2), "level front to back");
  assert.equal(rollText(0.8), "right side is higher");
  assert.equal(rollText(-0.8), "left side is higher");
  assert.equal(rollText(-0.1), "level side to side");
  assert.equal(rollText(undefined), "");
});

test("levelBadge", () => {
  assert.deepEqual(levelBadge(null), { text: "no sensor", kind: "bad" });
  assert.deepEqual(levelBadge({ sensor_ok: false }), { text: "no sensor", kind: "bad" });
  assert.deepEqual(levelBadge({ sensor_ok: true, calibrated: true, pitch_deg: 0.1, roll_deg: -0.2 }), { text: "level", kind: "ok" });
  assert.deepEqual(levelBadge({ sensor_ok: true, calibrated: true, pitch_deg: 1.1, roll_deg: 0 }), { text: "tilted", kind: "warn" });
  assert.deepEqual(levelBadge({ sensor_ok: true, calibrated: false, pitch_deg: 0, roll_deg: 0 }), { text: "level · not calibrated", kind: "warn" });
  assert.equal(levelBadge({ sensor_ok: true, pitch_deg: null, roll_deg: 0 }).text, "—");
});

test("fmtVec and fmtCm", () => {
  assert.deepEqual(fmtVec([0.0234, -0.0123, 0.9991]), { parts: ["x +0.023", "y -0.012", "z +0.999"], mag: "0.999 g" });
  assert.equal(fmtVec(null), null);
  assert.equal(fmtVec([1, 2]), null);
  assert.equal(fmtVec([1, NaN, 0]), null);
  assert.equal(fmtCm(2.4), "2.5 cm");
  assert.equal(fmtCm(4), "4 cm");
  assert.equal(fmtCm(0.2), "0");
  assert.equal(fmtCm(0), "0");
  assert.equal(fmtCm(null), "—");
});

test("calibration summary and learning progress", () => {
  assert.equal(calibSummary(null), null);
  assert.deepEqual(calibSummary({ method: "reference", forward_source: "axis", axis: "y-", ref: { a: [0, 0, 1], pitch_deg: 1.3, roll_deg: -0.4 } }),
    { level: "spirit level: +1.3° along, −0.4° across", forward: "board axis Y−" });
  assert.deepEqual(calibSummary({ method: "reference", forward_source: "driving", axis: null, ref: null }),
    { level: "not set — the board as mounted", forward: "learned while driving" });
  assert.equal(calibSummary({ method: "two_point", forward_source: "axis", axis: "x+" }).level, "two-point (earlier version)");
  assert.equal(calibSummary({ method: "single", forward_source: "axis", axis: "x+" }).level, "level ground (earlier version)");
  assert.equal(hasLevelRef(null), false);
  assert.equal(hasLevelRef({ method: "reference", ref: null }), false); // only the forward direction is set
  assert.equal(hasLevelRef({ method: "reference", ref: { a: [0, 0, 1], pitch_deg: 0, roll_deg: 0 } }), true);
  assert.equal(hasLevelRef({ method: "two_point" }), true);
  assert.equal(AXES.length, 6);
  assert.equal(learnFrac({ learn_s: 2.5 }), 0.5);
  assert.equal(learnFrac({ learn_s: 9 }), 1);
  assert.equal(learnFrac(null), 0);
});

test("spirit level: what a signed reading means", () => {
  assert.equal(signMeaning("pitch", 1.3), "+1.3° — front is higher");
  assert.equal(signMeaning("pitch", -2.06), "−2.1° — rear is higher");
  assert.equal(signMeaning("roll", -0.4), "−0.4° — left side is higher");
  assert.equal(signMeaning("roll", 0.4), "+0.4° — right side is higher");
  assert.equal(signMeaning("roll", 0.04), "0.0° — level");
  assert.equal(signMeaning("pitch", -0.04), "0.0° — level");
  assert.equal(signMeaning("pitch", null), "");
  assert.equal(fmtSigned(-0.4), "−0.4°");
  assert.equal(fmtSigned(0), "0.0°");
});

test("spirit level: typed angles (minus, Unicode minus, comma, °), rounded to 0.1", () => {
  assert.equal(parseDeg("1.3"), 1.3);
  assert.equal(parseDeg("-0.4"), -0.4);
  assert.equal(parseDeg("−0.4"), -0.4);
  assert.equal(parseDeg("+2"), 2);
  assert.equal(parseDeg("1,25"), 1.3);
  assert.equal(parseDeg(" 0.44° "), 0.4);
  assert.equal(parseDeg(".5"), 0.5);
  assert.equal(parseDeg("-0.04"), 0); // no negative zero
  assert.ok(!Object.is(parseDeg("-0.04"), -0));
  for (const bad of ["", "-", "+", "abc", "1.2.3", "1-2", null, undefined]) assert.equal(parseDeg(bad), null, String(bad));
});

test("spirit level: the ± button for number pads without a minus key", () => {
  assert.equal(toggleSign("1.3"), "-1.3");
  assert.equal(toggleSign("-1.3"), "1.3");
  assert.equal(toggleSign("−1.3"), "1.3");
  assert.equal(toggleSign("+2"), "-2");
  assert.equal(toggleSign(""), "-");
  assert.equal(toggleSign(toggleSign("0.7")), "0.7");
  assert.equal(parseDeg(toggleSign("0.7")), -0.7);
});

test("spirit level: range check ±45°", () => {
  assert.equal(degError(0), null);
  assert.equal(degError(-45), null);
  assert.equal(degError(45), null);
  assert.match(degError(45.1), /−45° and \+45°/);
  assert.match(degError(null), /Enter the angle/);
  assert.match(degError(NaN), /Enter the angle/);
});

test("levelErrorText: device texts as they are, 503 / 404 readable", () => {
  const e = (status, message) => Object.assign(new Error(message), { status });
  assert.equal(levelErrorText(e(409, "The van is moving or rocking. Keep still and try again")), "The van is moving or rocking. Keep still and try again");
  assert.equal(levelErrorText(e(503, "Level sensor is not connected")), "Level sensor is not connected");
  assert.equal(levelErrorText(e(503, "Error 503")), "Level sensor is not connected");
  assert.match(levelErrorText(e(404, "Not Found")), /update the device software/);
  assert.match(levelErrorText(e(422, "Error 422")), /between −45° and \+45°/); // pydantic list detail
  assert.equal(levelErrorText(e(422, "Invalid request: give either axis or learn")), "Invalid request: give either axis or learn");
});
