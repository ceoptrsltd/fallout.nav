// fallout.nav — экран /display: собственный рендер карты Esri на Canvas + видео + HUD.
// Контракт: docs/contracts/display.md. Рисование HUD — web/display/ui/index.js (агент C),
// запасной вариант — ./map/debug-hud.js.
//
// Параметры URL: ?pal=1 (устройство, 720×576, scaleX 0.9375), ?preview=1 (iframe веб-интерфейса),
// ?debug=1 (статистика, свой холст поверх), ?fps=30 (ограничить частоту кадров), ?dpr=2 (чёткость; без него в браузере — по экрану),
// ?ui=debug (принудительно отладочный HUD), ?demo=1 (демо в браузере без fnav-core, docs/contracts/demo.md;
// ?camera=<url> — картинка «камеры» в демо), ?verify=1 (с ?debug=1: HUD и ветер рисуются ещё и напрямую на
// скрытых холстах, пиксели сравниваются с экранными — проверка перерисовки по изменению, map/retained.js).
// Удалённый доступ (страница не с устройства, а через сервер, docs/contracts/remote.md): адреса — от корня
// приложения, тайлы Esri — напрямую с arcgisonline (не через мобильный интернет машины), MJPEG — ~5 к/с.

import { DEFAULT_SETTINGS, deepMerge } from "./map/defaults.js";
import { TileStore, browserTileLoader } from "./map/tiles.js";
import { MapRenderer, BG_COLOR } from "./map/renderer.js";
import { activeLayers, layerLevel, planRequests } from "./map/layers.js";
import { CompassFilter, MotionModel, OrientationBlend, RotationRig, ZoomAnimator, hudSpeed, vehicleAnchor, vehicleCenter } from "./map/motion.js";
import { BURNIN_AMP, burnInOffset, insetRect } from "./map/burnin.js";
import { Trail } from "./map/trail.js";
import { CameraLayer } from "./map/camera.js";
import { RearSignal, rearMirror, viewCamera } from "./map/rearview.js";
import { RetainedLayer, bumpEpoch } from "./map/retained.js";
import { armedStandby, standbyDelayMs } from "./map/standby.js";
import { Link, wsUrl } from "./map/link.js";
import { REMOTE, appUrl } from "./map/root.js";
import { ESRI_ATTRIBUTION, esriTileUrl } from "./map/esri.js";
import { GamesMenu } from "./ui/games-menu.js";
import { clamp, makeView, metersPerPixel } from "./map/mercator.js";
import * as debugHud from "./map/debug-hud.js";

const W = 768;
const H = 576;
const params = new URLSearchParams(location.search);
const DEMO = params.get("demo") === "1";
const PAL = params.get("pal") === "1" && !DEMO; // демо — всегда без PAL-сжатия и без зеркала
const PREVIEW = params.get("preview") === "1";
const DEBUG = params.get("debug") === "1";
const VERIFY = DEBUG && params.get("verify") === "1";
const FPS_CAP = Number(params.get("fps")) || 0;
const DPR_PARAM = Number(params.get("dpr")) || 0;
// пикселей холста на логический px. Устройство (PAL) — 1: холст 768×576 и так сжимается до 720×480.
// В браузере — сколько настоящих пикселей экрана приходится на px холста (масштаб окна × devicePixelRatio):
// карта рисуется в разрешении экрана, а не растягивается, и берёт тайлы подробнее (map/layers.js layerLevel).
let PR = clamp(DPR_PARAM || 1, 1, 3);
const TILE_CAPACITY = 300;
const FORCE_DEBUG_UI = params.get("ui") === "debug";
const DIRECT_TILES = DEMO || REMOTE; // Esri напрямую: на карте нужна подпись источников

const nowS = () => performance.now() / 1000;

