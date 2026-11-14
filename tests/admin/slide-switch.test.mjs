// Slide-to-switch for the power stations (web/admin/js/slide-switch.js, power-controls.js): pure helpers.
import test from "node:test";
import assert from "node:assert/strict";

const { slideCommits, shownState, COMMIT_AT } = await import("../../web/admin/js/slide-switch.js");

test("slide: only a slide almost to the end switches", () => {
  assert.equal(slideCommits(1), true);
  assert.equal(slideCommits(COMMIT_AT), true);
  assert.equal(slideCommits(0.6), false, "half way — snaps back");
  assert.equal(slideCommits(0), false, "a tap");
  assert.equal(slideCommits(NaN), false);
});

test("slide: the switch keeps the slid state until telemetry agrees or the wait runs out", () => {
  const e = { on: true, until: 1000 };
  assert.equal(shownState(false, e, 500), true, "telemetry not there yet");
  assert.equal(shownState(true, e, 500), true);
  assert.equal(shownState(false, e, 1500), false, "gave up — show what the station says");
  assert.equal(shownState(false, null, 0), false);
});
