// The device's clock in the admin (web/admin/js/devtime.js, rec-play.js): recording names are the DEVICE's local time,
// the phone may be in another zone (docs/contracts/api.md "Записи", fnav/core/timezone.py).
import test from "node:test";
import assert from "node:assert/strict";

import {
  setDeviceZone, deviceZone, validZone, zoneOffsetMs, wallToMs, deviceClock, fmtUtcOffset, zoneText,
} from "../../web/admin/js/devtime.js";
import { segmentStartMs, recStartMs, recLink, parseRecLink, linkOffset } from "../../web/admin/js/rec-play.js";

const PHONE_TZ = process.env.TZ;
const phoneIn = (tz) => { process.env.TZ = tz; };
test.afterEach(() => {
  setDeviceZone(null);
  if (PHONE_TZ === undefined) delete process.env.TZ;
  else process.env.TZ = PHONE_TZ;
});

// device in Warsaw (CET in November), a segment and an alarm event 2 min after its start
const SEG = "2026-11-02/2026-11-02_13-40-10.ts";
const T0 = Date.UTC(2026, 10, 2, 12, 40, 10) / 1000; // 13:40:10 CET
const EV = T0 + 120;

test("Play links: the offset is the same on a phone in any time zone", () => {
  setDeviceZone("Europe/Warsaw");
  for (const tz of ["Europe/Lisbon", "Europe/Helsinki", "Europe/Warsaw", "America/New_York", "Asia/Tokyo", "UTC"]) {
    phoneIn(tz);
    assert.equal(segmentStartMs(SEG), T0 * 1000, tz);
    assert.equal(parseRecLink(recLink(SEG, EV)).t, 120, tz);
  }
});

test("without the device's zone (older device, dev mock) — the browser's zone, as before", () => {
  assert.equal(setDeviceZone(undefined), null);
  phoneIn("Europe/Lisbon");
  assert.equal(parseRecLink(recLink(SEG, EV)).t, 0); // the bug: the event "before" the recording
  phoneIn("Europe/Helsinki");
  assert.equal(parseRecLink(recLink(SEG, EV)).t, 3720); // the bug: past the end of a 10-minute segment
  phoneIn("Europe/Warsaw");
  assert.equal(parseRecLink(recLink(SEG, EV)).t, 120); // the phone in the device's zone was always right
});

test("setDeviceZone keeps only zones the browser knows", () => {
  assert.equal(setDeviceZone("Europe/Lisbon"), "Europe/Lisbon");
  assert.equal(deviceZone(), "Europe/Lisbon");
  for (const bad of [null, "", "Mars/Olympus_Mons", 42, "../etc/passwd"]) {
    assert.equal(setDeviceZone(bad), null, String(bad));
  }
  assert.equal(validZone("UTC"), true);
  assert.equal(validZone("Nowhere/City"), false);
});