// ───────────── разметка ─────────────
const root = document.getElementById("root");
const mapCanvas = document.getElementById("map");
const windCanvas = document.getElementById("wind");
const hudCanvas = document.getElementById("hud");
// ?debug=1: статистика на своём холсте — иначе она меняется каждый кадр и HUD никогда не пропускал бы кадры
const debugCanvas = DEBUG ? root.appendChild(Object.assign(document.createElement("canvas"), { id: "debug" })) : null;
document.body.classList.toggle("pal", PAL);
document.body.classList.toggle("preview", PREVIEW);
// HUD и ветер перерисовываются, только когда изменилось нарисованное (map/retained.js); создаются ниже
let hudLayer = null;
let windLayer = null;
function sizeCanvases() {
  for (const c of [mapCanvas, windCanvas, hudCanvas, debugCanvas]) {
    if (!c) continue;
    c.width = Math.round(W * PR);
    c.height = Math.round(H * PR);
  }
  hudLayer?.invalidate();
  windLayer?.invalidate();
}
sizeCanvases();
const mapCtx = mapCanvas.getContext("2d", { alpha: false });
// меню игр (геймпад) — только на самом устройстве
const gamesMenu = !DEMO && !REMOTE && !PREVIEW
  ? new GamesMenu(root, appUrl)
  : null;
// ?verify=1: все холсты сравнения — в памяти (CPU), иначе GPU и CPU сглаживают края по-разному
const CTX_OPTS = VERIFY ? { willReadFrequently: true } : undefined;
const hudCtx = hudCanvas.getContext("2d", CTX_OPTS);
const windCtx = windCanvas.getContext("2d", CTX_OPTS);
const debugCtx = debugCanvas ? debugCanvas.getContext("2d") : null;
hudLayer = new RetainedLayer(hudCtx);
windLayer = new RetainedLayer(windCtx);
const FULL = { x: 0, y: 0, w: W, h: H };

let mirrorX = false;   // settings.display.mirror_x (set in applySettings)
let resized = false;   // холсты пересозданы (PR сменился) — карту перерисовать
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
  if (!DPR_PARAM) {
    // окно предпросмотра в веб-интерфейсе ещё и уменьшено (transform) — его масштаб в devicePixelRatio не виден,
    // но и не нужен: лишние пиксели только сглаживаются. Ступенями по 0.25, чтобы не пересоздавать холсты на каждый px.
    const pr = clamp(Math.ceil(s * (devicePixelRatio || 1) * 4) / 4, 1, 3);
    if (pr !== PR) {
      PR = pr;
      sizeCanvases();
      resized = true;
    }
  }
}
layout();
addEventListener("resize", layout);

// ───────────── состояние ─────────────
let settings = DEFAULT_SETTINGS;
let settingsFromWs = false;
let layers = activeLayers(settings);
let telemetry = null;
let weather = null; // {"type":"weather"} от fnav-core (ui/weather.js)
let lastInternet = null;
let mapDirty = true;
// экран «на охране» (map/standby.js, ui/standby.js): вместо кадров по rAF — таймер раз в секунду
let standby = false;
let rafId = 0;
let standbyTimer = null;
const HUD_REFRESH_S = 2; // страховка: HUD проигрывается не реже (map/retained.js)

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
  capacity: TILE_CAPACITY,
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
    : {
        src: appUrl("api/camera/mjpeg") + (REMOTE ? "?fps=5" : ""),
        rearSrc: appUrl("api/camera/rear/mjpeg") + (REMOTE ? "?fps=5" : ""),
      },
);
// задний вид (display.camera = "rear"): есть ли картинка — для слоя видео и надписей HUD (map/rearview.js)
const rearSignal = new RearSignal();
let rearShown = false;
// передняя в режиме камеры — то же по telemetry.camera: кадров нет — не стоп-кадр, а NO CAMERA (демо — без статуса)
const frontSignal = new RearSignal();
let frontShown = false;

function maxZoom() {
  return settings.tiles?.max_zoom ?? 19;
}

