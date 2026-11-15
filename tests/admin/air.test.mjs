// Air quality (web/admin/js/air-fmt.js): zones, chart range, gradient stops, paths.
import test from "node:test";
import assert from "node:assert/strict";

const { METRICS, metric, levelOf, fmtValue, seriesStats, chartRange, zoneStops, zoneLines, linePath, bandPath, dayMarks, fmtAge }
  = await import("../../web/admin/js/air-fmt.js");

test("air: every value has a zone — fine, raised, off the scale", () => {
  const co2 = metric("co2_ppm"), temp = metric("temp_c"), hum = metric("humidity_pct");
  assert.deepEqual([400, 999, 1000, 1499, 1500, 4000].map((v) => levelOf(co2, v)), ["good", "good", "warn", "warn", "bad", "bad"]);
  assert.deepEqual([5, 16, 20, 27, 35].map((v) => levelOf(temp, v)), ["bad", "warn", "good", "warn", "bad"]);
  assert.deepEqual([15, 25, 45, 65, 80].map((v) => levelOf(hum, v)), ["bad", "warn", "good", "warn", "bad"]);
  assert.equal(levelOf(co2, null), "");
  assert.deepEqual([fmtValue(co2, 812.4), fmtValue(temp, 21.46), fmtValue(temp, undefined)], ["812", "21.5", "—"]);
  assert.equal(METRICS.length, 5);
});

test("air: stats and the chart range", () => {
  const s = { min: [800, null, 700], avg: [900, null, 1000], max: [1000, null, 1600] };
  assert.deepEqual(seriesStats(s), { min: 700, max: 1600, avg: 950 });
  assert.equal(seriesStats({ min: [null], avg: [null], max: [null] }), null);
  const co2 = metric("co2_ppm");
  const r = chartRange(co2, { min: 700, max: 1600, avg: 950 });
  assert.ok(r.lo < 700 && r.hi > 1600 && r.lo >= 0);
  const flat = chartRange(co2, { min: 800, max: 810, avg: 805 });
  assert.ok(flat.hi - flat.lo >= co2.span, "a flat line is not blown up to full height");
  assert.ok(chartRange(metric("pm25"), { min: 1, max: 3, avg: 2 }).lo === 0);
  assert.ok(chartRange(metric("temp_c"), { min: -8, max: -2, avg: -5 }).lo < -8, "temperature may go below zero");
});

test("air: gradient stops follow the zones inside the range", () => {
  const co2 = metric("co2_ppm");
  const st = zoneStops(co2, 500, 2000);
  assert.deepEqual([...new Set(st.map((x) => x.level))], ["bad", "warn", "good"], "top to bottom");
  assert.ok(st.every((x, i) => i === 0 || x.offset >= st[i - 1].offset) && st[0].offset === 0 && st[st.length - 1].offset === 1);
  assert.deepEqual(zoneStops(co2, 500, 900).map((x) => x.level), ["good", "good"], "one zone — one colour");
  assert.deepEqual(zoneLines(co2, 500, 2000), [{ value: 1000, level: "warn" }, { value: 1500, level: "bad" }]);
  assert.deepEqual(zoneLines(co2, 500, 900), []);
});

test("air: the line breaks at gaps, the band is closed pieces", () => {
  const d = linePath([10, 20, null, 30, null, 15, 15], 60, 100, 0, 100);
  assert.equal((d.match(/M/g) || []).length, 3, "three pieces: a run, a lone point, a run");
  assert.ok(d.startsWith("M0.0 90.0L10.0 80.0"));
  assert.equal(linePath([], 60, 100, 0, 100), "");
  const b = bandPath([1, 2, null, 3, 4], [5, 6, null, 7, 8], 40, 100, 0, 10);
  assert.equal((b.match(/Z/g) || []).length, 2);
});

test("air: day marks and age", () => {
  const t0 = new Date(2026, 10, 9, 15, 0, 0).getTime() / 1000;
  const marks = dayMarks(t0, t0 + 7 * 86400);
  assert.equal(marks.length, 7);
  assert.equal(new Date(marks[0].t * 1000).getHours(), 0);
  assert.equal(marks[0].label, "TUE");
  assert.deepEqual([fmtAge(null), fmtAge(30), fmtAge(600), fmtAge(7200)], ["no data", "just now", "10 min ago", "2 h ago"]);
});
