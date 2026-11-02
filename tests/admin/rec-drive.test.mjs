// Driving in a recording: the GPS track at the playback time and impacts while driving (web/admin/js/rec-drive.js),
// docs/contracts/camera.md «Трек GPS записи», «Удар на ходу».
import test from "node:test";
import assert from "node:assert/strict";

import {
  GPS_MAX_GAP_S, gpsPath, gpxUrl, gpxName, cleanTrack, playUnix, sampleAt, lerpAngle, fmtKmh, fmtLatLon, trackLatLngs,
  splitMarks, impactText,
} from "../../web/admin/js/rec-drive.js";
import { setDeviceZone } from "../../web/admin/js/devtime.js";

const T0 = 1792507200; // 2026-10-20 14:40:00 UTC
const pt = (dt, extra = {}) => ({ t: T0 + dt, lat: 52 + dt * 1e-4, lon: 21, alt: 100, speed: 10 + dt, course: 90, fix: "3d", ...extra });

test("track URLs and the GPX name (device and remote prefix, rear camera)", () => {
  assert.equal(gpsPath("2026-10-20/2026-10-20_16-40-00.ts"), "/api/recordings/gps/2026-10-20/2026-10-20_16-40-00.ts");
  assert.equal(gpxUrl("2026-10-20/x.ts", "/"), "/api/recordings/gps/2026-10-20/x.ts?format=gpx");
  assert.equal(gpxUrl("rear/2026-10-20/a b.ts", "/fnadmin/"), "/fnadmin/api/recordings/gps/rear/2026-10-20/a%20b.ts?format=gpx");
  assert.equal(gpxName({ path: "2026-10-20/2026-10-20_16-40-00.ts", camera: "front" }), "2026-10-20_16-40-00.gpx");
  assert.equal(gpxName({ path: "rear/2026-10-20/2026-10-20_16-40-03.ts" }), "rear-2026-10-20_16-40-03.gpx");
});

test("playback time → unix: start_ts + currentTime; without it — the name in the DEVICE's zone; nosync — null", () => {
  assert.equal(playUnix({ path: "2026-10-20/2026-10-20_16-40-00.ts", start_ts: T0 + 0.25 }, 12.5), T0 + 12.75);
  assert.equal(playUnix({ path: "x.ts", start_ts: T0 }, -1), T0); // never before the start
  try {
    setDeviceZone("Europe/Warsaw"); // UTC+2 on 20 Oct 2026 (summer time ends on the 25th)
    assert.equal(playUnix({ path: "2026-10-20/2026-10-20_16-40-00.ts", start: "2026-10-20T16:40:00", start_ts: null }, 30), T0 + 30);
    setDeviceZone("Europe/Lisbon"); // the same name in another zone is another moment
    assert.equal(playUnix({ path: "2026-10-20/2026-10-20_15-40-00.ts", start: "2026-10-20T15:40:00" }, 0), T0);
  } finally {
    setDeviceZone(null);
  }
  assert.equal(playUnix({ path: "nosync/boot-abcd1234_0001.ts", start: null }, 10), null);
  assert.equal(playUnix({ path: "x.ts", start_ts: T0 }, NaN), null);
});

test("sampleAt: exact points, interpolation between them, the nearest across a gap, nothing far away", () => {
  const track = cleanTrack([pt(2), pt(0), pt(1), { t: T0 + 5, lat: 99, lon: 0 }, null, pt(3, { course: 350 }), pt(4, { course: 10 })]);
  assert.deepEqual(track.map((s) => s.t - T0), [0, 1, 2, 3, 4]); // sorted, the bad ones out
  assert.deepEqual(sampleAt(track, T0 + 1), { ...pt(1) });
  const mid = sampleAt(track, T0 + 1.5);
  assert.equal(mid.t, T0 + 1.5);
  assert.ok(Math.abs(mid.lat - (52 + 1.5e-4)) < 1e-9);
  assert.equal(mid.speed, 11.5);
  assert.equal(mid.alt, 100);
  assert.equal(mid.fix, "3d");
  assert.equal(Math.round(sampleAt(track, T0 + 3.5).course) % 360, 0); // 350° → 10° through north, not through south
  assert.ok(Math.abs(sampleAt(track, T0 + 3.25).course - 355) < 1e-9);
  // before the first / after the last point: the edge point while it is close, then nothing
  assert.equal(sampleAt(track, T0 - 2).t, T0);
  assert.equal(sampleAt(track, T0 - GPS_MAX_GAP_S - 0.1), null);
  assert.equal(sampleAt(track, T0 + 4 + GPS_MAX_GAP_S).t, T0 + 4);
  assert.equal(sampleAt(track, T0 + 4 + GPS_MAX_GAP_S + 0.1), null);
  // a gap in the track (no fix for a minute): the nearest point near its edges, nothing in the middle
  const gap = cleanTrack([pt(0), pt(1), pt(61), pt(62)]);
  assert.equal(sampleAt(gap, T0 + 2.5).t, T0 + 1);
  assert.equal(sampleAt(gap, T0 + 30), null);
  assert.equal(sampleAt(gap, T0 + 59).t, T0 + 61);
  // missing speed on one side: the nearest point's
  const partial = cleanTrack([pt(0, { speed: null, course: null }), pt(1)]);
  assert.equal(sampleAt(partial, T0 + 0.2).speed, null);
  assert.equal(sampleAt(partial, T0 + 0.8).speed, 11);
  assert.equal(sampleAt(partial, T0 + 0.8).course, 90);
  assert.equal(sampleAt([], T0), null);
  assert.equal(sampleAt(null, T0), null);
  assert.equal(sampleAt(track, NaN), null);
});

test("lerpAngle goes the short way round", () => {
  assert.equal(lerpAngle(10, 30, 0.5), 20);
  assert.equal(lerpAngle(350, 10, 0.5), 0);
  assert.equal(lerpAngle(10, 350, 0.25), 5);
  assert.equal(lerpAngle(90, 270, 0), 90);
});

test("formatting: km/h, coordinates, the polyline", () => {
  assert.equal(fmtKmh(13.9), "50 km/h");
  assert.equal(fmtKmh(0), "0 km/h");
  assert.equal(fmtKmh(null), "— km/h");
  assert.equal(fmtLatLon(52.229771, 21.011781), "52.22977, 21.01178");
  assert.equal(fmtLatLon(null, 21), "—");
  assert.deepEqual(trackLatLngs([pt(0), pt(1)]), [[52, 21], [52.0001, 21]]);
});

test("impacts while driving: split from the other marks, their text", () => {
  const marks = [{ kind: "motion", offset_s: 3 }, { kind: "impact", offset_s: 10, peak_g: 1.87 }, { kind: "sound", offset_s: 12 }];
  const { impacts, others } = splitMarks(marks);
  assert.deepEqual(impacts.map((m) => m.offset_s), [10]);
  assert.deepEqual(others.map((m) => m.kind), ["motion", "sound"]);
  assert.deepEqual(splitMarks(undefined), { impacts: [], others: [] });
  assert.equal(impactText(impacts[0]), "Impact 1.9 g while driving");
  assert.equal(impactText({ kind: "impact" }), "Impact while driving");
});
