// fallout.nav — экран /display: собственный рендер карты Esri на Canvas + видео + HUD.
// Контракт: docs/contracts/display.md. Рисование HUD — web/display/ui/index.js (агент C),
// запасной вариант — ./map/debug-hud.js.
//
// Параметры URL: ?pal=1 (устройство, 720×576, scaleX 0.9375), ?preview=1 (iframe веб-интерфейса),
// ?debug=1 (статистика), ?fps=30 (ограничить частоту кадров), ?dpr=2 (чёткость на ноутбуке),
// ?ui=debug (принудительно отладочный HUD), ?demo=1 (демо в браузере без fnav-core, docs/contracts/demo.md;
// ?camera=<url> — картинка «камеры» в демо).
// Удалённый доступ (страница не с устройства, а через сервер, docs/contracts/remote.md): адреса — от корня
// приложения, тайлы Esri — напрямую с arcgisonline (не через мобильный интернет машины), MJPEG — ~5 к/с.

import { DEFAULT_SETTINGS, deepMerge } from "./map/defaults.js";
import { TileStore, browserTileLoader } from "./map/tiles.js";
import { MapRenderer, BG_COLOR } from "./map/renderer.js";
import { activeLayers, planRequests } from "./map/layers.js";
import { CompassFilter, MotionModel, OrientationBlend, RotationRig, ZoomAnimator, vehicleAnchor } from "./map/motion.js";
import { Trail } from "./map/trail.js";
import { CameraLayer } from "./map/camera.js";
import { Link, wsUrl } from "./map/link.js";
import { REMOTE, appUrl } from "./map/root.js";
import { ESRI_ATTRIBUTION, esriTileUrl } from "./map/esri.js";
import { GamesMenu } from "./ui/games-menu.js";
import { clamp, makeView, tileLevelFor } from "./map/mercator.js";
import * as debugHud from "./map/debug-hud.js";

const W = 768;
const H = 576;
const params = new URLSearchParams(location.search);
const DEMO = params.get("demo") === "1";
const PAL = params.get("pal") === "1" && !DEMO; // демо — всегда без PAL-сжатия и без зеркала
const PREVIEW = params.get("preview") === "1";
const DEBUG = params.get("debug") === "1";
const FPS_CAP = Number(params.get("fps")) || 0;
const PR = clamp(Number(params.get("dpr")) || 1, 1, 3);
const FORCE_DEBUG_UI = params.get("ui") === "debug";
const DIRECT_TILES = DEMO || REMOTE; // Esri напрямую: на карте нужна подпись источников

const nowS = () => performance.now() / 1000;

// ───────────── разметка ─────────────
const root = document.getElementById("root");
const mapCanvas = document.getElementById("map");
const hudCanvas = document.getElementById("hud");
document.body.classList.toggle("pal", PAL);
document.body.classList.toggle("preview", PREVIEW);
for (const c of [mapCanvas, hudCanvas]) {
  c.width = W * PR;
  c.height = H * PR;
}
const mapCtx = mapCanvas.getContext("2d", { alpha: false });
// меню игр (геймпад) — только на самом устройстве
const gamesMenu = !DEMO && !REMOTE && !PREVIEW
  ? new GamesMenu(root, appUrl, (open) => { if (link) link.send({ type: "ui", games_menu: open }); })
  : null;
const hudCtx = hudCanvas.getContext("2d");

let mirrorX = false;   // settings.display.mirror_x (set in applySettings)
function layout() {
  if (PAL) {
    // 768 -> 720 for the PAL pixel aspect; display.mirror_x flips left-right for monitors that mirror
    // the picture themselves (rear-view CRTs such as the WAECO RV-50). Only on the device, not in previews.
    // The 768x576 canvas is squeezed into whatever the output really is: 720x576 (576i) -> 0.9375 x 1,
    // 720x288 (288p, progressive) -> 0.9375 x 0.5. The CRT stretches it back to the full 4:3 screen.
    const sx = (innerWidth || 720) / W, sy = (innerHeight || 576) / H;
    root.style.transformOrigin = "0 0";
    root.style.transform = mirrorX
      ? `translateX(${innerWidth || 720}px) scale(${-sx}, ${sy})`
      : `scale(${sx}, ${sy})`;
    return;
  }
  const s = Math.min(innerWidth / W, innerHeight / H);
  const x = (innerWidth - W * s) / 2;
  const y = (innerHeight - H * s) / 2;
  root.style.transform = `translate(${x}px, ${y}px) scale(${s})`;
}
layout();
addEventListener("resize", layout);