function applySettings(s) {
  settings = deepMerge(DEFAULT_SETTINGS, s || {});
  layers = activeLayers(settings);
  const ds = settings.display;
  mirrorX = !!ds.mirror_x;
  camera.setSafeArea(ds.safe_area_pct ?? 5); // задний вид — в видимую область кинескопа
  layout();
  orient.set(ds.orientation);
  rig.setOrientation(ds.orientation);
  compass.configure(ds.compass_smoothing_s);
  rig.configure({
    vehicleLagS: ds.vehicle_rotation_lag_s,
    mapLagS: ds.map_rotation_lag_s,
    deadbandDeg: ds.map_rotation_deadband_deg,
  });
  // счисление пути в тоннеле — по тем же порогам, что режим экрана у fnav-core (navlogic.py)
  motion.configure({ drMinMps: settings.nav.map_above_mps, drWindowS: settings.nav.dead_reckoning_s });
  if (!settings.display.show_trail) trail.clear();
  if (zoomAnim.target !== null) zoomAnim.setTarget(clamp(zoomAnim.target, 1, maxZoom()));
  mapDirty = true;
  checkStandby();
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
    camera.setCamera(viewCamera(msg), rearMirror(msg)); // до setVisible: подключиться сразу к нужной камере
    camera.setVisible(d.mode === "camera", t);
    const inet = msg.system ? msg.system.internet : null;
    if (inet === true && lastInternet === false) store.resetFailures(); // сеть вернулась — перезапросить
    lastInternet = inet;
    checkStandby();
  } else if (msg.type === "weather") {
    weather = msg;
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
let ui = { drawHud: debugHud.drawHud, drawVehicle: debugHud.drawVehicle, drawTrail: debugHud.drawTrail, drawWind: null, drawStandby: null };
let uiName = "debug";
const uiErrors = { drawHud: 0, drawVehicle: 0, drawTrail: 0, drawWind: 0, drawStandby: 0 };

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
      // ветер на своём холсте (drawHud(..., {wind: false})); нет функции — ветер рисует сам drawHud
      drawWind: typeof mod.drawWind === "function" ? mod.drawWind : null,
      drawStandby: typeof mod.drawStandby === "function" ? mod.drawStandby : null,
    };
    uiName = "ui";
    mapDirty = true;
    bumpEpoch(); // шрифты HUD загружены — перерисовать текст
  } catch (e) {
    console.warn("fnav: web/display/ui unavailable, using debug HUD", e);
  }
}

/**
 * Вызов функции C с защитой: исключение не роняет кадр; при систематических — откат на отладочную
 * (нет отладочной — функция отключается). Возвращает результат функции (или undefined при ошибке).
 */
