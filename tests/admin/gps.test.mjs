// GPS page: speed with its accuracy, the NAV-PVT hint, gpsd's fixed port (web/admin/js/gps-fmt.js).
import test from "node:test";
import assert from "node:assert/strict";

import { GPSD_DEVICE, speedAccText, fixLine, needsPvt, deviceMismatch } from "../../web/admin/js/gps-fmt.js";

test("speedAccText: two decimals under 1 m/s, one above; nothing when unknown", () => {
  assert.equal(speedAccText(0.254), "±0.25 m/s");
  assert.equal(speedAccText(0), "±0.00 m/s");
  assert.equal(speedAccText(2.46), "±2.5 m/s");
  assert.equal(speedAccText(null), "");
  assert.equal(speedAccText(undefined), "");
  assert.equal(speedAccText(-1), "");
  assert.equal(speedAccText(NaN), "");
});

test("fixLine: speed next to its error estimate", () => {
  assert.equal(fixLine({ fix: "3d", hdop: 0.912, speed_mps: 12.345, eps_mps: 0.32 }), "fix 3D · HDOP 0.91 · 12.3 m/s ±0.32 m/s");
  assert.equal(fixLine({ fix: "2d", hdop: 1.5, speed_mps: 0, eps_mps: null }), "fix 2D · HDOP 1.50 · 0.0 m/s");
  assert.equal(fixLine({ fix: "none", hdop: null, speed_mps: null }), "fix none · HDOP — · — m/s"); // older core: no eps_mps
  assert.equal(fixLine(null), "");
});

test("needsPvt: a fix without the accuracy from a core that has the field", () => {
  assert.equal(needsPvt({ fix: "3d", eps_mps: null }), true);
  assert.equal(needsPvt({ fix: "3d", eps_mps: 0.3 }), false);
  assert.equal(needsPvt({ fix: "none", eps_mps: null }), false); // no fix: unknown anyway
  assert.equal(needsPvt({ fix: "3d" }), false); // older core behind the remote web copy
  assert.equal(needsPvt(null), false);
});

test("deviceMismatch: only a port other than gpsd's", () => {
  assert.equal(GPSD_DEVICE, "/dev/gps0");
  assert.equal(deviceMismatch("/dev/gps0"), false);
  assert.equal(deviceMismatch("/dev/ttyAMA5"), true);
  assert.equal(deviceMismatch(""), false);
  assert.equal(deviceMismatch(undefined), false);
});