test("wall clock → unix in a zone, across summer time changes", () => {
  const W = "Europe/Warsaw";
  assert.equal(zoneOffsetMs(W, Date.UTC(2026, 0, 15)), 3600e3);
  assert.equal(zoneOffsetMs(W, Date.UTC(2026, 6, 15)), 7200e3);
  assert.equal(zoneOffsetMs("Nowhere/City", 0), null);
  assert.equal(wallToMs(2026, 7, 1, 12, 0, 0, W), Date.UTC(2026, 6, 1, 10, 0, 0));
  assert.equal(wallToMs(2026, 12, 31, 23, 59, 59, W), Date.UTC(2026, 11, 31, 22, 59, 59));
  // 25.10.2026: 03:00 CEST → 02:00 CET; 02:30 happens twice — the first one (like datetime.timestamp() on the device)
  assert.equal(wallToMs(2026, 10, 25, 1, 30, 0, W), Date.UTC(2026, 9, 24, 23, 30, 0));
  assert.equal(wallToMs(2026, 10, 25, 2, 30, 0, W), Date.UTC(2026, 9, 25, 0, 30, 0));
  assert.equal(wallToMs(2026, 10, 25, 3, 30, 0, W), Date.UTC(2026, 9, 25, 2, 30, 0));
  // 29.03.2026: 02:00 CET → 03:00 CEST; 02:30 does not exist — with the offset before the change
  assert.equal(wallToMs(2026, 3, 29, 2, 30, 0, W), Date.UTC(2026, 2, 29, 1, 30, 0));
  assert.equal(wallToMs(2026, 3, 29, 3, 30, 0, W), Date.UTC(2026, 2, 29, 1, 30, 0));
  assert.equal(wallToMs(2026, 11, 2, 13, 40, 10, "Europe/Lisbon"), Date.UTC(2026, 10, 2, 13, 40, 10));
  assert.equal(wallToMs(2026, 11, 2, 13, 40, 10, "Asia/Kolkata"), Date.UTC(2026, 10, 2, 8, 10, 10));
  // the default zone is the device's; null — the browser's
  setDeviceZone("Europe/Helsinki");
  phoneIn("Europe/Lisbon");
  assert.equal(wallToMs(2026, 11, 2, 13, 40, 10), Date.UTC(2026, 10, 2, 11, 40, 10));
  assert.equal(wallToMs(2026, 11, 2, 13, 40, 10, null), Date.UTC(2026, 10, 2, 13, 40, 10));
});

test("a recording made before the device changed its zone: start_ts and the event moment give the right offset", () => {
  const rec = { path: SEG, start: "2026-11-02T13:40:10", start_ts: T0 }; // written in Warsaw
  setDeviceZone("Europe/Lisbon"); // the van has since driven to Portugal
  phoneIn("Asia/Tokyo");
  assert.equal(recStartMs(rec), T0 * 1000);
  const link = parseRecLink(recLink(SEG, EV));
  assert.equal(link.t, 0); // by the name in today's zone — an hour off
  assert.equal(link.at, EV);
  assert.equal(linkOffset(link, rec), 120); // by the true start
  // no start_ts (recorded before the recorder kept it): its time in the device's zone, like the link
  const old = { path: SEG, start: "2026-11-02T13:40:10", start_ts: null };
  assert.equal(recStartMs(old), Date.UTC(2026, 10, 2, 13, 40, 10));
  assert.equal(linkOffset(link, old), 0);
  assert.equal(recStartMs({ path: SEG }), Date.UTC(2026, 10, 2, 13, 40, 10)); // only the name
  assert.equal(recStartMs({ path: "nosync/boot-abcd1234_0001.ts", start: null }), null);
  assert.equal(linkOffset({ play: "nosync/a.ts", t: null, at: null }, { path: "nosync/a.ts" }), null);
  assert.equal(linkOffset({ play: SEG, t: 7, at: null }, rec), 7); // an older link without "at"
  assert.equal(linkOffset(null, rec), null);
});

test("clock and zone texts in the device's zone", () => {
  phoneIn("America/New_York");
  setDeviceZone("Europe/Warsaw");
  assert.equal(deviceClock(EV), "13:42:10");
  assert.equal(deviceClock(EV, "Europe/Lisbon"), "12:42:10");
  assert.equal(deviceClock(NaN), "");
  setDeviceZone(null);
  assert.equal(deviceClock(EV), "07:42:10"); // the browser's
  assert.equal(fmtUtcOffset(60), "UTC+01:00");
  assert.equal(fmtUtcOffset(-210), "UTC−03:30");
  assert.equal(fmtUtcOffset(0), "UTC");
  assert.equal(fmtUtcOffset(null), "");
  assert.equal(zoneText({ timezone: "Europe/Lisbon", utc_offset_min: 0 }), "Europe/Lisbon (UTC)");
  assert.equal(zoneText({ timezone: "Europe/Warsaw", utc_offset_min: 60 }), "Europe/Warsaw (UTC+01:00)");
  assert.equal(zoneText({ timezone: null, utc_offset_min: 120 }), "UTC+02:00");
  assert.equal(zoneText(null), "—");
});