function callUi(name, ctx, ...args) {
  ctx.save();
  try {
    return ui[name](ctx, ...args);
  } catch (e) {
    if (uiErrors[name]++ === 0) console.error(`fnav: ui.${name} failed`, e);
    if (typeof ctx.reset === "function") ctx.reset();
    mapDirty = true; // reset() мог стереть карту — перерисовать в следующем кадре
    ctx.setTransform(PR, 0, 0, PR, 0, 0);
    if (uiErrors[name] > 30 && ui[name] !== debugHud[name]) {
      console.error(`fnav: ui.${name} keeps failing, switching to debug HUD`);
      ui[name] = debugHud[name] ?? null;
    }
    return undefined;
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
let hudShift = { dx: 0, dy: 0 }; // сдвиг HUD от выжигания в этом кадре (map/burnin.js)

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

// без фикса карта перерисовывается только ради мигающей иконки машины — ей хватает 12 к/с
const BLINK_REDRAW_S = 1 / 12;

function needsMapRedraw(view, t, blink) {
  const d = lastDraw;
  if (mapDirty || t - d.t > 0.5 || (blink && t - d.t >= BLINK_REDRAW_S - 0.002)) return true;
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
  const t = tms / 1000;
  if (FPS_CAP && lastFrame !== null && t - lastFrame < 1 / FPS_CAP - 0.004) return;
  if (resized) {
    resized = false;
    mapDirty = true;
  }
  const dt = lastFrame === null ? 0 : Math.min(0.25, t - lastFrame);
  if (dt > 0) fpsEma = fpsEma ? fpsEma + (1 / dt - fpsEma) * 0.05 : 1 / dt;
  lastFrame = t;

  const tel = telemetry || STUB_TELEMETRY;
  // геймпад: только пока fnav-core его видит (display.gamepad; нет поля — старое ядро, опрашиваем как раньше)
  if (gamesMenu) gamesMenu.poll(t, tel.gps ? tel.gps.speed_mps : null, telemetry !== null && tel.display?.gamepad !== false);
  const m = motion.update(t);
  lastPos = m;
  const f = orient.update(dt);
  const zoom = zoomAnim.update(dt) ?? clamp(tel.display.target_zoom ?? 17, 1, maxZoom());
  const ds = settings.display;
  const speed = m.speed ?? m.drSpeed ?? 0; // скорость движения (в тоннеле — счисления; спидометр её не показывает)
  const rot = rig.update(dt, m.headingTarget, speed);
  const compassDeg = compass.update(t);
  // от выжигания кинескопа HUD и машина раз в несколько минут сдвигаются на 2–3 px (map/burnin.js)
  const burn = burnInOffset(Date.now());
  const anchor = vehicleAnchor(f, W, H, ds.vehicle_anchor_y ?? 0.67);
  const pose = { ax: anchor.ax + burn.dx, ay: anchor.ay + burn.dy, bearing: rot.bearing, vehicleRot: rot.vehicleRot };
  // GPS-антенна — не центр машины: в точке машины на экране — её центр, антенна — впереди и правее
  // (settings.vehicle, display.md «Антенна GPS»); курс — тот, с которым нарисована иконка
  const center = vehicleCenter(m.mx, m.my, rig.vehicle, settings.vehicle);
  const view = makeView({ cx: center.x, cy: center.y, zoom, bearing: pose.bearing, ax: pose.ax, ay: pose.ay, w: W, h: H });
  const hasFix = !!(tel.gps && tel.gps.fix && tel.gps.fix !== "none" && tel.gps.lat != null);
  const dr = !hasFix && m.dr; // тоннель: фикса нет, машина едет по счислению
  const connected = link.connected(3);
  const mode = tel.display.mode === "camera" ? "camera" : "map";
  // задний вид: есть ли картинка — без сигнала слой прячет стоп-кадр, HUD пишет REAR и NO SIGNAL
  const rear = mode === "camera" && camera.which === "rear";
  let rearSig = null;
  if (rear) {
    rearSig = rearSignal.update(tel.camera_rear, camera, t);
    if (rearSignal.recovered) camera.reconnect(); // старый поток мог оборваться молча (последний кадр висит)
    camera.setHold(rearSig !== "ok");
  } else if (rearShown) {
    rearSignal.reset();
    camera.setHold(false);
  }
  rearShown = rear;
  const front = mode === "camera" && camera.which === "front" && !DEMO && !!tel.camera;
  if (front) {
    const sig = frontSignal.update(tel.camera, camera, t);
    if (frontSignal.recovered) camera.reconnect();
    camera.setNoSignal(sig === "no_signal");
  } else if (frontShown) {
    frontSignal.reset();
    camera.setNoSignal(false);
  }
  frontShown = front;

  if (ds.show_trail && m.hasPos && (hasFix || dr)) {
    // след — за центром машины; шаг ~2.5 px экрана, но так, чтобы точек хватило на trail_seconds
    trail.add(center.x, center.y, t, { mpp: metersPerPixel(m.lat, zoom), speed, keepS: ds.trail_seconds ?? 60 });
  }
  trail.prune(t, ds.trail_seconds ?? 60);

  const sp = safePct();
  const safe = { x: W * sp, y: H * sp, w: W * (1 - 2 * sp), h: H * (1 - 2 * sp) };
  const state = {
    now: t,
    telemetry: tel,
    weather,
    settings,
    view: {
      mode,
      camera: rear ? "rear" : "front", // какая камера в режиме camera (ui/rear.js — метка REAR)
      rearSignal: rearSig, // задний вид: "ok" | "wait" | "connecting" | "no_signal"; иначе null
      orientation: ds.orientation,
      bearingDeg: pose.bearing,
      headingDeg: m.headingKnown ? m.heading : null,
      vehicleHeadingDeg: m.headingKnown ? rot.vehicle : null,
      compassHeadingDeg: compass.known ? compassDeg : null,
      headingSource: tel.heading?.source || "none",
      speedMps: hudSpeed(m.speed, connected), // нет связи — "--", а не последняя скорость
      zoom,
      hasFix,
      deadReckoning: dr, // тоннель: иконка тусклая, в строке GPS — DR (ui/vehicle.js, ui/status.js)
      connected,
      internet: tel.system?.internet ?? null,
      recording: !!tel.camera?.recording,
      wallMs: Date.now(), // часы на экране (ui/clock.js)
      vehicleX: pose.ax, // где машина на экране (ветер вокруг неё, ui/weather.js)
      vehicleY: pose.ay,
      attribution: DIRECT_TILES && mode === "map", // подпись Esri внизу по центру — радио над ней
    },
    canvas: {
      w: W,
      h: H,
      // HUD сдвигается на ±BURNIN_AMP — его зона безопасности на столько же меньше: плашки из настоящей не выходят
      safe: insetRect(safe, BURNIN_AMP),
      pxPerMm: W / (ds.screen?.w_mm || 92),
    },
  };
  hudShift = burn;

  lastState = state;

  // ── карта ──
  const covered = camera.coversMap(t);
  if (m.hasPos) {
    // тайлы заказываем и под камерой (реже) — при возврате к карте они уже готовы
    const detail = Math.log2(PR);
    const level = layers.length ? layerLevel(layers[0], zoom, maxZoom(), detail) : 0;
    if (t - lastPlan > (covered ? 1.0 : 0.15) || level !== lastPlanLevel) {
      store.capacity = Math.round(TILE_CAPACITY * Math.max(1, PR)); // экран чётче — тайлов в кадре больше
      store.want(
        planRequests({
          view,
          layers,
          maxZoom: maxZoom(),
          targetZoom: zoomAnim.target,
          headingDeg: m.heading,
          speedMps: speed,
          urlFor: tileUrlFor,
          detail,
          failed: (key) => store.isFailed(key),
          maxRequests: Math.round(160 * Math.max(1, PR)),
        }),
      );
      lastPlan = t;
      lastPlanLevel = level;
    }
  }
  if (!covered && m.hasPos) {
    if (needsMapRedraw(view, t, !hasFix && !dr)) {
      const t0 = DEBUG ? performance.now() : 0;
      renderer.draw(mapCtx, view, layers, maxZoom(), PR);
      mapDirty = false;
      mapCtx.setTransform(PR, 0, 0, PR, 0, 0);
      if (ds.show_trail && trail.pts.length) {
        callUi("drawTrail", mapCtx, trail.project(view, t, { mx: center.x, my: center.y }), state);
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

  // ── ветер (свой холст под HUD) и HUD: проигрываются на холст, только когда нарисованное изменилось ──
  const th = DEBUG ? performance.now() : 0;
  const windOwn = typeof ui.drawWind === "function";
  const wrec = windLayer.begin();
  let windBox = null;
  if (windOwn) {
    wrec.setTransform(PR, 0, 0, PR, 0, 0);
    windBox = callUi("drawWind", wrec, state) ?? null;
  }
  windLayer.end(t, windBox, PR);
  const hrec = hudLayer.begin();
  hrec.setTransform(PR, 0, 0, PR, burn.dx * PR, burn.dy * PR);
  callUi("drawHud", hrec, state, { wind: !windOwn });
  if (DIRECT_TILES && mode === "map") drawAttribution(hrec, state);
  hudLayer.end(t, FULL, PR);
  if (verify) verifyFrame(state, windOwn, mode);
  if (DEBUG) {
    hudMsEma += (performance.now() - th - hudMsEma) * 0.05;
    const s = store.stats;
    drawDebug([
      `fps ${fpsEma.toFixed(0)}  map ${mapMsEma.toFixed(1)}ms  hud ${hudMsEma.toFixed(1)}ms  draws ${drawnFrames}  ui ${uiName}`,
      `zoom ${zoom.toFixed(2)} → ${zoomAnim.target ?? "-"}  lvl ${layers.length ? layerLevel(layers[0], zoom, maxZoom(), Math.log2(PR)) : "-"}  pr ${PR}  brg ${pose.bearing.toFixed(1)}  veh ${rot.vehicle.toFixed(1)}  ${rig.tracking ? "trk" : "hold"}`,
      `tiles cache ${store.cache.size}  load ${store.inflight.size}  q ${store.queue.length}  fail ${store.failures.size}`,
      `drawn ${renderer.lastDrawn.tiles}/${renderer.lastDrawn.fallbacks}/${renderer.lastDrawn.missing}  ok ${s.loaded} err ${s.failed} abort ${s.aborted} evict ${s.evicted}`,
      `pos ${m.lat?.toFixed(5) ?? "-"}, ${m.lon?.toFixed(5) ?? "-"}  v ${speed.toFixed(1)}${dr ? " DR" : ""}  trail ${trail.pts.length}  burn ${burn.dx},${burn.dy}`,
      `ws ${connected ? "ok" : "LOST"}${link.drops ? ` (drop ${link.drops})` : ""}  mode ${mode}  cam ${camera.which} ${camera.ok ? "ok" : "-"}${rearSig ? " " + rearSig : ""}  pad ${gamesMenu?.polling ? "poll" : "-"}`,
      layerStats(),
    ]);
  }
}

function layerStats() {
  const h = hudLayer.stats, w = windLayer.stats;
  const v = verify ? `  verify ${verify.checked} bad ${verify.bad} max ${verify.maxDiff}` : "";
  return `hud drawn ${h.drawn} skip ${h.skipped}  wind drawn ${w.drawn} skip ${w.skipped}${standby ? "  STANDBY" : ""}${v}`;
}

// ?verify=1: те же кадры напрямую (без записи и пропусков) на скрытых холстах — с первого кадра, чтобы
// анимации (бегущая строка, вспышки) шли так же; раз в 30 кадров пиксели сравниваются с экранными.
const verify = VERIFY ? { hud: null, wind: null, n: 0, checked: 0, bad: 0, maxDiff: 0 } : null;
function verifyFrame(state, windOwn, mode) {
  const mk = () => {
    const c = document.createElement("canvas");
    c.width = hudCanvas.width;
    c.height = hudCanvas.height;
    return c.getContext("2d", CTX_OPTS);
  };
  verify.hud ??= mk();
  verify.wind ??= mk();
  const vh = verify.hud, vw = verify.wind;
  vh.setTransform(PR, 0, 0, PR, hudShift.dx * PR, hudShift.dy * PR);
  vh.clearRect(0, 0, W, H);
  callUi("drawHud", vh, state, { wind: !windOwn });
  if (DIRECT_TILES && mode === "map") drawAttribution(vh, state);
  vw.setTransform(PR, 0, 0, PR, 0, 0);
  vw.clearRect(0, 0, W, H);
  if (windOwn) callUi("drawWind", vw, state);
  if (++verify.n % 30) return;
  for (const [live, direct] of [[hudCtx, vh], [windCtx, vw]]) {
    const a = live.getImageData(0, 0, live.canvas.width, live.canvas.height).data;
    const b = direct.getImageData(0, 0, direct.canvas.width, direct.canvas.height).data;
    let mx = a.length === b.length ? 0 : 255;
    for (let i = 0; i < a.length && mx < 255; i++) mx = Math.max(mx, Math.abs(a[i] - b[i]));
    verify.checked++;
    if (mx) verify.bad++;
    verify.maxDiff = Math.max(verify.maxDiff, mx);
  }
}

/** ?debug=1: статистика на своём холсте поверх всего. */
function drawDebug(lines) {
  debugCtx.setTransform(PR, 0, 0, PR, 0, 0);
  debugCtx.clearRect(0, 0, W, H);
  debugHud.drawStats(debugCtx, lines);
}

// ───────────── экран «на охране» ─────────────
// Пока охрана в состоянии armed (map/standby.js): ни карты, ни тайлов, ни ветра, ни камеры, ни геймпада;
// вместо rAF — таймер раз в секунду, HUD-холст проигрывается, только когда сменилась минута.

function loop(tms) {
  rafId = 0;
  if (armedStandby(telemetry, settings)) {
    standbyFrame();
    return;
  }
  rafId = requestAnimationFrame(loop);
  frame(tms);
}

function standbyFrame() {
  const t = nowS();
  if (!standby) {
    standby = true;
    camera.suspend(true);
    if (gamesMenu) gamesMenu.stopPolling();
    windLayer.begin();
    windLayer.end(t, null, PR); // стереть струйки
    hudLayer.refreshS = 60;
  }
  const state = {
    now: t,
    telemetry: telemetry || STUB_TELEMETRY,
    weather,
    settings,
    view: { mode: "map", wallMs: Date.now(), connected: link.connected(3) },
    canvas: { w: W, h: H, safe: { x: 0, y: 0, w: W, h: H }, pxPerMm: W / (settings.display.screen?.w_mm || 92) },
  };
  lastState = state;
  const rec = hudLayer.begin();
  rec.setTransform(PR, 0, 0, PR, 0, 0);
  if (ui.drawStandby) {
    callUi("drawStandby", rec, state);
  } else {
    rec.fillStyle = "#000";
    rec.fillRect(0, 0, W, H);
  }
  hudLayer.end(t, FULL, PR);
  if (DEBUG) drawDebug([`standby (alarm armed)  ui ${uiName}`, layerStats()]);
  clearTimeout(standbyTimer);
  standbyTimer = setTimeout(() => {
    standbyTimer = null;
    if (!rafId) rafId = requestAnimationFrame(loop);
  }, standbyDelayMs(Date.now()));
}

/** Телеметрия/настройки: охрану сняли (или сработала) — сразу обычный экран. */
function checkStandby() {
  if (standby && !armedStandby(telemetry, settings)) wake();
}

function wake() {
  standby = false;
  clearTimeout(standbyTimer);
  standbyTimer = null;
  camera.suspend(false);
  hudLayer.refreshS = HUD_REFRESH_S;
  mapDirty = true;
  lastFrame = null; // время сна не считать кадром
  if (!rafId) rafId = requestAnimationFrame(loop);
}

// ждём HUD агента C (шрифты/спрайты) не дольше 3 с, затем стартуем; догрузится — переключимся
await Promise.race([loadExternalUi(), new Promise((r) => setTimeout(r, 3000))]);
rafId = requestAnimationFrame(loop);

// для отладки из консоли
window.fnav = { store, motion, trail, link, camera, rig, gamesMenu, verify, layers: { hud: hudLayer, wind: windLayer }, get standby() { return standby; }, get demo() { return link && link.sim ? link : null; }, get state() { return lastState; }, get settings() { return settings; }, get telemetry() { return telemetry; } };
