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

test("the rear view called up by the camera key wakes the armed screen; front again — standby again", () => {
  const s = DEFAULT_SETTINGS;
  assert.equal(armedStandby({ ...tel("armed"), display: { camera: "rear" } }, s), false);
  assert.equal(armedStandby({ ...tel("armed"), display: { camera: "front" } }, s), true);
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

// ── визитка хозяина: логотип, телефон, бегущая строка ──

/** Контекст-заглушка: пишет вызовы; ширина текста — по размеру шрифта (с запасом: ~0.62 em на знак). */
function recCtx(calls = []) {
  const st = { font: "10px sans-serif", globalAlpha: 1 };
  return new Proxy({}, {
    get(_, k) {
      if (k === "measureText") {
        return (t) => {
          const px = parseFloat(/(\d+(?:\.\d+)?)px/.exec(st.font)?.[1] ?? "10");
          return { width: String(t).length * px * 0.62, actualBoundingBoxAscent: px * 0.72 };
        };
      }
      if (k in st) return st[k];
      if (typeof k === "string" && k.startsWith("create")) return () => ({ addColorStop() {} }); // градиенты заклёпок
      return (...a) => calls.push([k, ...a]);
    },
    set(_, k, v) {
      st[k] = v;
      calls.push(["=" + k, v]);
      return true;
    },
  });
}

/** OffscreenCanvas-заглушка: спрайты theme.js рисуются в записывающий контекст. */
class FakeCanvas {
  constructor(w, h) {
    this.width = w;
    this.height = h;
    this.calls = [];
  }
  getContext() {
    return recCtx(this.calls);
  }
}

test("owner card: the phone exactly as on ptrs.ltd, the 'contact the owner' line, animated", async () => {
  const { drawStandby, OWNER } = await import("../../web/display/ui/standby.js");
  const { clearSprites } = await import("../../web/display/ui/theme.js");
  assert.equal(OWNER.phone, "+48 792 556 939");
  assert.match(OWNER.ticker, /CONTACT THE OWNER/);
  assert.match(OWNER.ticker, /^[\x20-\x7e]+$/, "screen fonts are Latin only");
  globalThis.OffscreenCanvas = FakeCanvas;
  clearSprites();
  try {
    const calls = [];
    const r = drawStandby(recCtx(calls), { now: 3, view: { wallMs: Date.UTC(2026, 10, 14, 12, 0) }, canvas: { w: 768, h: 576 } });
    assert.equal(r.animated, true, "the marquee moves: app.js draws standby frames by rAF");
    assert.ok(calls.some((c) => c[0] === "fillText" && c[1] === OWNER.phone), "phone drawn");
    // бегущая строка — спрайт (fnavStatic: запись кадра сравнивает его по ссылке), в нём — текст строки
    const tick = calls.filter((c) => c[0] === "drawImage" && c[1].calls?.some((d) => d[0] === "fillText" && d[1] === OWNER.ticker));
    assert.ok(tick.length >= 1, "ticker sprite blitted");
    assert.ok(tick.every((c) => c[1].fnavStatic === true));
  } finally {
    globalThis.OffscreenCanvas = undefined;
    clearSprites();
  }
});

test("ticker offset: runs left at a steady whole-px pace, wraps every period, no pause", async () => {
  const { tickerOffset } = await import("../../web/display/ui/standby.js");
  assert.equal(tickerOffset(0, 1000), 0);
  assert.equal(tickerOffset(1, 1000), 60);
  assert.equal(tickerOffset(1 / 30, 1000), 2, "2 px per frame at 30 fps");
  assert.equal(tickerOffset(1000 / 60, 1000), 0, "wraps after one period");
  assert.equal(tickerOffset(1000 / 60 + 0.5, 1000), 30);
  for (const t of [0.013, 7.7, 123.456]) assert.ok(Number.isInteger(tickerOffset(t, 777)));
  assert.equal(tickerOffset(NaN, 1000), 0);
  assert.equal(tickerOffset(5, 0), 0);
});

test("standby layout: logo, ALARM ON, clock and phone stay in the 5% safe area at every minute shift", async () => {
  const { standbyLayout, standbyDrift } = await import("../../web/display/ui/standby.js");
  const W = 768, H = 576;
  const safe = { x0: W * 0.05, y0: H * 0.05, x1: W * 0.95, y1: H * 0.95 };
  const inside = (r, what) => {
    assert.ok(r.x >= safe.x0 && r.y >= safe.y0 && r.x + r.w <= safe.x1 && r.y + r.h <= safe.y1, `${what} ${JSON.stringify(r)}`);
  };
  for (let m = 0; m < 10; m++) {
    const d = standbyDrift(m * 60000);
    const L = standbyLayout(recCtx(), W, H, d);
    inside(L.logo, `logo m${m}`);
    inside(L.clock, `clock m${m}`);
    inside(L.phone, `phone m${m}`);
    inside({ x: L.alarm.lockX - 16, y: L.alarm.y - 22, w: L.alarm.w, h: 44 }, `alarm m${m}`);
    assert.ok(L.ticker.rule1 >= safe.y0 && L.ticker.rule2 + 2 <= safe.y1, `ticker m${m}`);
    // ничего не налезает: логотип над телефоном, телефон над строкой, столбик правее логотипа
    assert.ok(L.logo.y + L.logo.h <= L.phone.y, "logo above the phone");
    assert.ok(L.phone.y + L.phone.h <= L.ticker.rule1, "phone above the ticker");
    assert.ok(L.logo.x + L.logo.w < L.clock.x && L.logo.x + L.logo.w < L.alarm.lockX - 16, "column right of the logo");
    // крупно: логотип ≥ половины высоты экрана, цифры телефона ≥ 60 px (минимум текста — 25 px)
    assert.ok(L.logo.h >= H / 2);
    assert.ok(parseFloat(/(\d+)px/.exec(L.phone.font)[1]) >= 60);
  }
});

test("owner logo: loaded asynchronously (first frames without it), then drawn as a static sprite", async () => {
  const { drawStandby, OWNER } = await import("../../web/display/ui/standby.js");
  const { clearSprites } = await import("../../web/display/ui/theme.js");
  const loads = [];
  globalThis.Image = class {
    set src(v) {
      this._src = v;
      loads.push(v);
      queueMicrotask(() => this.onload?.());
    }
    get src() {
      return this._src;
    }
  };
  globalThis.OffscreenCanvas = FakeCanvas;
  clearSprites();
  try {
    const state = { now: 0, view: { wallMs: 0 }, canvas: { w: 768, h: 576 } };
    const logoBlits = (calls) => calls.filter((c) => c[0] === "drawImage" && c[1].calls?.some((d) => d[0] === "drawImage" && d[1] instanceof globalThis.Image));
    const c1 = [];
    drawStandby(recCtx(c1), state);
    assert.equal(logoBlits(c1).length, 0, "not loaded yet: the frame still renders, without the logo");
    assert.equal(loads.length, 1);
    assert.match(loads[0], /\/display\/ui\/img\/ptrs-logo\.png$/);
    assert.equal(loads[0], OWNER.logoUrl);
    await new Promise((r) => setTimeout(r, 0));
    const c2 = [];
    drawStandby(recCtx(c2), state);
    const b = logoBlits(c2);
    assert.equal(b.length, 1, "logo drawn once loaded");
    assert.equal(b[0][1].fnavStatic, true, "a theme.js sprite, not a raw <img>");
    assert.equal(loads.length, 1, "loaded once");
    const c3 = [];
    drawStandby(recCtx(c3), state);
    assert.equal(logoBlits(c3)[0][1], b[0][1], "same sprite every frame (retained compares it by reference)");
  } finally {
    delete globalThis.Image;
    globalThis.OffscreenCanvas = undefined;
    clearSprites();
  }
});
