// Dashboard: pure helpers (web/admin/js/dash-fmt.js).
import test from "node:test";
import assert from "node:assert/strict";

import {
  EVENTS_N, POLL, pollDelay, alarmLive, alarmActions, alarmWord, armedFor, armProgress, armHint, alarmChips,
  recentEvents, recentKey, eventExtras, eventCamera, snapCams, snapPath, thumbPath, eventAgo, fixInfo, fmtLatLon, motionInfo, limitText, camInfo,
  rearInfo, screenAction, exposureInfo, tempInfo, throttleInfo, diskInfo, netInfo, levelInfo, radioInfo, speakerInfo,
} from "../../web/admin/js/dash-fmt.js";

test("poll: fast without telemetry, slower while armed, slow when off", () => {
  assert.equal(pollDelay(false, "off"), POLL.offline);
  assert.equal(pollDelay(false, null), POLL.offline);
  assert.equal(pollDelay(true, "armed"), POLL.active);
  assert.equal(pollDelay(true, "arming"), POLL.active);
  assert.equal(pollDelay(true, "triggered"), POLL.active);
  assert.equal(pollDelay(true, "off"), POLL.idle);
  assert.equal(pollDelay(true, null), POLL.idle);
  assert.ok(POLL.offline < POLL.active && POLL.active < POLL.idle);
});

test("alarm live: telemetry first, then the api status, else null", () => {
  const t = { state: "armed", arm_in_s: null, siren: true, hazard: false, sensor_ok: true, seq: 7 };
  const st = { state: "off", arm_in_s: null, siren: false, hazard: { active: true }, sensor: { ok: false } };
  assert.deepEqual(alarmLive(t, st, 100, 50), { state: "armed", armIn: null, siren: true, hazard: false, sensorOk: true, at: 100, seq: 7 });
  assert.deepEqual(alarmLive(null, st, 100, 50), { state: "off", armIn: null, siren: false, hazard: true, sensorOk: false, at: 50, seq: null });
  assert.equal(alarmLive(null, null, 0, 0), null);
  assert.equal(alarmLive({ state: "off" }, null, 1, 0).seq, null); // an old device without seq
});

test("alarm buttons: Arm when off; Disarm otherwise; Stop first while the siren or the lights run", () => {
  assert.deepEqual(alarmActions({ state: "off" }).map((a) => a.what), ["arm"]);
  assert.deepEqual(alarmActions({ state: "arming" }).map((a) => a.what), ["disarm"]);
  assert.deepEqual(alarmActions({ state: "armed" }).map((a) => a.what), ["disarm"]);
  assert.deepEqual(alarmActions({ state: "triggered" }).map((a) => a.what), ["disarm"]);
  const t = alarmActions({ state: "triggered", siren: true, hazard: false, hazardEnabled: true });
  assert.deepEqual(t.map((a) => a.what), ["silence", "disarm"]);
  assert.equal(t[0].label, "Stop siren & lights");
  assert.equal(t[0].cls, "primary");
  assert.equal(t[1].cls, "danger");
  assert.equal(alarmActions({ state: "triggered", siren: false, hazard: true })[0].label, "Stop the lights");
  assert.equal(alarmActions({ state: "triggered", siren: true, hazard: false, hazardEnabled: false })[0].label, "Stop the siren");
  assert.deepEqual(alarmActions({ state: null }), []);
});

test("alarm words, armed time, countdown bar, arm hint", () => {
  assert.equal(alarmWord("off"), "OFF");
  assert.equal(alarmWord("arming"), "ARMING");
  assert.equal(alarmWord("triggered"), "TRIGGERED");
  assert.equal(alarmWord("weird"), "WEIRD");
  assert.equal(alarmWord(null), "—");
  assert.equal(armedFor(1000, 1030), "Armed just now");
  assert.equal(armedFor(1000, 1000 + 2 * 3600 + 600), "Armed for 2 h 10 min");
  assert.equal(armedFor(null, 1000), null);
  assert.equal(armProgress(60, 60), 0);
  assert.equal(armProgress(15, 60), 0.75);
  assert.equal(armProgress(0, 60), 1);
  assert.equal(armProgress(90, 60), 0); // clamped
  assert.equal(armProgress(null, 60), 0);
  assert.equal(armProgress(5, 0), 0); // no total: no division by zero
  assert.equal(armHint(0), "Arms at once.");
  assert.equal(armHint(60), "Arms 60 s after you press Arm.");
  assert.equal(armHint(null), "");
});

