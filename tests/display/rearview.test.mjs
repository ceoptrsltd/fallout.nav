// Задний вид на экране в машине: web/display/map/rearview.js (какая камера, есть ли картинка),
// web/display/map/camera.js (слой видео: источник, зеркало, без стоп-кадра), web/display/ui/rear.js (REAR, NO SIGNAL).
import test from "node:test";
import assert from "node:assert/strict";

import { REAR_STALL_S, REAR_WAIT_S, RearSignal, rearMirror, rearStatusKind, viewCamera } from "../../web/display/map/rearview.js";
import { CameraLayer } from "../../web/display/map/camera.js";
import { RecordingContext, RetainedLayer } from "../../web/display/map/retained.js";

/** Контекст-журнал: вызовы и присваивания; measureText — по длине строки и кеглю. */
function logCtx(name = "cv") {
  const log = [];
  const st = { font: "10px sans-serif", globalAlpha: 1 };
  const stack = [];
  const canvas = { name, width: 768, height: 576 };
  const grad = () => ({ addColorStop: () => {} });
  return new Proxy({}, {
    get(_, k) {
      if (k === "canvas") return canvas;
      if (k === "log") return log;
      if (k === "measureText") {
        return (t) => {
          const px = parseFloat(/(\d+(?:\.\d+)?)px/.exec(st.font)?.[1] ?? "10");
          return { width: String(t).length * px * 0.6, actualBoundingBoxAscent: px * 0.7 };
        };
      }
      if (k === "save") return () => stack.push({ ...st });
      if (k === "restore") return () => { const s = stack.pop(); if (s) Object.assign(st, s); };
      if (k === "createLinearGradient" || k === "createRadialGradient") return grad;
      if (k in st) return st[k];
      return (...a) => log.push([k, ...a]);
    },
    set(_, k, v) {
      st[k] = v;
      return true;
    },
    has: () => true,
  });
}
// спрайты HUD рисуются на OffscreenCanvas — в node его нет
globalThis.OffscreenCanvas = class {
  constructor(w, h) {
    this.width = w;
    this.height = h;
  }
  getContext() {
    return logCtx("sprite");
  }
};
const texts = (log) => log.filter((e) => e[0] === "fillText").map((e) => e[1]);

// ── какая камера ────────────────────────────────────────────────────────

test("view camera: rear only in camera mode with display.camera = rear; mirror unless rear_mirror is false", () => {
  assert.equal(viewCamera({ display: { mode: "camera", camera: "rear", reason: "rear" } }), "rear");
  assert.equal(viewCamera({ display: { mode: "camera", camera: "front" } }), "front");
  assert.equal(viewCamera({ display: { mode: "map", camera: "rear" } }), "front", "inconsistent: map wins");
  assert.equal(viewCamera({ display: { mode: "camera" } }), "front", "old core: no field");
  assert.equal(viewCamera(null), "front");
  assert.equal(rearMirror({ display: { rear_mirror: true } }), true);
  assert.equal(rearMirror({ display: { rear_mirror: false } }), false);
  assert.equal(rearMirror({ display: {} }), true, "default as rear_camera.mirror");
});

test("rear status: what it says about the video", () => {
  assert.equal(rearStatusKind(null), "down", "rear camera off in the settings");
  assert.equal(rearStatusKind({ state: "disabled" }), "down");
  assert.equal(rearStatusKind({ state: "stopped" }), "down");
  assert.equal(rearStatusKind({ state: "error", error: "Rear camera: no video for 15 s" }), "down");
  assert.equal(rearStatusKind({ state: "error", fps_actual: 12.4, error: "No space left" }), "ok", "recording error, frames still come");
  assert.equal(rearStatusKind({ state: "starting" }), "starting");
  assert.equal(rearStatusKind({ state: "live" }), "ok", "record = false: no fps_actual at all");
  assert.equal(rearStatusKind({ state: "recording", fps_actual: 25 }), "ok");
  assert.equal(rearStatusKind({ state: "recording", fps_actual: null }), "stall");
  assert.equal(rearStatusKind({ state: "recording" }), "stall");
  // основного потока нет (запись стоит), а второй даёт картинку: экран её показывает
  const noMain = "Rear camera: no main stream (Invalid data): not recording, the picture comes from the second stream";
  assert.equal(rearStatusKind({ state: "error", error: noMain, video_age_s: 0.2 }), "ok");
  assert.equal(rearStatusKind({ state: "starting", video_age_s: 0.1 }), "ok", "main still connecting, second already shows");
  assert.equal(rearStatusKind({ state: "error", error: noMain, video_age_s: 4.5 }), "down", "second stream stopped too");
  assert.equal(rearStatusKind({ state: "error", video_age_s: null }), "down");
});

