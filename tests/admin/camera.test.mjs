// Camera page: Front | Rear, the rear camera's status and settings, the car screen (web/admin/js/camera-fmt.js).
import test from "node:test";
import assert from "node:assert/strict";

import {
  CAMS, camName, camFromHash, camState, camDot, codecInfo, rearStatus, screenInfo, rtspProblem, passwordInfo,
  takePassword, rearConfirm, snapName, protectedInfo, storageWarning, sizeText, deleteAllText, deletedText,
} from "../../web/admin/js/camera-fmt.js";

test("tabs: names and the camera from the page link", () => {
  assert.deepEqual(CAMS, ["front", "rear"]);
  assert.equal(camName("rear"), "Rear");
  assert.equal(camName("front"), "Front");
  assert.equal(camName(undefined), "Front");
  assert.equal(camFromHash("#/camera?cam=rear"), "rear");
  assert.equal(camFromHash("#/camera?cam=front"), "front");
  assert.equal(camFromHash("#/camera?cam=side"), null);
  assert.equal(camFromHash("#/camera"), null);
  assert.equal(camFromHash(""), null);
  assert.equal(camFromHash(null), null);
});

test("camera state: recording, live without recording, off, error; the tab dot", () => {
  assert.deepEqual(camState({ state: "recording", recording: true }), { text: "recording", kind: "ok", rec: true });
  assert.deepEqual(camState({ state: "live" }), { text: "live, not recording", kind: "ok", rec: false });
  assert.deepEqual(camState({ state: "disabled" }), { text: "off", kind: "", rec: false });
  assert.equal(camState({ state: "error" }).kind, "bad");
  assert.equal(camState({ state: "starting" }).kind, "warn");
  assert.equal(camState({ state: "stopped" }).kind, "warn");
  assert.deepEqual(camState({ state: "weird" }), { text: "weird", kind: "warn", rec: false });
  assert.deepEqual(camState(null), { text: "—", kind: "", rec: false });
  assert.equal(camDot({ state: "recording", recording: true }), "rec");
  assert.equal(camDot({ state: "live" }), "ok");
  assert.equal(camDot({ state: "error" }), "bad");
  assert.equal(camDot({ state: "starting" }), "warn");
  assert.equal(camDot({ state: "disabled" }), "off");
  assert.equal(camDot(null), "off");
});

test("codec: H.264 is fine, anything else needs the camera set to H.264", () => {
  assert.deepEqual(codecInfo("h264"), { text: "H.264", kind: "" });
  assert.deepEqual(codecInfo("H264"), { text: "H.264", kind: "" });
  assert.equal(codecInfo("hevc").kind, "bad");
  assert.match(codecInfo("hevc").text, /^H\.265 — set the camera to H\.264$/);
  assert.match(codecInfo("vp9").text, /^VP9/);
  assert.deepEqual(codecInfo(null), { text: "—", kind: "" });
});

test("rear status card values", () => {
  const s = rearStatus({
    state: "recording", recording: true, codec: "h264", output_size: "1920x1080", stream_fps: 25, fps_actual: 24.96,
    bitrate_kbps: 3987.4, frames_dropped: 3, current_segment: "rear/2026-10-28/2026-10-28_10-00-00.ts", error: null,
  });
  assert.equal(s.state.text, "recording");
  assert.equal(s.codec.text, "H.264");
  assert.equal(s.size, "1920×1080");
  assert.equal(s.streamFps, "25");
  assert.equal(s.fps, "25");
  assert.equal(s.bitrate, "3987 kbit/s");
  assert.deepEqual(s.dropped, { text: "3", kind: "warn" });
  assert.equal(s.segment, "2026-10-28_10-00-00.ts");
  assert.deepEqual(s.error, { text: "—", kind: "" });
  assert.equal(rearStatus({ fps_actual: 12.46 }).fps, "12.5");

  const e = rearStatus({ state: "error", error: "Connection refused" });
  assert.deepEqual(e.error, { text: "Connection refused", kind: "bad" });
  assert.equal(e.state.kind, "bad");
  const none = rearStatus(null);
  assert.equal(none.state.text, "—");
  assert.equal(none.size, "—");
  assert.equal(none.bitrate, "—");
  assert.deepEqual(none.dropped, { text: "0", kind: "" });
  assert.equal(rearStatus({ state: "disabled" }).state.text, "off");
});