test("alarm chips: camera watch always, hazard when used, problems only when wrong", () => {
  const base = {
    vision: { enabled: true, available: true, watching: false, people: 0 },
    hazard: { enabled: false, active: false, error: null },
    sensor: { ok: true },
    relay: { configured: true, pending: 0 },
  };
  assert.deepEqual(alarmChips(base).map((c) => [c.key, c.text, c.kind]), [["vision", "Camera watch ready", "ok"]]);
  const watching = (people) => alarmChips({ ...base, vision: { ...base.vision, watching: true, people } })[0];
  assert.deepEqual([watching(0).text, watching(0).kind], ["Camera watching", "ok"]);
  assert.deepEqual([watching(1).text, watching(1).kind], ["A person in view", "warn"]);
  assert.equal(watching(3).text, "3 people in view");
  assert.equal(alarmChips({ ...base, vision: { enabled: false } })[0].text, "Camera watch off");
  assert.equal(alarmChips({ ...base, vision: { enabled: true, available: false } })[0].kind, "bad");
  const err = alarmChips({ ...base, vision: { enabled: true, available: true, error: "model missing" } })[0];
  assert.deepEqual([err.text, err.kind, err.title], ["Camera watch error", "bad", "model missing"]);

  const hz = (h, opts) => alarmChips({ ...base, hazard: h }, opts).find((c) => c.key === "hazard");
  assert.equal(hz({ enabled: false, active: false }), undefined);
  assert.equal(hz({ enabled: true, active: false }).text, "Hazard lights ready");
  assert.equal(hz({ enabled: true, active: true }).kind, "warn");
  assert.equal(hz({ enabled: true, active: false }, { hazardActive: true }).text, "Hazard lights flashing"); // live wins
  assert.equal(hz({ enabled: true, active: true }, { hazardActive: false }).text, "Hazard lights ready");
  assert.equal(hz({ enabled: false, error: "relay not found" }).kind, "bad");

  const sen = alarmChips({ ...base, sensor: { ok: false, error: "I2C timeout" } }).find((c) => c.key === "sensor");
  assert.equal(sen.kind, "bad");
  assert.match(sen.title, /I2C timeout/);
  assert.equal(alarmChips(base, { sensorOk: false }).some((c) => c.key === "sensor"), true); // live telemetry wins
  assert.equal(alarmChips({ ...base, sensor: { ok: false } }, { sensorOk: true }).some((c) => c.key === "sensor"), false);

  assert.equal(alarmChips({ ...base, relay: { configured: false, pending: 0 } }).find((c) => c.key === "relay").text, "No notifications");
  assert.equal(alarmChips({ ...base, relay: { configured: true, pending: 2 } }).find((c) => c.key === "relay").text, "2 waiting to send");

  assert.deepEqual(alarmChips(null), []);
  assert.deepEqual(alarmChips({ state: "off" }), []); // an old device: nothing reported
});