// ── есть ли картинка ────────────────────────────────────────────────────

const REC = { state: "recording", fps_actual: 25 };
const img = (o = {}) => ({ loaded: false, failed: false, connectedAt: 0, ...o });

test("rear signal: black while the first frame is on its way, CONNECTING if it takes long, then the picture", () => {
  const s = new RearSignal();
  assert.equal(s.update(REC, img({ connectedAt: 10 }), 10), "wait");
  assert.equal(s.update(REC, img({ connectedAt: 10 }), 10 + REAR_WAIT_S - 0.01), "wait");
  assert.equal(s.update(REC, img({ connectedAt: 10 }), 10 + REAR_WAIT_S), "connecting");
  assert.equal(s.update(REC, img({ connectedAt: 10, loaded: true }), 12), "ok");
  assert.equal(s.recovered, false);
});

test("rear signal: no camera, stopped or failed stream — NO SIGNAL; the camera starting — CONNECTING", () => {
  const s = new RearSignal();
  assert.equal(s.update(null, img({ loaded: true }), 1), "no_signal");
  assert.equal(s.update({ state: "error" }, img({ loaded: true }), 2), "no_signal");
  assert.equal(s.update({ state: "starting" }, img({ loaded: true }), 3), "connecting");
  assert.equal(s.update(REC, img({ failed: true }), 4), "no_signal", "the <img> failed (503): retried every 2 s");
  assert.equal(s.update({ state: "error", fps_actual: 20 }, img({ loaded: true }), 5), "ok", "only the recording failed");
});

test("rear signal: recording without frames is a frozen picture after REAR_STALL_S, not at the first poll", () => {
  const s = new RearSignal();
  const stall = { state: "recording", fps_actual: null };
  const ok = img({ loaded: true });
  assert.equal(s.update(REC, ok, 0), "ok");
  assert.equal(s.update(stall, ok, 1), "ok");
  assert.equal(s.update(stall, ok, 1 + REAR_STALL_S - 0.05), "ok");
  assert.equal(s.update(stall, ok, 1 + REAR_STALL_S), "no_signal");
  assert.equal(s.update(stall, ok, 9), "no_signal");
  // кадры снова идут: переподключить поток (старый мог оборваться молча) и ждать его первый кадр
  assert.equal(s.update(REC, ok, 10), "wait");
  assert.equal(s.recovered, true);
  assert.equal(s.update(REC, img({ connectedAt: 10 }), 10.1), "wait");
  assert.equal(s.recovered, false, "only once");
  // один опрос без fps (1 с) — не стоп-кадр
  assert.equal(s.update(stall, ok, 20), "ok");
  assert.equal(s.update(REC, ok, 21), "ok");
  assert.equal(s.update(stall, ok, 22), "ok", "the stall clock starts again");
});

test("rear signal: recovered fires on bad → good, not on the first good status; reset starts clean", () => {
  const s = new RearSignal();
  s.update(REC, img({ loaded: true }), 0);
  assert.equal(s.recovered, false);
  s.update({ state: "starting" }, img({ loaded: true }), 1);
  assert.equal(s.recovered, false);
  s.update(REC, img({ loaded: true }), 2);
  assert.equal(s.recovered, true, "starting → recording");
  s.update(null, img(), 3);
  s.reset();
  s.update(REC, img({ loaded: true }), 4);
  assert.equal(s.recovered, false, "a new rear view connects anyway");
});

