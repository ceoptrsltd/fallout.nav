// Экран «на охране»: web/display/map/standby.js (когда), web/display/ui/standby.js (что рисуем).
import test from "node:test";
import assert from "node:assert/strict";

import { armedStandby, standbyDelayMs } from "../../web/display/map/standby.js";
import { DEFAULT_SETTINGS } from "../../web/display/map/defaults.js";

const tel = (state) => ({ alarm: state === undefined ? null : { state } });

test("standby only while armed: not arming, not triggered, not off, not without the alarm service", () => {
  const s = DEFAULT_SETTINGS;
  assert.equal(DEFAULT_SETTINGS.display.armed_standby, true);
  assert.equal(armedStandby(tel("armed"), s), true);
  for (const st of ["arming", "triggered", "off"]) assert.equal(armedStandby(tel(st), s), false, st);
  assert.equal(armedStandby(tel(), s), false);
  assert.equal(armedStandby(null, s), false, "no telemetry yet");
  const off = { display: { ...s.display, armed_standby: false } };
  assert.equal(armedStandby(tel("armed"), off), false, "switched off in the settings");
});

test("standby redraw timer: to the start of the next second", () => {
  assert.equal(standbyDelayMs(10_000), 1015);
  assert.equal(standbyDelayMs(10_999), 16);
  assert.equal(standbyDelayMs(10_400), 615);
});

test("standby screen: black, dim ALARM ON, the clock; the block drifts once a minute", async () => {
  globalThis.OffscreenCanvas = undefined; // спрайты не нужны (null — blit пропускает)
  const { drawStandby, standbyDrift } = await import("../../web/display/ui/standby.js");
  const calls = [];
  const st = { font: "10px sans-serif", globalAlpha: 1 };
  const ctx = new Proxy({}, {
    get(_, k) {
      if (k === "measureText") return (t) => ({ width: String(t).length * 20, actualBoundingBoxAscent: 30 });
      if (k in st) return st[k];
      return (...a) => calls.push([k, ...a]);
    },
    set(_, k, v) {
      st[k] = v;
      calls.push(["=" + k, v]);
      return true;
    },
  });
  const wallMs = Date.UTC(2026, 9, 27, 19, 43, 10);
  const state = { telemetry: { system: { utc_offset_min: 60, time_synced: true } }, view: { wallMs }, canvas: { w: 768, h: 576 } };
  const r = drawStandby(ctx, state);
  assert.deepEqual(calls.find((c) => c[0] === "fillRect"), ["fillRect", 0, 0, 768, 576]);
  assert.ok(calls.some((c) => c[0] === "fillText" && c[1] === "ALARM ON"));
  assert.ok(calls.some((c) => c[0] === "fillText" && c[1] === "0"), "clock digits (20:43)");
  assert.ok(r.x >= 0 && r.y >= 0 && r.x + r.w <= 768 && r.y + r.h <= 576);
  // сдвиг меняется раз в минуту и не уводит блок далеко
  const d0 = standbyDrift(wallMs), d1 = standbyDrift(wallMs + 59_000 - (wallMs % 60000)), d2 = standbyDrift(wallMs + 60_000);
  assert.deepEqual(d0, d1);
  assert.notDeepEqual(d0, d2);
  for (let m = 0; m < 10; m++) {
    const d = standbyDrift(m * 60000);
    assert.ok(Math.abs(d.dx) <= 30 && Math.abs(d.dy) <= 20);
  }
});
