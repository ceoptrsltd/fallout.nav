// Охрана на экране: web/display/ui/alarm.js, docs/contracts/alarm.md §4 («Экран»).
import test from "node:test";
import assert from "node:assert/strict";

import {
  BANNER_S, FLASH_S, HAZARD_HZ, alarmBanner, alarmFlash, alarmIndicator, bannerPlace, bannerWord, hazardOn, newAlarmTrack,
  newestFlash, updateAlarmTrack,
} from "../../web/display/ui/alarm.js";

const A = (state, seq = 3, extra = {}) => ({ state, arm_in_s: null, level_g: 0.004, sensor_ok: true, siren: false, seq, last: null, ...extra });
const hit = (kind, seq = 4) => A("triggered", seq, { last: { kind, ts: 1, title: "x" } });

test("alarmIndicator: nothing when off or without the service; ARM n / ARMED / ALARM", () => {
  assert.equal(alarmIndicator(null), null);
  assert.equal(alarmIndicator(undefined), null);
  assert.equal(alarmIndicator(A("off")), null);
  assert.deepEqual(alarmIndicator(A("arming", 3, { arm_in_s: 45 })), { text: "ARM 45", kind: "arming" });
  assert.equal(alarmIndicator(A("arming", 3, { arm_in_s: 44.2 })).text, "ARM 45", "rounded up: never shows 0 too early");
  assert.equal(alarmIndicator(A("arming", 3, { arm_in_s: null })).text, "ARM");
  assert.equal(alarmIndicator(A("arming", 3, { arm_in_s: -1 })).text, "ARM 0");
  assert.deepEqual(alarmIndicator(A("armed")), { text: "ARMED", kind: "armed" });
  assert.deepEqual(alarmIndicator(hit("impact")), { text: "ALARM", kind: "triggered" });
});

test("hazardOn: only telemetry alarm.hazard === true; blinks like real hazard lights", () => {
  assert.equal(hazardOn(A("triggered", 3, { hazard: true })), true);
  assert.equal(hazardOn(A("off", 3, { hazard: true })), true, "the 3 s test runs with the alarm off too");
  assert.equal(hazardOn(A("armed", 3, { hazard: false })), false);
  assert.equal(hazardOn(A("armed")), false, "older device: no field");
  assert.equal(hazardOn(null), false);
  assert.ok(HAZARD_HZ >= 1 && HAZARD_HZ <= 2);
});

test("bannerWord: impact / tilt / move, anything else is ALARM", () => {
  assert.equal(bannerWord("impact"), "IMPACT");
  assert.equal(bannerWord("tilt"), "TILTED");
  assert.equal(bannerWord("move"), "MOVING");
  assert.equal(bannerWord("test"), "ALARM");
  assert.equal(bannerWord(undefined), "ALARM");
});

test("banner: only for a NEW seq while triggered, for 10 s", () => {
  const t = newAlarmTrack();
  updateAlarmTrack(t, A("armed", 3), 0);
  assert.equal(alarmBanner(t, A("armed", 3), 0), null);
  updateAlarmTrack(t, hit("impact", 4), 5);
  assert.deepEqual(alarmBanner(t, hit("impact", 4), 5.1), { word: "IMPACT", t0: 5 });
  assert.equal(alarmBanner(t, hit("impact", 4), 5 + BANNER_S - 0.01).word, "IMPACT");
  assert.equal(alarmBanner(t, hit("impact", 4), 5 + BANNER_S), null);
  // the next event restarts the 10 s and takes its kind
  updateAlarmTrack(t, hit("tilt", 5), 20);
  assert.equal(alarmBanner(t, hit("tilt", 5), 21).word, "TILTED");
  // back to armed (30 s after the last event): gone even inside the window
  updateAlarmTrack(t, A("armed", 5), 22);
  assert.equal(alarmBanner(t, A("armed", 5), 22), null);
});

test("banner: the seq seen on the first message is not new; seq may arrive one message before the state", () => {
  const t = newAlarmTrack();
  updateAlarmTrack(t, hit("impact", 7), 0); // the screen starts while the alarm is already triggered
  assert.equal(alarmBanner(t, hit("impact", 7), 0.5), null);
  const t2 = newAlarmTrack();
  updateAlarmTrack(t2, A("armed", 3), 0);
  updateAlarmTrack(t2, A("armed", 4), 1); // seq first …
  updateAlarmTrack(t2, hit("move", 4), 1.1); // … then triggered
  assert.equal(alarmBanner(t2, hit("move", 4), 1.2).word, "MOVING");
});

test("banner: disarm hides it at once and it does not come back on re-arm", () => {
  const t = newAlarmTrack();
  updateAlarmTrack(t, A("armed", 3), 0);
  updateAlarmTrack(t, hit("impact", 4), 1);
  updateAlarmTrack(t, A("off", 4), 2);
  assert.equal(alarmBanner(t, A("off", 4), 2), null);
  updateAlarmTrack(t, A("armed", 4), 3);
  assert.equal(alarmBanner(t, hit("impact", 4), 3.5), null);
});

