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

test("slide: letting go in the middle sets a timer - 60 min near the start down to 5 near the end", async () => {
  const { slideZone, leftText, TIMER_FROM, TIMER_TO } = await import("../../web/admin/js/slide-switch.js");
  assert.deepEqual(slideZone(0), { kind: "none" }, "a tap");
  assert.deepEqual(slideZone(TIMER_FROM - 0.01), { kind: "none" }, "the empty start");
  assert.deepEqual(slideZone(TIMER_FROM), { kind: "timer", minutes: 60 });
  assert.deepEqual(slideZone(TIMER_TO - 0.001), { kind: "timer", minutes: 5 });
  assert.deepEqual(slideZone(TIMER_TO), { kind: "none" }, "the empty stretch before the end");
  assert.deepEqual(slideZone(COMMIT_AT - 0.01), { kind: "none" });
  assert.deepEqual(slideZone(COMMIT_AT), { kind: "now" });
  assert.deepEqual(slideZone(1), { kind: "now" });
  assert.deepEqual(slideZone(NaN), { kind: "none" });
  const mins = [];
  for (let f = TIMER_FROM; f < TIMER_TO; f += 0.001) mins.push(slideZone(f).minutes);
  assert.deepEqual([...new Set(mins)], [60, 55, 50, 45, 40, 35, 30, 25, 20, 15, 10, 5], "every 5 min step, in order");
  assert.deepEqual(slideZone(0.4, false), { kind: "none" }, "a switch without timers: the middle snaps back");
  assert.equal(leftText(1440), "24 min");
  assert.equal(leftText(61), "2 min");
  assert.equal(leftText(40), "40 s");
});
