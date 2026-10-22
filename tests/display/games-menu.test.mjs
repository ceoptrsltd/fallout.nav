import test from "node:test";
import assert from "node:assert/strict";
import { readPad } from "../../web/display/ui/games-menu.js";

const pad = (mapping, pressed = [], axes = []) => ({
  mapping,
  buttons: Array.from({ length: 17 }, (_, i) => ({ pressed: pressed.includes(i) })),
  axes: Array.from({ length: 8 }, (_, i) => axes[i] ?? 0),
});

test("raw PS4-style layout: Cross=1, Circle=2, PS=12, D-pad on hat axes 6/7", () => {
  assert.deepEqual(readPad(pad("", [12])), { ok: false, back: false, home: true, up: false, down: false });
  assert.equal(readPad(pad("", [1])).ok, true);
  assert.equal(readPad(pad("", [2])).back, true);
  assert.equal(readPad(pad("", [], { 7: -1 })).up, true);
  assert.equal(readPad(pad("", [], { 7: 1 })).down, true);
  assert.equal(readPad(pad("", [], { 1: 0.9 })).down, true); // левый стик
  assert.equal(readPad(pad("", [], { 1: 0.3 })).down, false);
});

test("standard mapping: A=0, B=1, Home=16, D-pad 12/13", () => {
  const s = readPad(pad("standard", [0, 16, 13]));
  assert.equal(s.ok, true);
  assert.equal(s.home, true);
  assert.equal(s.down, true);
  assert.equal(readPad(pad("standard", [1])).back, true);
});