test("flash: ALARM ON / ALARM OFF for 2.5 s on off ↔ not-off, not on the first message", () => {
  const t = newAlarmTrack();
  updateAlarmTrack(t, A("armed"), 0);
  assert.equal(alarmFlash(t, 0.1), null, "first observation is not a toggle");
  updateAlarmTrack(t, A("triggered"), 1);
  assert.equal(alarmFlash(t, 1.1), null, "armed → triggered is not a toggle");
  updateAlarmTrack(t, A("off"), 2);
  assert.deepEqual(alarmFlash(t, 2.5), { on: false, t0: 2 });
  assert.equal(alarmFlash(t, 2 + FLASH_S), null);
  updateAlarmTrack(t, A("arming", 3, { arm_in_s: 60 }), 10);
  assert.deepEqual(alarmFlash(t, 10.2), { on: true, t0: 10 });
  updateAlarmTrack(t, A("armed"), 11); // arming → armed: no new flash
  assert.equal(alarmFlash(t, 11.2).t0, 10);
  // the service disappears (null): nothing changes
  updateAlarmTrack(t, null, 12);
  assert.equal(alarmFlash(t, 12.1).on, true);
});

test("newestFlash: the most recently started running flash wins", () => {
  const items = [{ id: "vol", t0: 3.0, dur: 2.5 }, { id: "alarm", t0: 3.5, dur: 2.5 }, { id: "mode", t0: -1, dur: 2.5 }];
  assert.equal(newestFlash(items, 3.8).id, "alarm");
  assert.equal(newestFlash(items, 3.2).id, "vol", "the alarm flash has not started yet");
  assert.equal(newestFlash(items, 5.7).id, "alarm");
  assert.equal(newestFlash(items, 6.1), null);
  assert.equal(newestFlash([], 1), null);
});

test("bannerPlace: centred, nudged left of a compass on the right, below a centred compass", () => {
  const safe = { x: 38.4, y: 28.8, w: 691.2, h: 518.4 };
  const free = bannerPlace(safe, 344, 92, []);
  assert.equal(free.x, Math.round(384 - 172));
  const right = bannerPlace(safe, 344, 92, [{ x: 540, y: 30, w: 190, h: 210 }]);
  assert.equal(right.y, free.y, "same height");
  assert.ok(right.x + 344 <= 530 && right.x < free.x, "left of the compass");
  const centre = bannerPlace(safe, 344, 92, [{ x: 250, y: 30, w: 270, h: 100 }]);
  assert.equal(centre.x, free.x);
  assert.ok(centre.y >= 140, "below the compass");
});

test("banner: a lost camera reads CAMERA; a camera back (no alarm) shows none", () => {
  assert.equal(bannerWord("video_lost"), "CAMERA");
  const t = newAlarmTrack();
  updateAlarmTrack(t, A("armed", 3), 0);
  updateAlarmTrack(t, hit("video_lost", 4), 5);
  assert.equal(alarmBanner(t, hit("video_lost", 4), 5.1).word, "CAMERA");
  updateAlarmTrack(t, hit("video_back", 5), 30); // still triggered: the back note is not an alarm
  assert.equal(alarmBanner(t, hit("video_back", 5), 30.1), null);
});

test("armed / disarmed in the log: no banner, only the ALARM ON / OFF flash", () => {
  const t = newAlarmTrack();
  updateAlarmTrack(t, A("off", 3), 0);
  const armed = A("arming", 4, { arm_in_s: 60, last: { kind: "armed", ts: 1, title: "Alarm armed" } });
  updateAlarmTrack(t, armed, 1);
  assert.equal(alarmBanner(t, armed, 1.1), null);
  assert.deepEqual(alarmFlash(t, 1.1), { on: true, t0: 1 });
  updateAlarmTrack(t, hit("impact", 5), 70);
  assert.equal(alarmBanner(t, hit("impact", 5), 70.1).word, "IMPACT");
  // disarmed with the keypad in the middle of the alarm: the banner goes at once, ALARM OFF flashes
  const off = A("off", 6, { last: { kind: "disarmed", ts: 2, title: "Alarm disarmed" } });
  updateAlarmTrack(t, off, 72);
  assert.equal(alarmBanner(t, off, 72.1), null);
  assert.deepEqual(alarmFlash(t, 72.1), { on: false, t0: 72 });
  // even if a switch note ever came while triggered, it is quiet
  const odd = A("triggered", 7, { last: { kind: "disarmed", ts: 3, title: "x" } });
  updateAlarmTrack(t, odd, 80);
  assert.equal(alarmBanner(t, odd, 80.1), null);
});