// ───────────── состояние ─────────────
let settings = DEFAULT_SETTINGS;
let settingsFromWs = false;
let layers = activeLayers(settings);
let telemetry = null;
let lastInternet = null;
let mapDirty = true;

const STUB_TELEMETRY = {
  type: "telemetry",
  t: 0,
  gps: { fix: "none", lat: null, lon: null, alt_m: null, speed_mps: null, course_deg: null, hdop: null, sats_used: 0, sats_visible: 0, time_utc: null, age_s: null },
  heading: { deg: null, source: "none", mag_deg: null, gps_course_deg: null, declination_deg: null },
  compass: null,
  display: { mode: "map", reason: "startup", target_zoom: 17, orientation: "heading_up" },
  camera: { state: "stopped", recording: false, frames_dropped: 0, recordings_count: 0, recordings_gb: 0, time_synced: false },
  disk: {},
  system: {},
  errors: [],
};

const motion = new MotionModel();
const zoomAnim = new ZoomAnimator(4.0);
const orient = new OrientationBlend(1.2); // только положение машины; поворот — RotationRig
const rig = new RotationRig();
const compass = new CompassFilter(); // курс для компаса — без задержек
const trail = new Trail();
const store = new TileStore({
  loader: browserTileLoader,
  now: nowS,
  capacity: 300,
  maxConcurrent: 6,
  onChange: () => {
    mapDirty = true;
  },
});
const renderer = new MapRenderer(store);
const camera = new CameraLayer(
  document.getElementById("camera-layer"),
  document.getElementById("camera"),
  document.getElementById("camera-msg"),
  DEMO
    ? { src: params.get("camera") || "camera-demo.jpg", cacheBust: false }
    : { src: appUrl("api/camera/mjpeg") + (REMOTE ? "?fps=5" : "") },
);

function maxZoom() {
  return settings.tiles?.max_zoom ?? 19;
}

function applySettings(s) {
  settings = deepMerge(DEFAULT_SETTINGS, s || {});
  layers = activeLayers(settings);
  const ds = settings.display;
  mirrorX = !!ds.mirror_x;
  layout();
  orient.set(ds.orientation);
  rig.setOrientation(ds.orientation);
  compass.configure(ds.compass_smoothing_s);
  rig.configure({
    vehicleLagS: ds.vehicle_rotation_lag_s,
    mapLagS: ds.map_rotation_lag_s,
    deadbandDeg: ds.map_rotation_deadband_deg,
  });
  if (!settings.display.show_trail) trail.clear();
  if (zoomAnim.target !== null) zoomAnim.setTarget(clamp(zoomAnim.target, 1, maxZoom()));
  mapDirty = true;
}
applySettings({});

function onMessage(msg) {
  if (msg.type === "telemetry") {
    const t = nowS();
    telemetry = msg;
    motion.ingest(msg, t);
    compass.ingest(msg.heading ? msg.heading.deg : null, t);
    const d = msg.display || {};
    if (Number.isFinite(d.target_zoom)) zoomAnim.setTarget(clamp(d.target_zoom, 1, maxZoom()));
    camera.setVisible(d.mode === "camera", t);
    const inet = msg.system ? msg.system.internet : null;
    if (inet === true && lastInternet === false) store.resetFailures(); // сеть вернулась — перезапросить
    lastInternet = inet;
  } else if (msg.type === "settings") {
    settingsFromWs = true;
    applySettings(msg.settings);
  }
}

