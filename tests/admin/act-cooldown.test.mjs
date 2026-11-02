// Alarm buttons after an action (web/admin/js/dash-fmt.js actCooldown): Dashboard and Alarm page.
import test from "node:test";
import assert from "node:assert/strict";

import { ACT_COOL_MS, actCooldown, alarmActions } from "../../web/admin/js/dash-fmt.js";

test("cool-down: inert for ACT_COOL_MS after the answer, then live", () => {
  assert.equal(ACT_COOL_MS, 1500);
  const t0 = 1_000_000;
  assert.equal(actCooldown(t0, t0), ACT_COOL_MS);
  assert.equal(actCooldown(t0, t0 + 50), ACT_COOL_MS - 50); // Wi-Fi answer in ~50 ms, the second tap of a double tap
  assert.ok(actCooldown(t0, t0 + 400) > 0); // a slow "did it register?" tap
  assert.ok(actCooldown(t0, t0 + ACT_COOL_MS - 1) > 0);
  assert.equal(actCooldown(t0, t0 + ACT_COOL_MS), 0);
  assert.equal(actCooldown(t0, t0 + 60_000), 0);
  assert.equal(actCooldown(t0, t0 + 100, 200), 100); // own length
});

test("cool-down: no action yet, bad input, the clock going back — never stuck", () => {
  assert.equal(actCooldown(null, 5), 0);
  assert.equal(actCooldown(undefined, 5), 0);
  assert.equal(actCooldown(NaN, 5), 0);
  assert.equal(actCooldown(10, NaN), 0);
  assert.equal(actCooldown(1_000_000, 1_000_000 - 10_000), 0); // the clock was set back after the action
});

test("the swap the cool-down guards: Arm and Disarm take the same place", () => {
  // off → Arm at the first place; after the answer (arming) Disarm is there; a second tap within the cool-down is ignored
  assert.equal(alarmActions({ state: "off" })[0].what, "arm");
  assert.equal(alarmActions({ state: "arming" })[0].what, "disarm");
  // Stop siren & lights → after the answer Disarm moves to its place
  assert.equal(alarmActions({ state: "triggered", siren: true })[0].what, "silence");
  assert.equal(alarmActions({ state: "triggered", siren: false })[0].what, "disarm");
});