test("car screen: rear view overrides the map / camera logic", () => {
  assert.deepEqual(screenInfo({ mode: "camera", camera: "rear", rear_mirror: true }), { camera: "rear", text: "Rear camera, mirrored" });
  assert.deepEqual(screenInfo({ mode: "camera", camera: "rear", rear_mirror: false }), { camera: "rear", text: "Rear camera" });
  assert.deepEqual(screenInfo({ mode: "camera", camera: "rear" }), { camera: "rear", text: "Rear camera, mirrored" }); // default mirror
  assert.deepEqual(screenInfo({ mode: "camera", camera: "front" }), { camera: "front", text: "Front camera" });
  assert.deepEqual(screenInfo({ mode: "map", camera: "front" }), { camera: "front", text: "Map" });
  assert.deepEqual(screenInfo({ mode: "map" }), { camera: "front", text: "Map" }); // an older device without camera
  assert.deepEqual(screenInfo(null), { camera: null, text: "—" });
});

test("RTSP address: rtsp:// with a host, no login inside, required only to turn the camera on", () => {
  assert.equal(rtspProblem("rtsp://192.168.1.60:554/stream1"), null);
  assert.equal(rtspProblem("RTSP://cam.local/h264/ch1/main/av_stream"), null);
  assert.equal(rtspProblem("rtsps://10.0.0.9/live"), null);
  assert.equal(rtspProblem("rtsp://10.0.0.9"), null);
  assert.equal(rtspProblem(""), null);
  assert.equal(rtspProblem(null), null);
  assert.equal(rtspProblem("", { required: true }), "Enter the camera's RTSP address");
  assert.equal(rtspProblem("   ", { required: true }), "Enter the camera's RTSP address");
  assert.equal(rtspProblem("http://10.0.0.9/stream"), "The address starts with rtsp://");
  assert.equal(rtspProblem("10.0.0.9/stream"), "The address starts with rtsp://");
  assert.equal(rtspProblem("rtsp://10.0.0.9/stream 1"), "No spaces in the address");
  assert.equal(rtspProblem(" rtsp://10.0.0.9/stream"), "No spaces in the address");
  assert.match(rtspProblem("rtsp://admin:secret@10.0.0.9/stream"), /^Leave the login out/);
  assert.match(rtspProblem("rtsp://admin@10.0.0.9/stream"), /^Leave the login out/);
  assert.equal(rtspProblem("rtsp:///stream"), "The camera's IP address is missing");
  assert.equal(rtspProblem("rtsp://:554/stream"), "The camera's IP address is missing");
});

test("password field: write-only status", () => {
  assert.deepEqual(passwordInfo(true, null), { text: "Set: kept on the device, never shown", kind: "ok" });
  assert.deepEqual(passwordInfo(false, null), { text: "Not set", kind: "" });
  assert.deepEqual(passwordInfo(null, null), { text: "Not known while the camera is off", kind: "" });
  assert.deepEqual(passwordInfo(null, null, true), { text: "Not reported by the camera yet", kind: "" });
  assert.equal(passwordInfo(false, null, true).text, "Not set");
  assert.equal(passwordInfo(true, "").kind, "warn");
  assert.match(passwordInfo(true, "").text, /^Removed/);
  assert.match(passwordInfo(false, "s3cret").text, /^New password/);
  assert.match(passwordInfo(null, "s3cret").text, /^New password/);
});

test("takePassword: out of the settings patch, the rest stays", () => {
  const p1 = { rear_camera: { url: "rtsp://x/y", password: "abc" }, camera: { fps: 15 } };
  assert.equal(takePassword(p1), "abc");
  assert.deepEqual(p1, { rear_camera: { url: "rtsp://x/y" }, camera: { fps: 15 } });
  const p2 = { rear_camera: { password: "" } };
  assert.equal(takePassword(p2), ""); // remove it
  assert.deepEqual(p2, {}); // nothing else to save
  const p3 = { rear_camera: { url: "rtsp://x/y" } };
  assert.equal(takePassword(p3), undefined);
  assert.deepEqual(p3, { rear_camera: { url: "rtsp://x/y" } });
  assert.equal(takePassword({}), undefined);
  assert.equal(takePassword(null), undefined);
});