// ── слой видео ──────────────────────────────────────────────────────────

function fakeEl() {
  const classes = new Set();
  const attrs = {};
  const on = {};
  return {
    classes,
    hidden: true,
    classList: {
      toggle(c, force) {
        const v = force === undefined ? !classes.has(c) : !!force;
        if (v) classes.add(c);
        else classes.delete(c);
        return v;
      },
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
    },
    style: { props: {}, setProperty(k, v) { this.props[k] = v; } },
    addEventListener: (k, f) => (on[k] = f),
    getAttribute: (k) => attrs[k] ?? null,
    removeAttribute: (k) => delete attrs[k],
    get src() {
      return attrs.src ?? "";
    },
    set src(v) {
      attrs.src = String(v);
    },
    fire: (k) => on[k]?.(),
  };
}

function layer(opts = {}) {
  const els = { layer: fakeEl(), img: fakeEl(), msg: fakeEl() };
  const cam = new CameraLayer(els.layer, els.img, els.msg, {
    src: "/api/camera/mjpeg",
    rearSrc: "/api/camera/rear/mjpeg",
    retryS: 0.01,
    lingerS: 0.02,
    ...opts,
  });
  return { cam, ...els };
}
const srcOf = (img) => (img.getAttribute("src") ?? "").replace(/[?&]_=\d+$/, "");
const cls = (el) => [...el.classes].sort().join(" ");

test("camera layer: the front as before — cover, no mirror; hidden only until its first frame", () => {
  const { cam, img, msg } = layer();
  cam.setCamera("front", true);
  cam.setVisible(true, 0);
  assert.equal(srcOf(img), "/api/camera/mjpeg");
  assert.equal(cls(img), "blank", "nothing to show yet");
  img.fire("load");
  assert.equal(cls(img), "");
  assert.equal(cam.ok, true);
  img.fire("error");
  assert.equal(msg.hidden, false, "NO CAMERA, as before");
  cam.suspend(true);
});

test("camera layer: rear — its own stream at once, mirrored, the old frame hidden until the new one", () => {
  const { cam, img } = layer();
  cam.setVisible(true, 0);
  img.fire("load");
  cam.setCamera("rear", true);
  assert.equal(srcOf(img), "/api/camera/rear/mjpeg");
  assert.equal(cls(img), "blank mirror rear");
  img.fire("load");
  assert.equal(cls(img), "mirror rear");
  cam.setCamera("rear", false);
  assert.equal(cls(img), "rear", "rear_mirror off: not mirrored");
  cam.setHold(true);
  assert.equal(cls(img), "blank rear", "no signal: no frozen frame");
  cam.setHold(false);
  const before = img.getAttribute("src");
  cam.setCamera("rear", false);
  assert.equal(img.getAttribute("src"), before, "repeated telemetry does not reconnect");
  // обратно — сразу передняя, без зеркала и без кадра задней
  cam.setCamera("front", false);
  assert.equal(srcOf(img), "/api/camera/mjpeg");
  assert.equal(cls(img), "blank");
  img.fire("load");
  assert.equal(cls(img), "");
  cam.suspend(true);
});

test("camera layer: rear errors leave NO CAMERA to the HUD; retry goes to the rear stream", async () => {
  const { cam, img, msg } = layer();
  cam.setCamera("rear", true);
  cam.setVisible(true, 0);
  img.fire("error");
  assert.equal(cam.failed, true);
  assert.equal(msg.hidden, true);
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(srcOf(img), "/api/camera/rear/mjpeg");
  assert.equal(cam.failed, true, "until a frame comes");
  img.fire("load");
  assert.equal(cam.failed, false);
  const t0 = cam.connectedAt;
  await new Promise((r) => setTimeout(r, 5));
  cam.reconnect();
  assert.ok(cam.connectedAt > t0);
  assert.equal(cam.loaded, false);
  cam.suspend(true);
});