let link;
let demo = null;
let tileUrlFor; // undefined → <root>tiles/... (fnav-core proxy)
if (DEMO) {
  // демо: телеметрия из симулятора в браузере, тайлы напрямую с Esri, без /api, /ws, /tiles
  const mod = await import("./demo/demo.js");
  demo = mod;
  tileUrlFor = (layer, z, x, y) => mod.esriTileUrl(settings, layer, z, x, y);
  link = mod.startDemo({
    onMessage,
    now: nowS,
    getSettings: () => settings,
    applyPatch: (patch) => applySettings(deepMerge(settings, patch)),
    getState: () => {
      const v = lastState && lastState.view;
      if (!v) return null;
      return {
        speedMps: v.speedMps,
        headingDeg: v.compassHeadingDeg ?? v.headingDeg,
        mode: v.mode,
        zoom: v.zoom,
        lat: motion.hasPos ? lastPos.lat : null,
        lon: motion.hasPos ? lastPos.lon : null,
      };
    },
  });
} else {
  if (REMOTE) tileUrlFor = (layer, z, x, y) => esriTileUrl(settings, layer, z, x, y);
  link = new Link(wsUrl("ws"), {
    onMessage,
    now: nowS,
    onOpen: () => store.resetFailures(),
  });
  link.start();

  fetch(appUrl("api/settings"), { credentials: "same-origin" })
    .then((r) => (r.ok ? r.json() : null))
    .then((s) => {
      if (s && !settingsFromWs) applySettings(s);
    })
    .catch(() => {});
}

// ───────────── HUD (агент C) с запасным вариантом ─────────────
let ui = { drawHud: debugHud.drawHud, drawVehicle: debugHud.drawVehicle, drawTrail: debugHud.drawTrail };
let uiName = "debug";
const uiErrors = { drawHud: 0, drawVehicle: 0, drawTrail: 0 };

async function loadExternalUi() {
  if (FORCE_DEBUG_UI) return;
  try {
    const mod = await import("./ui/index.js");
    if (typeof mod.drawHud !== "function") throw new Error("ui/index.js: no drawHud export");
    if (typeof mod.loadUi === "function") {
      await Promise.race([
        mod.loadUi(),
        new Promise((_, rej) => setTimeout(() => rej(new Error("loadUi: timeout 10 s")), 10000)),
      ]);
    }
    ui = {
      drawHud: mod.drawHud,
      drawVehicle: typeof mod.drawVehicle === "function" ? mod.drawVehicle : debugHud.drawVehicle,
      drawTrail: typeof mod.drawTrail === "function" ? mod.drawTrail : debugHud.drawTrail,
    };
    uiName = "ui";
    mapDirty = true;
  } catch (e) {
    console.warn("fnav: web/display/ui unavailable, using debug HUD", e);
  }
}

/** Вызов функции C с защитой: исключение не роняет кадр; при систематических — откат на отладочную. */
function callUi(name, ctx, ...args) {
  ctx.save();
  try {
    ui[name](ctx, ...args);
  } catch (e) {
    if (uiErrors[name]++ === 0) console.error(`fnav: ui.${name} failed`, e);
    if (typeof ctx.reset === "function") ctx.reset();
    mapDirty = true; // reset() мог стереть карту — перерисовать в следующем кадре
    ctx.setTransform(PR, 0, 0, PR, 0, 0);
    if (uiErrors[name] > 30 && ui[name] !== debugHud[name]) {
      console.error(`fnav: ui.${name} keeps failing, switching to debug HUD`);
      ui[name] = debugHud[name];
    }
  } finally {
    ctx.restore();
  }
}

// ───────────── кадр ─────────────
const safePct = () => (settings.display.safe_area_pct ?? 5) / 100;
let lastFrame = null;
let lastPlan = -1;
let lastPlanLevel = -1;
let lastDraw = { t: -1, cx: 0, cy: 0, zoom: 0, bearing: 0, ax: 0, ay: 0 };
let fpsEma = 0;
let drawnFrames = 0;
let hadPos = false;
let lastState = null;
let lastPos = { lat: null, lon: null };
let mapMsEma = 0;
let hudMsEma = 0;

