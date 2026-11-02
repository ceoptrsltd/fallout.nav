// New version of the admin after a deploy (web/admin/js/version-fmt.js; the marker: tools/web-version.sh).
import test from "node:test";
import assert from "node:assert/strict";

import { VERSION_CHECK_MS, VERSION_GAP_MS, versionOf, versionStep, versionDue } from "../../web/admin/js/version-fmt.js";

test("versionOf: the marker's version, null without one", () => {
  assert.equal(versionOf({ version: "5c84a574863c5b8a", commit: "0ff6b7e", built: "2026-11-02T13:04:11Z" }), "5c84a574863c5b8a");
  assert.equal(versionOf({ version: " abc " }), "abc");
  assert.equal(versionOf({ version: 7 }), "7");
  assert.equal(versionOf({ version: "" }), null);
  assert.equal(versionOf({ commit: "0ff6b7e" }), null);
  assert.equal(versionOf(null), null);
  assert.equal(versionOf("abc"), null);
  assert.equal(versionOf({ version: {} }), null);
});

test("versionStep: the first answer is the base, a different version later = a deploy", () => {
  let s = versionStep(undefined, { status: 200, version: "a1" });
  assert.deepEqual(s, { base: "a1", changed: false });
  s = versionStep(s.base, { status: 200, version: "a1" });
  assert.deepEqual(s, { base: "a1", changed: false });
  s = versionStep(s.base, { status: 200, version: "b2" });
  assert.deepEqual(s, { base: "a1", changed: true });
});

test("versionStep: no marker on this server (dev, mock) — the check is off for good", () => {
  let s = versionStep(undefined, { status: 404, version: null });
  assert.deepEqual(s, { base: null, changed: false });
  s = versionStep(s.base, { status: 200, version: "a1" }); // a marker appearing later does not count: base unknown
  assert.deepEqual(s, { base: null, changed: false });
  // a file without "version"
  assert.deepEqual(versionStep(undefined, { status: 200, version: null }), { base: null, changed: false });
});

test("versionStep: no connection or a deploy in progress — no verdict, try again", () => {
  assert.deepEqual(versionStep(undefined, { status: 0, version: null }), { base: undefined, changed: false });
  assert.deepEqual(versionStep(undefined, { status: 502, version: null }), { base: undefined, changed: false });
  assert.deepEqual(versionStep(undefined, null), { base: undefined, changed: false });
  // known base: the marker gone for a moment (404) or a server error is not a new version
  assert.deepEqual(versionStep("a1", { status: 404, version: null }), { base: "a1", changed: false });
  assert.deepEqual(versionStep("a1", { status: 0, version: null }), { base: "a1", changed: false });
  assert.deepEqual(versionStep("a1", { status: 503, version: "b2" }), { base: "a1", changed: false });
  // after the network came back the first answer becomes the base
  assert.deepEqual(versionStep(undefined, { status: 200, version: "a1" }), { base: "a1", changed: false });
});

test("versionDue: visible pages, not more often than the gap; force at startup and after a page error", () => {
  const t = 10_000_000;
  assert.ok(VERSION_GAP_MS < VERSION_CHECK_MS);
  assert.equal(versionDue(NaN, t), true); // never checked
  assert.equal(versionDue(t, t + 1000), false); // visibilitychange right after the last check
  assert.equal(versionDue(t, t + VERSION_GAP_MS), true);
  assert.equal(versionDue(t, t + VERSION_CHECK_MS), true);
  assert.equal(versionDue(t, t + VERSION_CHECK_MS, { hidden: true }), false); // in the background: when it is back
  assert.equal(versionDue(t, t + 10, { force: true }), true);
  assert.equal(versionDue(t, t + 10, { hidden: true, force: true }), true);
  assert.equal(versionDue(t, t - 60_000), true); // the clock went back: not stuck
});
