import { test } from "node:test";
import assert from "node:assert/strict";
import { nextVolume, VOL_STEP } from "../../web/admin/js/dash-fmt.js";

test("radio volume −/+ on the dashboard: steps like the knob, quick taps build on the value just sent, 0–100", () => {
  assert.equal(VOL_STEP, 5);
  assert.equal(nextVolume(null, 60, VOL_STEP), 65); // from telemetry
  assert.equal(nextVolume(65, 60, VOL_STEP), 70); // second tap before telemetry caught up
  assert.equal(nextVolume(null, 3, -VOL_STEP), 0);
  assert.equal(nextVolume(98, 98, VOL_STEP), 100);
  assert.equal(nextVolume(null, null, VOL_STEP), null); // no radio volume yet: nothing to step from
});