/** Тайлы напрямую (демо, удалённо): атрибуция Esri на холсте (условия Esri), мелко, внизу по центру. */
function drawAttribution(ctx, state) {
  const l = settings.display.layers || {};
  const text = l.street_map_instead ? ESRI_ATTRIBUTION.street : ESRI_ATTRIBUTION.imagery;
  const sf = state.canvas.safe;
  ctx.save();
  ctx.font = '11px "DejaVu Sans", Arial, sans-serif';
  ctx.textAlign = "center";
  ctx.textBaseline = "bottom";
  const w = ctx.measureText(text).width + 10;
  const x = W / 2, y = sf.y + sf.h;
  ctx.fillStyle = "rgba(0,0,0,0.55)";
  ctx.fillRect(x - w / 2, y - 15, w, 15);
  ctx.fillStyle = "rgba(255,255,255,0.85)";
  ctx.fillText(text, x, y - 2);
  ctx.restore();
}

function needsMapRedraw(view, t, hasFix) {
  const d = lastDraw;
  if (mapDirty || !hasFix || t - d.t > 0.5) return true;
  const px = Math.hypot(view.cx - d.cx, view.cy - d.cy) * view.scale;
  return (
    px > 0.05 ||
    Math.abs(view.zoom - d.zoom) > 1e-4 ||
    Math.abs(view.bearing - d.bearing) > 0.02 ||
    Math.abs(view.ax - d.ax) > 0.05 ||
    Math.abs(view.ay - d.ay) > 0.05
  );
}

