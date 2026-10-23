import test from "node:test";
import assert from "node:assert/strict";

import { RADIO_H, dialPosition, marqueeOffset, radioRect, radioVisible, trackLine } from "../../web/display/ui/radio.js";
import { SPEED_BOX } from "../../web/display/ui/speed.js";
import { INDICATOR_ROW } from "../../web/display/ui/status.js";
import { CONNECT_S, DEMO_STATIONS, STATION_S, TRACK_S, demoRadioStatus } from "../../web/display/demo/radio.js";
import { DEFAULT_SETTINGS } from "../../web/display/map/defaults.js";

const SAFE = { x: 768 * 0.05, y: 576 * 0.05, w: 768 * 0.9, h: 576 * 0.9 };
const st = (radio, display = {}) => ({ telemetry: { radio }, settings: { display } });

test("radioVisible: playing / connecting / waiting_output; display.radio_panel=false hides", () => {
  for (const s of ["playing", "connecting", "waiting_output"]) assert.equal(radioVisible(st({ state: s })), true, s);
  for (const s of ["stopped", "error"]) assert.equal(radioVisible(st({ state: s })), false, s);
  assert.equal(radioVisible(st(null)), false);
  assert.equal(radioVisible({}), false);
  assert.equal(radioVisible(st({ state: "playing" }, { radio_panel: false })), false);
  assert.equal(DEFAULT_SETTINGS.display.radio_panel, true);
});

test("radioRect: between the speed box and the indicators, bottom of the safe area, two indicator rows high", () => {
  const r = radioRect(SAFE, 136);
  assert.equal(r.h, RADIO_H);
  assert.equal(RADIO_H, 2 * INDICATOR_ROW.h + INDICATOR_ROW.gap + 6);
  assert.ok(r.x >= SAFE.x + SPEED_BOX.w + 8, "clears the speedometer");
  assert.ok(r.x + r.w <= SAFE.x + SAFE.w - 136 - 8, "clears the indicators");
  assert.equal(r.y + r.h, Math.round(SAFE.y + SAFE.h));
  assert.ok(r.w > 250, `wide enough for a station name (${r.w})`);
  // поднимается над подписью Esri
  assert.equal(radioRect(SAFE, 136, 17).y, r.y - 17);
  // the "NO NET" row (third from the bottom) stays above the panel
  const noNetBottom = SAFE.y + SAFE.h - 2 * (INDICATOR_ROW.h + INDICATOR_ROW.gap);
  assert.ok(r.y >= Math.floor(noNetBottom) - 1);
});

test("dialPosition: stable per station, within the dial", () => {
  const a = dialPosition("Wasteland FM");
  assert.equal(a, dialPosition("Wasteland FM"));
  assert.notEqual(a, dialPosition("Mojave Wave 101"));
  for (const n of ["", null, "x", "Радио Шансон", "a".repeat(200)]) {
    const p = dialPosition(n);
    assert.ok(p >= 0.08 && p <= 0.92, `${n}: ${p}`);
  }
});

test("marqueeOffset: no scroll when it fits; pause, steady run, seamless loop", () => {
  assert.equal(marqueeOffset(10, 100, 200), 0);
  assert.equal(marqueeOffset(1.0, 400, 200), 0); // pause first
  assert.equal(marqueeOffset(2.6, 400, 200, { speed: 34, pause: 1.6, gap: 56 }), 34);
  const run = (400 + 56) / 34;
  // just before the end of the run the text has moved by (textW + gap): the second copy sits where the first began
  assert.ok(Math.abs(marqueeOffset(1.6 + run - 1e-6, 400, 200) - 456) < 0.01);
  assert.equal(marqueeOffset(1.6 + run + 0.5, 400, 200), 0); // next pause
  assert.equal(marqueeOffset(1.6 + run + 1.6 + 1, 400, 200), 34); // next run
  assert.equal(marqueeOffset(-5, 400, 200), 0);
});

test("trackLine: track, else genre ON AIR, else ON AIR", () => {
  assert.deepEqual(trackLine({ track: " A - B " }), { text: "A - B", isTrack: true });
  assert.deepEqual(trackLine({ track: null, genre: "Jazz" }), { text: "JAZZ · ON AIR", isTrack: false });
  assert.deepEqual(trackLine({ track: "", genre: null }), { text: "ON AIR", isTrack: false });
  assert.deepEqual(trackLine(null), { text: "ON AIR", isTrack: false });
});

test("demo radio: tracks change, stations switch via connecting, one station without titles", () => {
  const a = demoRadioStatus(1);
  assert.equal(a.state, "playing");
  assert.equal(a.station, DEMO_STATIONS[0].name);
  assert.equal(a.track, DEMO_STATIONS[0].tracks[0]);
  assert.equal(demoRadioStatus(TRACK_S + 1).track, DEMO_STATIONS[0].tracks[1]);
  const c = demoRadioStatus(STATION_S + 1);
  assert.equal(c.state, "connecting");
  assert.equal(c.station, DEMO_STATIONS[1].name);
  assert.equal(c.track, null);
  assert.equal(demoRadioStatus(STATION_S + CONNECT_S + 1).track, DEMO_STATIONS[1].tracks[0]);
  const j = demoRadioStatus(2 * STATION_S + CONNECT_S + 5);
  assert.equal(j.station, DEMO_STATIONS[2].name);
  assert.equal(j.track, null);
  assert.equal(j.genre, "Jazz");
  for (const k of ["state", "station", "track", "genre", "volume", "output", "output_connected"]) assert.ok(k in a, k);
});

test("satsText: fix, visible / used satellites", async () => {
  const { satsText } = await import("../../web/display/ui/status.js");
  assert.equal(satsText({ fix: "3d", sats_visible: 39, sats_used: 20 }), "3D 39/20");
  assert.equal(satsText({ fix: "2d", sats_visible: 7, sats_used: 4 }), "2D 7/4");
  assert.equal(satsText({ fix: "none", sats_visible: 3 }), "— 3/0");
  assert.equal(satsText(null), "— 0/0");
});
