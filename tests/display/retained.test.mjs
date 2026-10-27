// Перерисовка по изменению: web/display/map/retained.js (запись кадра, сравнение, проигрывание).
import test from "node:test";
import assert from "node:assert/strict";

import { RecordingContext, RetainedLayer, bumpEpoch, isStaticImage, replay } from "../../web/display/map/retained.js";

/** «Настоящий» контекст для тестов: журнал вызовов и присваиваний, measureText по длине строки и шрифту. */
function logCtx(name = "cv") {
  const log = [];
  const st = { font: "10px sans-serif", globalAlpha: 1 };
  const stack = [];
  const canvas = { name, width: 768, height: 576 };
  const grad = (kind, args) => {
    const g = { kind, args, stops: [], addColorStop: (o, c) => { g.stops.push([o, c]); log.push(["stop", o, c]); } };
    return g;
  };
  const ctx = new Proxy({}, {
    get(_, k) {
      if (k === "canvas") return canvas;
      if (k === "log") return log;
      if (k === "measureText") {
        return (t) => {
          log.push(["measureText", st.font, t]);
          const px = parseFloat(/(\d+(?:\.\d+)?)px/.exec(st.font)?.[1] ?? "10");
          return { width: String(t).length * px * 0.5, actualBoundingBoxAscent: px * 0.7 };
        };
      }
      if (k === "save") return () => { stack.push({ ...st }); log.push(["save"]); };
      if (k === "restore") return () => { const s = stack.pop(); if (s) Object.assign(st, s); log.push(["restore"]); };
      if (k === "createLinearGradient") return (...a) => { log.push(["LG", ...a]); return grad("LG", a); };
      if (k === "createRadialGradient") return (...a) => { log.push(["RG", ...a]); return grad("RG", a); };
      if (k in st) return st[k];
      return (...a) => { log.push([k, ...a.map((v) => (Array.isArray(v) ? `[${v}]` : v))]); };
    },
    set(_, k, v) {
      st[k] = v;
      log.push(["=" + k, v && typeof v === "object" ? `${v.kind}(${v.args})` : v]);
      return true;
    },
    has: () => true, // как у настоящего контекста: свойства есть всегда ("font" in ctx)
  });
  return ctx;
}

const isState = (e) => ["save", "restore"].includes(e[0]) || e[0] === "measureText";
/** Журнал без служебных save/restore/measureText — только то, что влияет на пиксели. */
const pixels = (log) => log.filter((e) => !isState(e)).map((e) => JSON.stringify(e));

function scene(ctx, v = {}) {
  ctx.save();
  ctx.globalAlpha *= 0.5;
  ctx.fillStyle = v.fill ?? "#123456";
  ctx.fillRect(10, 20, 30, 40);
  ctx.beginPath();
  ctx.moveTo(1, 2);
  ctx.lineTo(v.x ?? 3, 4);
  ctx.arc(5, 6, 7, 0, Math.PI);
  ctx.lineWidth = 3;
  ctx.setLineDash([4, 2]);
  ctx.stroke();
  const g = ctx.createLinearGradient(0, 0, 10, 0);
  g.addColorStop(0, "#000");
  g.addColorStop(1, v.stop ?? "#fff");
  ctx.fillStyle = g;
  ctx.font = "bold 20px Test";
  const w = ctx.measureText("abc").width;
  ctx.fillText("abc", w, 5);
  ctx.strokeText("x", 1, 2, 50);
  if (v.img) ctx.drawImage(v.img, 1, 2, 3, 4);
  ctx.restore();
}

const FULL = { x: 0, y: 0, w: 768, h: 576 };

test("replay draws exactly what direct drawing draws", () => {
  const direct = logCtx();
  scene(direct);
  const real = logCtx();
  const rec = new RecordingContext(real);
  rec._begin();
  scene(rec);
  real.log.length = 0; // measureText во время записи — не рисование
  replay(real, rec.ops);
  assert.deepEqual(pixels(real.log), pixels(direct.log));
});