function frame(tms) {
  requestAnimationFrame(frame);
  const t = tms / 1000;
  if (FPS_CAP && lastFrame !== null && t - lastFrame < 1 / FPS_CAP - 0.004) return;
  const dt = lastFrame === null ? 0 : Math.min(0.25, t - lastFrame);
  if (dt > 0) fpsEma = fpsEma ? fpsEma + (1 / dt - fpsEma) * 0.05 : 1 / dt;
  lastFrame = t;

  const tel = telemetry || STUB_TELEMETRY;
  if (gamesMenu) gamesMenu.poll(t, tel.gps ? tel.gps.speed_mps : null);
  const m = motion.update(t);
  lastPos = m;
  const f = orient.update(dt);
  const zoom = zoomAnim.update(dt) ?? clamp(tel.display.target_zoom ?? 17, 1, maxZoom());
  const ds = settings.display;
  const rot = rig.update(dt, m.headingTarget, m.speed ?? 0);
  const compassDeg = compass.update(t);
  const pose = { ...vehicleAnchor(f, W, H, ds.vehicle_anchor_y ?? 0.67), bearing: rot.bearing, vehicleRot: rot.vehicleRot };
  const view = makeView({ cx: m.mx, cy: m.my, zoom, bearing: pose.bearing, ax: pose.ax, ay: pose.ay, w: W, h: H });
  const hasFix = !!(tel.gps && tel.gps.fix && tel.gps.fix !== "none" && tel.gps.lat != null);
  const connected = link.connected(3);
  const mode = tel.display.mode === "camera" ? "camera" : "map";

  if (ds.show_trail && m.hasPos && hasFix) {
    trail.add(m.mx, m.my, t);
  }
  trail.prune(t, ds.trail_seconds ?? 60);

  const sp = safePct();
  const state = {
    now: t,
    telemetry: tel,
    settings,
    view: {
      mode,
      orientation: ds.orientation,
      bearingDeg: pose.bearing,
      headingDeg: m.headingKnown ? m.heading : null,
      vehicleHeadingDeg: m.headingKnown ? rot.vehicle : null,
      compassHeadingDeg: compass.known ? compassDeg : null,
      headingSource: tel.heading?.source || "none",
      speedMps: m.speed,
      zoom,
      hasFix,
      connected,
      internet: tel.system?.internet ?? null,
      recording: !!tel.camera?.recording,
      attribution: DIRECT_TILES && mode === "map", // подпись Esri внизу по центру — радио над ней
    },
    canvas: {
      w: W,
      h: H,
      safe: { x: W * sp, y: H * sp, w: W * (1 - 2 * sp), h: H * (1 - 2 * sp) },
      pxPerMm: W / (ds.screen?.w_mm || 92),
    },
  };

  lastState = state;

  // ── карта ──
  const covered = camera.coversMap(t);
  if (m.hasPos) {
    // тайлы заказываем и под камерой (реже) — при возврате к карте они уже готовы
    const level = tileLevelFor(zoom, maxZoom());
    if (t - lastPlan > (covered ? 1.0 : 0.15) || level !== lastPlanLevel) {
      store.want(
        planRequests({
          view,
          layers,
          maxZoom: maxZoom(),
          targetZoom: zoomAnim.target,
          headingDeg: m.heading,
          speedMps: m.speed ?? 0,
          urlFor: tileUrlFor,
        }),
      );
      lastPlan = t;
      lastPlanLevel = level;
    }
  }
  if (!covered && m.hasPos) {
    if (needsMapRedraw(view, t, hasFix)) {
      const t0 = DEBUG ? performance.now() : 0;
      renderer.draw(mapCtx, view, layers, maxZoom(), PR);
      mapDirty = false;
      mapCtx.setTransform(PR, 0, 0, PR, 0, 0);
      if (ds.show_trail && trail.pts.length) {
        callUi("drawTrail", mapCtx, trail.project(view, t, { mx: m.mx, my: m.my }), state);
      }
      callUi("drawVehicle", mapCtx, pose.ax, pose.ay, pose.vehicleRot, state);
      lastDraw = { t, cx: view.cx, cy: view.cy, zoom, bearing: view.bearing, ax: view.ax, ay: view.ay };
      drawnFrames++;
      if (DEBUG) mapMsEma += (performance.now() - t0 - mapMsEma) * 0.05;
    }
    hadPos = true;
  } else if (!m.hasPos && (mapDirty || hadPos)) {
    mapCtx.setTransform(PR, 0, 0, PR, 0, 0);
    mapCtx.fillStyle = BG_COLOR;
    mapCtx.fillRect(0, 0, W, H);
    mapDirty = false;
    hadPos = false;
  } else if (covered) {
    mapDirty = true; // после возврата с камеры — перерисовать сразу
  }

  // ── HUD ──
  hudCtx.setTransform(PR, 0, 0, PR, 0, 0);
  hudCtx.clearRect(0, 0, W, H);
  const th = DEBUG ? performance.now() : 0;
  callUi("drawHud", hudCtx, state);
  if (DIRECT_TILES && mode === "map") drawAttribution(hudCtx, state);
  if (DEBUG) {
    hudMsEma += (performance.now() - th - hudMsEma) * 0.05;
    const s = store.stats;
    debugHud.drawStats(hudCtx, [
      `fps ${fpsEma.toFixed(0)}  map ${mapMsEma.toFixed(1)}ms  hud ${hudMsEma.toFixed(1)}ms  draws ${drawnFrames}  ui ${uiName}`,
      `zoom ${zoom.toFixed(2)} → ${zoomAnim.target ?? "-"}  lvl ${tileLevelFor(zoom, maxZoom())}  brg ${pose.bearing.toFixed(1)}  veh ${rot.vehicle.toFixed(1)}  ${rig.tracking ? "trk" : "hold"}`,
      `tiles cache ${store.cache.size}  load ${store.inflight.size}  q ${store.queue.length}  fail ${store.failures.size}`,
      `drawn ${renderer.lastDrawn.tiles}/${renderer.lastDrawn.fallbacks}/${renderer.lastDrawn.missing}  ok ${s.loaded} err ${s.failed} abort ${s.aborted} evict ${s.evicted}`,
      `pos ${m.lat?.toFixed(5) ?? "-"}, ${m.lon?.toFixed(5) ?? "-"}  v ${(m.speed ?? 0).toFixed(1)}  trail ${trail.pts.length}`,
      `ws ${connected ? "ok" : "LOST"}  mode ${mode}  cam ${camera.ok ? "ok" : "-"}`,
    ]);
  }
}

// ждём HUD агента C (шрифты/спрайты) не дольше 3 с, затем стартуем; догрузится — переключимся
await Promise.race([loadExternalUi(), new Promise((r) => setTimeout(r, 3000))]);
requestAnimationFrame(frame);

// для отладки из консоли
window.fnav = { store, motion, trail, link, camera, rig, get demo() { return link && link.sim ? link : null; }, get state() { return lastState; }, get settings() { return settings; }, get telemetry() { return telemetry; } };