test("camera layer: a stream kept open after leaving the camera view is dropped when the camera changes", () => {
  const { cam, img } = layer();
  cam.setVisible(true, 0);
  img.fire("load");
  cam.setVisible(false, 1); // поток ещё lingerS открыт
  assert.equal(cam.streaming, true);
  cam.setCamera("rear", true);
  assert.equal(cam.streaming, false);
  assert.equal(img.getAttribute("src"), null);
  cam.setVisible(true, 2);
  assert.equal(srcOf(img), "/api/camera/rear/mjpeg");
  cam.suspend(true);
});

test("camera layer: no rear source (demo) — always the front", () => {
  const { cam, img } = layer({ rearSrc: null });
  cam.setCamera("rear", true);
  cam.setVisible(true, 0);
  assert.equal(cam.which, "front");
  assert.equal(srcOf(img), "/api/camera/mjpeg");
  assert.equal(img.classes.has("mirror"), false);
  cam.suspend(true);
});

// ── HUD ─────────────────────────────────────────────────────────────────

test("rear tag goes between the clock and the compass, as near the centre as fits; below them if no room", async () => {
  const { rearTagPlace } = await import("../../web/display/ui/rear.js");
  const safe = { x: 38.4, y: 28.8, w: 691.2, h: 518.4 };
  const clock = { x: 41, y: 32, w: 280, h: 60 };
  const v1 = { x: 540, y: 20, w: 190, h: 220 };
  const r = rearTagPlace(safe, 140, 48, [v1, clock]);
  assert.equal(r.y, 32);
  assert.ok(r.x >= clock.x + clock.w + 10 && r.x + r.w <= v1.x - 10, JSON.stringify(r));
  const wide = rearTagPlace(safe, 140, 48, [{ x: 600, y: 20, w: 120, h: 120 }]);
  assert.equal(wide.x, Math.round(384 - 70), "free top: centred");
  const v2 = { x: 274, y: 20, w: 220, h: 220 };
  const r2 = rearTagPlace(safe, 140, 48, [v2, { x: 41, y: 32, w: 220, h: 60 }]);
  assert.ok(r2.x >= v2.x + v2.w + 10 && r2.x + r2.w <= safe.x + safe.w, "compass in the middle: right of it");
  // v2: лента компаса почти во всю ширину, часы под ней — метка правее часов, на их высоте
  const tape = { x: 165, y: 20, w: 435, h: 110 };
  const clock2 = { x: 41, y: 140, w: 280, h: 60 };
  const r3 = rearTagPlace(safe, 140, 48, [tape, clock2]);
  assert.equal(r3.y, 140);
  assert.equal(r3.x, clock2.x + clock2.w + 10);
  const full = rearTagPlace(safe, 140, 48, [{ x: 0, y: 0, w: 768, h: 100 }]);
  assert.ok(full.y >= 110, "no gap: below");
});

test("HUD: REAR tag in the rear view; NO SIGNAL / CONNECTING only without a picture", async () => {
  const ui = await import("../../web/display/ui/index.js");
  const { drawRearSignal } = await import("../../web/display/ui/rear.js");
  const safe = { x: 38.4, y: 28.8, w: 691.2, h: 518.4 };
  for (const sig of ["ok", "wait", null]) {
    const g = logCtx();
    assert.equal(drawRearSignal(g, { view: { rearSignal: sig } }, safe), null);
    assert.equal(g.log.length, 0, String(sig));
  }
  const g = logCtx();
  const r = drawRearSignal(g, { view: { rearSignal: "no_signal" } }, safe);
  assert.deepEqual(texts(g.log), ["REAR CAMERA", "NO SIGNAL"]);
  assert.ok(r.x > safe.x && r.x + r.w < safe.x + safe.w);
  const g2 = logCtx();
  drawRearSignal(g2, { view: { rearSignal: "connecting" } }, safe);
  assert.ok(texts(g2.log).includes("CONNECTING"));

  const st = state(5, { camera: "rear", rearSignal: "ok" });
  const h = logCtx();
  ui.drawHud(h, st);
  assert.ok(texts(h.log).includes("REAR"));
  assert.ok(!texts(h.log).includes("NO SIGNAL"));
  const h2 = logCtx();
  ui.drawHud(h2, state(5, { camera: "rear", rearSignal: "no_signal" }));
  assert.ok(texts(h2.log).includes("NO SIGNAL"));
  const h3 = logCtx();
  ui.drawHud(h3, state(5, { camera: "front", rearSignal: null }));
  assert.ok(!texts(h3.log).includes("REAR"), "front camera: no tag");
});