test("events: newest first, n of them; extras; change key sees a snapshot added later", () => {
  const evs = [{ id: "a", ts: 100 }, { id: "c", ts: 300 }, { id: "b", ts: 200 }, null, { id: "d" }];
  assert.deepEqual(recentEvents(evs, 2).map((e) => e.id), ["c", "b"]);
  assert.equal(recentEvents(Array.from({ length: 20 }, (_, i) => ({ id: "x" + i, ts: i }))).length, EVENTS_N);
  assert.deepEqual(recentEvents(undefined), []);

  assert.deepEqual(eventExtras({ data: { snapshot: true, segment: "2026-10-27/2026-10-27_21-00-00.ts" }, url: "#/alarm" }),
    { snapshot: true, segment: "2026-10-27/2026-10-27_21-00-00.ts", snapshotRear: false, segmentRear: null, link: null });
  assert.deepEqual(eventExtras({ url: "#/tracking" }).link, { href: "#/tracking", text: "Show on the map" });
  assert.deepEqual(eventExtras({ data: { snapshot: "yes", segment: "" } }), { snapshot: false, segment: null, snapshotRear: false, segmentRear: null, link: null });
  // the rear camera's snapshot and kept recording (data.snapshot_rear / data.segment_rear)
  const xr = eventExtras({ data: { snapshot_rear: true, segment_rear: "rear/2026-10-27/2026-10-27_21-00-00.ts" } });
  assert.equal(xr.snapshotRear, true);
  assert.equal(xr.segmentRear, "rear/2026-10-27/2026-10-27_21-00-00.ts");
  assert.equal(xr.snapshot, false);

  const before = recentKey([{ id: "e1", data: {} }]);
  const after = recentKey([{ id: "e1", data: { snapshot: true } }]);
  assert.notEqual(before, after);
  assert.notEqual(after, recentKey([{ id: "e1", data: { snapshot: true, snapshot_rear: true } }]));
  assert.notEqual(after, recentKey([{ id: "e1", data: { snapshot: true, segment_rear: "rear/x.ts" } }]));
  assert.equal(recentKey([]), "");

  assert.equal(snapPath("b746dce4655142babc03073033a00062"), "api/alarm/snapshot/b746dce4655142babc03073033a00062.jpg");
  assert.equal(snapPath("../x"), "api/alarm/snapshot/..%2Fx.jpg");
  assert.equal(thumbPath("b746dce4655142babc03073033a00062", "rear"), "api/alarm/snapshot/b746dce4655142babc03073033a00062-rear.jpg?thumb=1");
  assert.equal(snapPath("b746dce4", "rear"), "api/alarm/snapshot/b746dce4-rear.jpg");
  assert.equal(snapPath("b746dce4", "front"), "api/alarm/snapshot/b746dce4.jpg");
  // the snapshot of the camera that raised the event first: a rear-camera event shows the rear's
  const both = { snapshot: true, snapshot_rear: true };
  assert.equal(eventCamera({ data: { camera: "rear" } }), "rear");
  assert.equal(eventCamera({ data: {} }), "front");
  assert.equal(eventCamera(null), "front");
  assert.deepEqual(snapCams({ data: { ...both, camera: "rear" } }), ["rear", "front"]);
  assert.deepEqual(snapCams({ data: { ...both, camera: "front" } }), ["front", "rear"]);
  assert.deepEqual(snapCams({ kind: "impact", data: both }), ["front", "rear"]);
  assert.deepEqual(snapCams({ data: { snapshot: true, camera: "rear" } }), ["front"], "no rear snapshot: the front's");
  assert.deepEqual(snapCams({ data: {} }), []);
  assert.equal(eventAgo(1000, 1000 + 5 * 60 + 10), "5 min ago");
  assert.equal(eventAgo(null, 1000), "—");
});

test("position: fix, coordinates, moving or parked, speed limit sign", () => {
  assert.deepEqual(fixInfo({ fix: "3d", sats_used: 9 }), { text: "3D · 9 sats", kind: "ok" });
  assert.deepEqual(fixInfo({ fix: "2d", sats_used: 4 }), { text: "2D · 4 sats", kind: "warn" });
  assert.deepEqual(fixInfo({ fix: "none" }), { text: "No fix", kind: "bad" });
  assert.deepEqual(fixInfo(null), { text: "No fix", kind: "bad" });
  assert.equal(fmtLatLon(52.229676, 21.012229), "52.22968, 21.01223");
  assert.equal(fmtLatLon(null, 21), null);
  assert.deepEqual(motionInfo({ fix: "3d", speed_mps: 15 }, { deg: 45 }), { text: "54 km/h NE", moving: true });
  assert.deepEqual(motionInfo({ fix: "3d", speed_mps: 15 }, { deg: null }), { text: "54 km/h", moving: true });
  assert.deepEqual(motionInfo({ fix: "3d", speed_mps: 0 }), { text: "Parked", moving: false });
  assert.deepEqual(motionInfo({ fix: "3d", speed_mps: 0.6 }), { text: "Parked", moving: false });
  assert.deepEqual(motionInfo({ fix: "none" }), { text: "No position", moving: false });
  // fix lost (gpsd silent, older core): the last speed in the packet is not "90 km/h NE" forever
  assert.deepEqual(motionInfo({ fix: "none", lat: 52.2, lon: 21.0, speed_mps: 25, course_deg: 45 }, { deg: 45 }),
    { text: "No position", moving: false });
  assert.deepEqual(motionInfo({ fix: "3d", speed_mps: null }, { deg: 45 }), { text: "Parked", moving: false });
  assert.equal(limitText({ kmh: 50, src: "osm" }), "50");
  assert.equal(limitText(null), null);
  assert.equal(limitText({ kmh: 0 }), null);
});