test("rear save confirmation: only when a recording camera reconnects or goes off", () => {
  const on = { enabled: true, record: true };
  assert.equal(rearConfirm({ url: "rtsp://a/b" }, { enabled: false }), null); // off: nothing to interrupt
  assert.equal(rearConfirm({ enabled: true, url: "rtsp://a/b" }, { enabled: false }), null); // turning it on
  assert.equal(rearConfirm({ mirror: false }, on), null); // the mirror is applied on the fly
  assert.equal(rearConfirm({ fps: 15, bitrate_kbps: 2048 }, on), null); // estimates only: no reconnect
  assert.equal(rearConfirm({}, on), null); // nothing changes
  assert.equal(rearConfirm({}, on, true).title, "Rear camera restart"); // only the password: it reconnects too
  assert.equal(rearConfirm({ url: "rtsp://a/c" }, on).okText, "Apply and reconnect");
  assert.equal(rearConfirm({ transport: "udp", mirror: true }, on).title, "Rear camera restart");
  assert.equal(rearConfirm({ url: "rtsp://a/c" }, { enabled: true, record: false }), null); // not recording: no gap
  assert.equal(rearConfirm({ enabled: false }, on).title, "Turn the rear camera off?");
  assert.equal(rearConfirm({ enabled: false }, { enabled: true, record: false }).okText, "Turn off");
  assert.equal(rearConfirm(null, null), null);
});

test("snapshot file name: camera and local time", () => {
  const d = new Date(2026, 9, 28, 9, 5, 7);
  assert.equal(snapName("rear", d), "rear-2026-10-28_09-05-07.jpg");
  assert.equal(snapName("front", d), "front-2026-10-28_09-05-07.jpg");
  assert.equal(snapName("rear", d.getTime()), "rear-2026-10-28_09-05-07.jpg");
});

test("protected recordings: status row, amber while they squeeze the unprotected ones", () => {
  assert.deepEqual(protectedInfo({ protected_gb: 3.24, loop_minutes: 95.4, over_limit: false }), { text: "3.2 GB · unprotected ~95 min", kind: "" });
  assert.deepEqual(protectedInfo({ protected_gb: 9.8, loop_minutes: 60, over_limit: true }), { text: "9.8 GB · unprotected ~60 min", kind: "warn" });
  assert.deepEqual(protectedInfo({ protected_gb: 0 }), { text: "0 GB", kind: "" });
  assert.deepEqual(protectedInfo({}), { text: "—", kind: "" }); // older camera service
  assert.deepEqual(protectedInfo(null), { text: "—", kind: "" });
});

test("storage warning: protected recordings take X of the Y GB limit, only ~N min kept; the front's numbers first", () => {
  const front = { protected_gb: 9.84, loop_minutes: 61, over_limit: true };
  const rear = { protected_gb: 2.5, loop_minutes: 70, over_limit: true };
  const w = storageWarning(front, rear, 10);
  assert.match(w, /^Protected recordings take 9\.8 GB of the 10 GB recordings limit: only the newest ~61 min of unprotected footage is kept/);
  assert.match(w, /Unprotect recordings you no longer need\.$/);
  assert.match(storageWarning({ over_limit: false }, rear, 10), /take 2\.5 GB of the 10 GB/); // only the rear squeezed
  assert.match(storageWarning(front, null, undefined), /of the recordings limit:/);
  assert.match(storageWarning({ over_limit: true }, null, 10), /^Protected recordings fill the 10 GB recordings limit: only the newest hour or less/);
  assert.equal(storageWarning({ over_limit: false, protected_gb: 9 }, { over_limit: false }, 10), null);
  assert.equal(storageWarning(null, null, 10), null);
  assert.equal(storageWarning({ state: "recording" }, undefined, 10), null); // older camera service: no field
});

test("delete all recordings: confirmation from the server's count and the result", () => {
  const est = { deleted: 41, freed_bytes: 10.63e9, protected: 3, protected_bytes: 0.9e9, kept_recording: 2 };
  assert.equal(deleteAllText(est, false), "41 recordings of both cameras (10.6 GB) will be deleted permanently.");
  assert.equal(deleteAllText(est, true), "38 recordings of both cameras (9.7 GB) will be deleted permanently.");
  assert.equal(deleteAllText({ deleted: 1, freed_bytes: 45e6 }, false), "1 recording of both cameras (45 MB) will be deleted permanently.");
  assert.equal(deleteAllText({ deleted: 2, freed_bytes: 5e8, protected: 2, protected_bytes: 5e8 }, true), null); // all protected
  assert.equal(deleteAllText({ deleted: 0, freed_bytes: 0 }, false), null);
  assert.equal(deleteAllText(null, false), null);
  assert.equal(deletedText({ deleted: 38, freed_bytes: 9.73e9, kept_protected: 3 }), "Deleted 38 recordings, 9.7 GB freed · 3 protected kept");
  assert.equal(deletedText({ deleted: 1, freed_bytes: 2e5, kept_protected: 0 }), "Deleted 1 recording, 1 MB freed");
  assert.equal(deletedText({ deleted: 0 }), "Nothing was deleted");
  assert.equal(sizeText(0), "0 MB");
  assert.equal(sizeText(undefined), "0 MB");
});
