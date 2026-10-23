import test from "node:test";
import assert from "node:assert/strict";

import { clockParts } from "../../web/display/ui/clock.js";
import { DEFAULT_SETTINGS } from "../../web/display/map/defaults.js";

const at = (iso, system) => clockParts({ telemetry: { system } }, Date.parse(iso));

test("clockParts: device time zone from utc_offset_min, zero-padded 24h time, short weekday and date", () => {
  // 2026-10-23 11:05 UTC → Warsaw (CEST, +120) 13:05, Friday
  assert.deepEqual(at("2026-10-23T11:05:00Z", { utc_offset_min: 120, time_synced: true }),
    { hh: "13", mm: "05", day: "FRI", date: "23 OCT", synced: true });
  // the offset moves the date too: 23:30 UTC + 2 h = Saturday 01:30
  assert.deepEqual(at("2026-10-23T23:30:00Z", { utc_offset_min: 120 }),
    { hh: "01", mm: "30", day: "SAT", date: "24 OCT", synced: true });
  // negative offsets and the year boundary
  assert.deepEqual(at("2027-01-01T02:00:00Z", { utc_offset_min: -300 }),
    { hh: "21", mm: "00", day: "THU", date: "31 DEC", synced: true });
});

test("clockParts: without an offset the browser clock is used; unsynced time is flagged", () => {
  const ms = Date.parse("2026-10-23T11:05:00Z");
  const d = new Date(ms);
  const p = clockParts({ telemetry: {} }, ms);
  assert.equal(p.hh, String(d.getHours()).padStart(2, "0"));
  assert.equal(p.mm, "05");
  assert.equal(p.synced, true);
  assert.equal(at("2026-10-23T11:05:00Z", { utc_offset_min: 0, time_synced: false }).synced, false);
  assert.equal(clockParts({}, ms).synced, true); // no telemetry yet — not reported as unsynced
});

test("clockParts: view.wallMs is the default time source", () => {
  const p = clockParts({ view: { wallMs: Date.parse("2026-03-02T08:09:00Z") }, telemetry: { system: { utc_offset_min: 60 } } });
  assert.deepEqual(p, { hh: "09", mm: "09", day: "MON", date: "2 MAR", synced: true });
});

test("display.show_clock is on by default", () => {
  assert.equal(DEFAULT_SETTINGS.display.show_clock, true);
});