test("camera: recording state and the night exposure line", () => {
  assert.deepEqual(camInfo({ state: "recording", recording: true }), { text: "Recording", kind: "ok", rec: true });
  assert.equal(camInfo({ state: "error", recording: false }).kind, "bad");
  assert.equal(camInfo({ state: "starting" }).text, "Starting…");
  assert.equal(camInfo({ state: "stopped" }).text, "Not recording");
  assert.equal(camInfo(null).text, "Not recording");
  assert.deepEqual(camInfo({ state: "live" }), { text: "Live, not recording", kind: "ok", rec: false });
  assert.equal(camInfo({ state: "disabled" }).text, "Off");

  // the rear camera's line and the car screen button
  assert.deepEqual(rearInfo({ state: "recording", recording: true }), { text: "Recording", kind: "ok", rec: true });
  assert.deepEqual(rearInfo({ state: "live" }), { text: "Live", kind: "ok", rec: false });
  assert.equal(rearInfo({ state: "error", error: "x" }).kind, "bad");
  assert.equal(rearInfo({ state: "stopped" }).text, "Not recording");
  assert.equal(rearInfo({ state: "disabled" }).text, "Off");
  assert.equal(rearInfo(null).text, "Starting…"); // on, but no status yet
  assert.equal(screenAction({ mode: "camera", camera: "rear" }).to, "front");
  assert.equal(screenAction({ mode: "camera", camera: "rear" }).label, "Back to front");
  assert.equal(screenAction({ mode: "camera", camera: "rear" }).onScreen, true);
  assert.equal(screenAction({ mode: "map", camera: "front" }).to, "rear");
  assert.equal(screenAction(null).label, "Show on screen");
  assert.equal(screenAction(null).onScreen, false);

  const night = exposureInfo({ exposure_ms: 124.8, max_exposure_ms: 125, gain: 16, lux: 3.4, moving: false });
  assert.equal(night.text, "Night · 125 ms · 16×");
  assert.equal(night.night, true);
  assert.equal(night.title, "exposure 125 ms · gain 16× · 3.4 lux · limit 125 ms (parked)");
  const day = exposureInfo({ exposure_ms: 4.2, max_exposure_ms: 66, gain: 1.12, moving: true });
  assert.equal(day.text, "Day · 4.2 ms · 1.1×");
  assert.equal(day.night, false);
  assert.match(day.title, /limit 66 ms \(driving\)/);
  assert.equal(exposureInfo({ exposure_ms: 30 }).text, "Day · 30 ms"); // no limit, no gain reported
  assert.equal(exposureInfo({ exposure_ms: null }), null);
  assert.equal(exposureInfo(undefined), null);
});