test("properties read back like a real context: globalAlpha *= , invalid values ignored, transform kept", () => {
  const rec = new RecordingContext(logCtx());
  rec._begin();
  rec.globalAlpha *= 0.5;
  assert.equal(rec.globalAlpha, 0.5);
  rec.globalAlpha = 2; // вне 0..1 — настоящий контекст игнорирует
  rec.lineWidth = -1;
  assert.equal(rec.globalAlpha, 0.5);
  assert.equal(rec.lineWidth, 1);
  rec.save();
  rec.translate(10, 5);
  rec.scale(2, 2);
  const m = rec.getTransform();
  assert.deepEqual([m.a, m.d, m.e, m.f], [2, 2, 10, 5]);
  rec.restore();
  assert.equal(rec.getTransform().e, 0);
  assert.equal(rec.canvas.name, "cv", "canvas — настоящий: по нему ключуются трекеры alarm/level/radio");
});

test("measureText goes to the real context with the recorded font", () => {
  const real = logCtx();
  const rec = new RecordingContext(real);
  rec._begin();
  rec.font = "30px Test";
  assert.equal(rec.measureText("abcd").width, 60);
  assert.deepEqual(real.log.find((e) => e[0] === "measureText"), ["measureText", "30px Test", "abcd"]);
});

test("same frame again is skipped; a changed argument, colour stop or text is drawn", () => {
  const real = logCtx();
  const layer = new RetainedLayer(real);
  const frame = (t, v) => {
    scene(layer.begin(), v);
    return layer.end(t, FULL, 1);
  };
  assert.equal(frame(0, {}), true, "first frame");
  assert.equal(frame(0.1, {}), false, "same picture");
  assert.equal(frame(0.2, { x: 3.0000001 }), true, "path point moved");
  assert.equal(frame(0.3, { x: 3.0000001 }), false);
  assert.equal(frame(0.4, { x: 3.0000001, stop: "#eee" }), true, "gradient stop changed");
  assert.equal(frame(0.5, { x: 3.0000001, stop: "#eee", fill: "#654321" }), true);
  assert.deepEqual(layer.stats, { drawn: 4, skipped: 2 });
});

test("replay clears the frame area first and leaves the context state as it was", () => {
  const real = logCtx();
  const layer = new RetainedLayer(real);
  scene(layer.begin());
  real.log.length = 0;
  layer.end(0, FULL, 2);
  const first = real.log.slice(0, 4).map((e) => e[0]);
  assert.deepEqual(first, ["save", "setTransform", "clearRect", "save"]);
  assert.deepEqual(real.log.find((e) => e[0] === "clearRect"), ["clearRect", 0, 0, 768, 576]);
  const saves = real.log.filter((e) => e[0] === "save").length;
  const restores = real.log.filter((e) => e[0] === "restore").length;
  assert.equal(saves, restores);
});

test("images: sprites (fnavStatic) compare by reference, anything else always redraws", () => {
  const sprite = { fnavStatic: true };
  const live = { width: 10 }; // обычный холст: содержимое может измениться
  assert.equal(isStaticImage(sprite), true);
  assert.equal(isStaticImage(live), false);
  const layer = new RetainedLayer(logCtx());
  const frame = (t, img) => {
    scene(layer.begin(), { img });
    return layer.end(t, FULL, 1);
  };
  frame(0, sprite);
  assert.equal(frame(0.1, sprite), false);
  assert.equal(frame(0.2, { fnavStatic: true }), true, "another sprite object");
  assert.equal(frame(0.3, live), true);
  assert.equal(frame(0.4, live), true, "mutable source: never skipped");
});

test("Path2D and other objects in arguments make the frame volatile", () => {
  const layer = new RetainedLayer(logCtx());
  const path = { kind: "Path2D" };
  const frame = (t) => {
    const r = layer.begin();
    r.fill(path);
    return layer.end(t, FULL, 1);
  };
  frame(0);
  assert.equal(frame(0.1), true);
});

test("safety net: an unchanged frame is still redrawn every refreshS; font loading forces a redraw", () => {
  const layer = new RetainedLayer(logCtx(), { refreshS: 2 });
  const frame = (t) => {
    scene(layer.begin());
    return layer.end(t, FULL, 1);
  };
  frame(0);
  assert.equal(frame(1.9), false);
  assert.equal(frame(2.0), true);
  assert.equal(frame(2.5), false);
  bumpEpoch();
  assert.equal(frame(2.6), true);
  layer.invalidate();
  assert.equal(frame(2.7), true, "canvas resized: redraw");
});

