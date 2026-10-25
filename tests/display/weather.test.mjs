import test from "node:test";
import assert from "node:assert/strict";

import { fmtTemp, weatherKind, weatherVisible, windScreenAngle } from "../../web/display/ui/weather.js";
import { DEFAULT_SETTINGS } from "../../web/display/map/defaults.js";

test("weatherKind: WMO codes to icon kinds", () => {
  const k = (c) => weatherKind(c);
  assert.deepEqual([0, 1, 2, 3, 45, 48, 51, 55, 61, 65, 80, 71, 86, 95, 99].map(k),
    ["clear", "partly", "partly", "cloudy", "fog", "fog", "drizzle", "drizzle", "rain", "rain", "rain", "snow", "snow",
      "thunder", "thunder"]);
  assert.equal(weatherKind(null), "cloudy");
});

test("fmtTemp: rounded, no negative zero", () => {
  assert.equal(fmtTemp(12.6), "13°");
  assert.equal(fmtTemp(-0.4), "0°");
  assert.equal(fmtTemp(-12.4), "-12°");
  assert.equal(fmtTemp(null), "--°");
});

test("weatherVisible: needs a temperature; display.weather_panel=false hides", () => {
  const st = (weather, display = {}) => ({ weather, settings: { display } });
  assert.equal(weatherVisible(st({ temp_c: 3 })), true);
  assert.equal(weatherVisible(st({ temp_c: null })), false);
  assert.equal(weatherVisible(st(null)), false);
  assert.equal(weatherVisible(st({ temp_c: 3 }, { weather_panel: false })), false);
  assert.equal(DEFAULT_SETTINGS.display.weather_panel, true);
  assert.equal(DEFAULT_SETTINGS.display.wind_on_map, true);
});

test("windScreenAngle: where the wind blows, relative to the rotated map", () => {
  const deg = (s) => Math.round((windScreenAngle(s) * 180) / Math.PI);
  const st = (from, bearing) => ({ weather: { wind_from_deg: from, wind_ms: 5 }, view: { bearingDeg: bearing } });
  assert.equal(deg(st(180, 0)), 0); // southerly wind on a north-up map: blows up the screen
  assert.equal(deg(st(0, 0)), 180); // northerly: down
  assert.equal(deg(st(90, 90)), 180); // heading east into an easterly wind: headwind, down the screen
  assert.equal(deg(st(270, 90)), 0); // heading east with a westerly: tailwind, up
  assert.equal(windScreenAngle({ weather: { wind_ms: 3 } }), null);
});

test("speed limit sign: shown from telemetry, blinks only clearly above the limit", async () => {
  const { overLimit, speedLimitKmh } = await import("../../web/display/ui/speed.js");
  const st = (speed_limit, display = {}) => ({ telemetry: { speed_limit }, settings: { display } });
  assert.equal(speedLimitKmh(st({ kmh: 50, src: "osm" })), 50);
  assert.equal(speedLimitKmh(st(null)), null);
  assert.equal(speedLimitKmh(st({ kmh: 50 }, { speed_limit: false })), null);
  assert.equal(overLimit(52.9, 50), false);
  assert.equal(overLimit(53.5, 50), true);
  assert.equal(overLimit(null, 50), false);
  assert.equal(DEFAULT_SETTINGS.display.speed_limit, true);
});