test("system: temperature, power, disk, network", () => {
  assert.deepEqual(tempInfo(52.4), { text: "52 °C", kind: "" });
  assert.equal(tempInfo(72).kind, "warn");
  assert.equal(tempInfo(81).kind, "bad");
  assert.deepEqual(tempInfo(null), { text: "—", kind: "" });

  assert.deepEqual(throttleInfo("0x0"), { text: "OK", kind: "ok" });
  assert.deepEqual(throttleInfo("0x50005"), { text: "Under-voltage, throttled", kind: "bad" });
  assert.deepEqual(throttleInfo("0x50000"), { text: "Under-voltage, throttling since boot", kind: "warn" });
  assert.deepEqual(throttleInfo(null), { text: "—", kind: "" });
  assert.deepEqual(throttleInfo("zz"), { text: "zz", kind: "" });

  assert.deepEqual(diskInfo({ free_gb: 12.34 }), { text: "12 GB free", kind: "" });
  assert.deepEqual(diskInfo({ free_gb: 3.21 }), { text: "3.2 GB free", kind: "warn" });
  assert.deepEqual(diskInfo({}, { free_gb: 1.5 }), { text: "1.5 GB free", kind: "bad" }); // camera's figure as a fallback
  assert.deepEqual(diskInfo(null, null), { text: "—", kind: "" });

  assert.deepEqual(netInfo({ internet: true, wifi_ssid: "Pixel", wifi_signal_pct: 72 }), { text: "Online · Pixel 72%", kind: "ok" });
  assert.deepEqual(netInfo({ internet: true }), { text: "Online", kind: "ok" });
  assert.deepEqual(netInfo({ internet: false, wifi_ssid: "Camp" }), { text: "No internet · Camp", kind: "warn" });
  assert.deepEqual(netInfo({}), { text: "—", kind: "" });
});

test("level tile", () => {
  assert.equal(levelInfo(null), null);
  assert.deepEqual(levelInfo({ sensor_ok: false }), { text: "No sensor", kind: "bad", sub: "", note: "" });
  assert.deepEqual(levelInfo({ sensor_ok: true, calibrated: true, pitch_deg: 0.1, roll_deg: -0.2 }),
    { text: "Level", kind: "ok", sub: "Pitch +0.1° · Roll -0.2°", note: "" });
  const t = levelInfo({ sensor_ok: true, calibrated: false, pitch_deg: 2.04, roll_deg: 0 });
  assert.equal(t.text, "Tilted");
  assert.equal(t.kind, "warn");
  assert.equal(t.note, "not calibrated");
  assert.equal(levelInfo({ sensor_ok: true, pitch_deg: null, roll_deg: null }).text, "—");
});

test("radio tile and the Bluetooth speaker", () => {
  const p = radioInfo({ state: "playing", station: "Radio Nowy Swiat", track: "Artist - Song", volume: 55 });
  assert.equal(p.title, "Radio Nowy Swiat");
  assert.equal(p.line, "Artist - Song");
  assert.equal(p.track, true);
  assert.equal(p.playing, true);
  assert.equal(p.active, true);
  assert.equal(p.volume, 55);
  assert.equal(radioInfo({ state: "playing", station: "X" }).line, "On air");
  const w = radioInfo({ state: "waiting_output", station: "X" });
  assert.deepEqual([w.active, w.playing, w.line, w.lineKind], [true, false, "Waiting for the speaker", "warn"]);
  const s = radioInfo({ state: "stopped", station: "X", volume: 60 });
  assert.deepEqual([s.active, s.off, s.line, s.canPlay], [false, true, "Off", true]);
  const none = radioInfo({ state: "stopped", station: null });
  assert.deepEqual([none.title, none.canPlay], ["No station", false]);
  assert.equal(radioInfo({ state: "error", station: "X" }).lineKind, "bad");
  assert.equal(radioInfo(null).canPlay, false);

  assert.deepEqual(speakerInfo({ output: "JBL Flip 5", output_connected: true }), { text: "JBL Flip 5", connected: true, kind: "ok" });
  assert.deepEqual(speakerInfo({ output: "JBL Flip 5", output_connected: false }), { text: "JBL Flip 5", connected: false, kind: "warn" });
  assert.deepEqual(speakerInfo({ output: null }), { text: "No speaker", connected: false, kind: "" });
  assert.deepEqual(speakerInfo(null), { text: "No speaker", connected: false, kind: "" });
});

test("hazard banner word: the car screen's words", async () => {
  const { bannerWord } = await import("../../web/admin/js/dash-fmt.js");
  assert.equal(bannerWord("impact"), "IMPACT");
  assert.equal(bannerWord("tilt"), "TILTED");
  assert.equal(bannerWord("move"), "MOVING");
  assert.equal(bannerWord("approach"), "PERSON");
  assert.equal(bannerWord("face"), "FACE");
  assert.equal(bannerWord("loiter"), "PERSON");
  assert.equal(bannerWord("test"), "ALARM");
  assert.equal(bannerWord(undefined), "ALARM");
});