function state(now, { camera = "front", rearSignal = null, mode = "camera" } = {}) {
  return {
    now,
    telemetry: { gps: { fix: "3d", sats_used: 9, sats_visible: 17 }, heading: { deg: 40, source: "gps" },
      display: { mode, camera }, camera: { recording: false }, system: { internet: true }, errors: [] },
    weather: null,
    settings: { display: { compass_style: "v1", orientation: "heading_up", safe_area_pct: 5 } },
    view: { mode, camera, rearSignal, orientation: "heading_up", bearingDeg: 40, headingDeg: 40, compassHeadingDeg: 40,
      speedMps: 3, hasFix: true, connected: true, internet: true, recording: false, wallMs: Date.UTC(2026, 9, 28, 2, 0) },
    canvas: { w: 768, h: 576, safe: { x: 38.4, y: 28.8, w: 691.2, h: 518.4 }, pxPerMm: 8.35 },
  };
}

test("retained HUD: the rear view is redrawn only when the tag or the message changes", async () => {
  const ui = await import("../../web/display/ui/index.js");
  const real = logCtx("hud");
  const layer = new RetainedLayer(real);
  const frame = (st) => {
    const rec = layer.begin();
    ui.drawHud(rec, st);
    return layer.end(st.now, { x: 0, y: 0, w: 768, h: 576 });
  };
  const t = 7;
  for (let i = 0; i < 3; i++) frame(state(t, { camera: "rear", rearSignal: "ok" })); // прогрев кэшей
  assert.equal(layer.rec instanceof RecordingContext, true);
  assert.equal(frame(state(t, { camera: "rear", rearSignal: "ok" })), false, "same picture: skipped");
  assert.equal(frame(state(t, { camera: "rear", rearSignal: "wait" })), false, "waiting for a frame draws nothing new");
  assert.equal(frame(state(t, { camera: "rear", rearSignal: "no_signal" })), true, "NO SIGNAL appears");
  assert.equal(frame(state(t, { camera: "rear", rearSignal: "no_signal" })), false);
  assert.equal(frame(state(t, { camera: "rear", rearSignal: "ok" })), true, "picture is back");
  assert.equal(frame(state(t, { camera: "front" })), true, "back to the front: the tag goes at once");
  assert.equal(frame(state(t, { camera: "front" })), false);
});

test("camera layer: the rear view fits the CRT's visible area (display.safe_area_pct)", () => {
  const { cam, layer: el } = layer();
  cam.setSafeArea(5);
  assert.equal(el.style.props["--safe"], "5%", "CSS #camera.rear: inside 5 % margins on every side");
  cam.setSafeArea(7.5);
  assert.equal(el.style.props["--safe"], "7.5%");
  cam.setSafeArea(99);
  assert.equal(el.style.props["--safe"], "20%", "clamped like the setting");
});

test("camera layer: the front without frames (by its status) — no frozen frame, NO CAMERA; frames again — picture", () => {
  const { cam, img, msg } = layer();
  cam.setVisible(true, 0);
  img.fire("load");
  assert.equal(cls(img), "");
  cam.setNoSignal(true);
  assert.equal(cls(img), "blank", "the last frame is hidden");
  assert.equal(msg.hidden, false, "NO CAMERA");
  cam.setNoSignal(false);
  assert.equal(cls(img), "");
  assert.equal(msg.hidden, true);
  cam.setNoSignal(true);
  cam.setCamera("rear", true); // the rear view has its own signal (HUD): the front's flag does not carry over
  assert.equal(cam.noSignal, false);
  assert.equal(msg.hidden, true);
  cam.suspend(true);
});
