// Level page: pure helpers (web/admin/js/level-fmt.js), docs/contracts/level.md.
import test from "node:test";
import assert from "node:assert/strict";

import {
  AXES, fmtDeg, nearZero, pitchText, rollText, levelBadge, fmtVec, fmtCm, calibSummary, learnFrac, levelErrorText,
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
  assert.deepEqual(calibSummary({ method: "two_point", forward_source: "axis", axis: "y-" }),
    { level: "precise, two-point", forward: "board axis Y−" });
  assert.deepEqual(calibSummary({ method: "single", forward_source: "driving", axis: null }),
    { level: "single, on level ground", forward: "learned while driving" });
  assert.equal(AXES.length, 6);
  assert.equal(learnFrac({ learn_s: 2.5 }), 0.5);
  assert.equal(learnFrac({ learn_s: 9 }), 1);
  assert.equal(learnFrac(null), 0);
});

test("levelErrorText: device texts as they are, 503 / 404 readable", () => {
  const e = (status, message) => Object.assign(new Error(message), { status });
  assert.equal(levelErrorText(e(409, "The van is moving or rocking. Keep still and try again")), "The van is moving or rocking. Keep still and try again");
  assert.equal(levelErrorText(e(503, "Level sensor is not connected")), "Level sensor is not connected");
  assert.equal(levelErrorText(e(503, "Error 503")), "Level sensor is not connected");
  assert.match(levelErrorText(e(404, "Not Found")), /update the device software/);
});
