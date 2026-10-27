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

// ── опрос геймпада только при подключённом геймпаде ──────────────────────

function fakeEnv(pads) {
  const env = { getGamepads: 0, listeners: [] };
  // в node 21+ navigator — свойство только с геттером: подменяем определением
  Object.defineProperty(globalThis, "navigator", {
    value: { getGamepads: () => { env.getGamepads++; return pads(); } }, configurable: true, writable: true,
  });
  globalThis.addEventListener = (type) => env.listeners.push(["add", type]);
  globalThis.removeEventListener = (type) => env.listeners.push(["remove", type]);
  globalThis.document = { createElement: () => ({ hidden: true, innerHTML: "" }) };
  return env;
}

test("no gamepad present: getGamepads() is never called (Chromium would poll at 250 Hz)", async () => {
  const { GamesMenu } = await import("../../web/display/ui/games-menu.js");
  const env = fakeEnv(() => []);
  const m = new GamesMenu({ appendChild() {} }, (p) => "/" + p);
  for (let i = 0; i < 10; i++) m.poll(i / 60, 0, false);
  assert.equal(env.getGamepads, 0);
  assert.deepEqual(env.listeners, [], "nothing to stop: polling never started");
});

test("pad plugged in: polled every frame; unplugged: polling stopped once (add + remove gamepadconnected)", async () => {
  const { GamesMenu } = await import("../../web/display/ui/games-menu.js");
  const env = fakeEnv(() => [pad("", [12])]);
  const m = new GamesMenu({ appendChild() {} }, (p) => "/" + p);
  m.poll(0, 0, true);
  m.poll(0.1, 0, true);
  assert.equal(env.getGamepads, 2);
  assert.equal(m.polling, true);
  m.poll(0.2, 0, false);
  m.poll(0.3, 0, false);
  assert.equal(env.getGamepads, 2);
  assert.deepEqual(env.listeners, [["add", "gamepadconnected"], ["remove", "gamepadconnected"]]);
  assert.equal(m.polling, false);
});

test("menu still opens with a pad: hold PS for 0.8 s", async () => {
  const { GamesMenu } = await import("../../web/display/ui/games-menu.js");
  fakeEnv(() => [pad("", [12])]);
  globalThis.fetch = async () => ({ json: async () => ({ games: [{ id: "doom", title: "Doom", system: "Doom" }] }) });
  const m = new GamesMenu({ appendChild() {} }, (p) => "/" + p);
  m.poll(0, 0, true);
  assert.equal(m.open, false);
  m.poll(0.85, 0, true);
  assert.equal(m.open, true);
  m.poll(0.9, 0, false); // геймпад выдернули — меню закрывается
  assert.equal(m.open, false);
});
