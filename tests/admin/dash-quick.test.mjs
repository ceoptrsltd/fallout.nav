// Dashboard: quick actions and the offline view (web/admin/js/dash-fmt.js).
import test from "node:test";
import assert from "node:assert/strict";

import {
  newerAlarm, serverPos, videoAction, SCREEN_MODES, screenNowText, quickConfirm, protectedText, actCooldown,
} from "../../web/admin/js/dash-fmt.js";

test("newerAlarm: the newer of the last telemetry and the api/alarm status", () => {
  const tA = { state: "armed", siren: false, hazard: false, sensor_ok: true, seq: 4 };
  const st = { state: "off", siren: false, hazard: { active: false }, sensor: { ok: true } };
  assert.equal(newerAlarm(tA, 2000, st, 1000).state, "armed");
  assert.equal(newerAlarm(tA, 2000, st, 1000).at, 2000);
  assert.equal(newerAlarm(tA, 1000, st, 2000).state, "off"); // e.g. the answer of Disarm
  assert.equal(newerAlarm(tA, 1000, st, 2000).at, 2000);
  assert.equal(newerAlarm(tA, 1000, null, 0).state, "armed");
  assert.equal(newerAlarm(null, 0, st, 5).state, "off");
  assert.equal(newerAlarm(undefined, 0, null, 0), null);
  assert.equal(newerAlarm({}, 9, st, 5).state, "off"); // a telemetry packet without the alarm
});

test("serverPos: the server's last point only when newer than what this phone saw", () => {
  const last = { ts: 1_800_000_100, lat: 52.1, lon: 21.0, speed_mps: 0, course_deg: 90, received_ts: 1_800_000_102 };
  assert.deepEqual(serverPos(last, 1_800_000_000_000), { lat: 52.1, lon: 21.0, speed_mps: 0, course_deg: 90, ts: 1_800_000_100 });
  assert.equal(serverPos(last, 1_800_000_200_000), null); // the kept telemetry is newer
  assert.ok(serverPos(last, 0)); // nothing kept
  assert.equal(serverPos({}, 0), null); // the server has no point yet ({})
  assert.equal(serverPos(null, 0), null);
  assert.equal(serverPos({ lat: 1, lon: 2 }, 0), null); // no time
  assert.equal(serverPos({ received_ts: 5, lat: 1, lon: 2 }, 0).ts, 5);
  assert.equal(serverPos({ ts: 5, lat: 1, lon: 2, speed_mps: "x" }, 0).speed_mps, null);
});

test("videoAction: the state word and what the button does", () => {
  assert.deepEqual([videoAction(true).state, videoAction(true).label, videoAction(true).to], ["On", "Turn off", false]);
  assert.deepEqual([videoAction(false).state, videoAction(false).label, videoAction(false).to], ["Off", "Turn on", true]);
  assert.equal(videoAction(null).to, null); // not known: a plain toggle
  assert.equal(videoAction(undefined).state, "—");
});

test("screen modes and what the car screen shows", () => {
  assert.deepEqual(SCREEN_MODES.map((m) => m.value), ["auto", "map", "camera"]);
  assert.equal(screenNowText({ mode: "map", reason: "speed" }), "Showing the map · by speed");
  assert.equal(screenNowText({ mode: "camera", reason: "forced", camera: "front" }), "Showing the camera · forced");
  assert.equal(screenNowText({ mode: "camera", reason: "no_fix" }), "Showing the camera · no GPS fix");
  assert.equal(screenNowText({ mode: "camera", reason: "rear", camera: "rear" }), "Showing the rear camera");
  assert.equal(screenNowText({ mode: "map", reason: "whatever" }), "Showing the map");
  assert.equal(screenNowText(null), "");
  assert.equal(screenNowText({}), "");
});

test("quickConfirm: the car monitor going dark asks; turning it on does not", () => {
  const off = quickConfirm("video", { to: false });
  assert.ok(off && off.danger);
  assert.match(off.title, /off/);
  assert.ok(quickConfirm("video", { to: null }), "a toggle of an unknown state may turn it off");
  assert.equal(quickConfirm("video", { to: true }), null);
});

test("quickConfirm: forcing the map / camera asks only while the van moves; Auto never", () => {
  assert.equal(quickConfirm("screen", { mode: "camera", moving: false }), null);
  assert.equal(quickConfirm("screen", { mode: "map", moving: false }), null);
  assert.match(quickConfirm("screen", { mode: "camera", moving: true }).text, /moving/);
  assert.ok(quickConfirm("screen", { mode: "map", moving: true }));
  assert.equal(quickConfirm("screen", { mode: "auto", moving: true }), null);
  assert.equal(quickConfirm("protect"), null);
});

test("protectedText: front, rear or both", () => {
  assert.equal(protectedText({ segment: "a.ts", segment_rear: "rear/b.ts" }), "Front and rear recordings protected");
  assert.equal(protectedText({ segment: "a.ts", segment_rear: null }), "Recording protected");
  assert.equal(protectedText({ segment: null, segment_rear: "rear/b.ts" }), "Rear recording protected");
  assert.equal(protectedText(null), "Protected");
});

test("quick actions reuse the alarm buttons' double-tap guard", () => {
  const t0 = 1_000_000;
  assert.ok(actCooldown(t0, t0 + 100) > 0); // "Turn off" became "Turn on" in place: a second tap is ignored
  assert.equal(actCooldown(null, t0), 0);
});