test("a moving box (wind): old area cleared, empty frames cost nothing", () => {
  const real = logCtx();
  const layer = new RetainedLayer(real);
  const frame = (t, box) => {
    const r = layer.begin();
    if (box) r.fillRect(box.x, box.y, 1, 1);
    real.log.length = 0;
    const drawn = layer.end(t, box, 1);
    return { drawn, clear: real.log.find((e) => e[0] === "clearRect") ?? null };
  };
  frame(0, { x: 10, y: 10, w: 100, h: 100 });
  const moved = frame(0.1, { x: 30, y: 10, w: 100, h: 100 });
  assert.deepEqual(moved.clear, ["clearRect", 10, 10, 120, 100], "union of the old and the new box");
  const gone = frame(0.2, null);
  assert.deepEqual(gone, { drawn: true, clear: ["clearRect", 30, 10, 100, 100] });
  assert.deepEqual(frame(0.3, null), { drawn: false, clear: null });
  assert.deepEqual(frame(5, null), { drawn: false, clear: null }, "nothing on the canvas: no refresh either");
});

test("the whole HUD records and replays without unsupported calls", async () => {
  // спрайты рисуются на OffscreenCanvas — в node его нет: подставляем журналирующий контекст
  globalThis.OffscreenCanvas = class {
    constructor(w, h) {
      this.width = w;
      this.height = h;
    }
    getContext() {
      return logCtx("sprite");
    }
  };
  const ui = await import("../../web/display/ui/index.js");
  const { DEFAULT_SETTINGS } = await import("../../web/display/map/defaults.js");
  const state = (now) => ({
    now,
    telemetry: { gps: { fix: "3d", sats_used: 9, sats_visible: 17, speed_mps: 0 }, heading: { deg: 123, source: "mag" },
      display: { mode: "map" }, camera: { recording: true }, system: { internet: true, utc_offset_min: 60 },
      alarm: { state: "armed", seq: 1 }, level: { sensor_ok: true, calibrated: true, pitch_deg: 1.2, roll_deg: -0.7 },
      radio: null, errors: [] },
    weather: { temp_c: 7, code: 3, wind_ms: 1.8, wind_from_deg: 150, days: [] },
    settings: { display: { ...DEFAULT_SETTINGS.display } },
    view: { mode: "map", bearingDeg: 120, headingDeg: 123, compassHeadingDeg: 123, speedMps: 0, hasFix: true, connected: true,
      internet: true, recording: true, wallMs: Date.UTC(2026, 9, 27, 19, 43), vehicleX: 384, vehicleY: 386 },
    canvas: { w: 768, h: 576, safe: { x: 38.4, y: 28.8, w: 691.2, h: 518.4 }, pxPerMm: 8.35 },
  });
  for (const t of [0, 3.5, 3.6]) {
    ui.drawHud(logCtx("warm"), state(t)); // кэши ширин текста и спрайты — общие: прогреть, чтобы журналы сравнимы
    const direct = logCtx("hud");
    ui.drawHud(direct, state(t));
    const real = logCtx("hud2");
    const rec = new RecordingContext(real);
    rec._begin();
    ui.drawHud(rec, state(t));
    real.log.length = 0;
    replay(real, rec.ops);
    assert.deepEqual(pixels(real.log), pixels(direct.log), `t=${t}`);
  }
  // ветер отдельно + HUD без ветра = те же вызовы, что drawHud с ветром
  const s = state(3.6);
  const both = logCtx("a");
  ui.drawHud(both, s);
  const split = logCtx("b");
  const box = ui.drawWind(split, s);
  ui.drawHud(split, s, { wind: false });
  const strip = (log) => pixels(log).filter((e) => !e.startsWith('["setLineDash"') && !e.startsWith('["=globalCompositeOperation"'));
  assert.ok(box && box.w > 0 && box.h > 0);
  assert.equal(strip(split.log).length, strip(both.log).length);
  assert.equal(ui.drawWind(logCtx(), { ...s, view: { ...s.view, mode: "camera" } }), null, "no wind in camera view");
});
