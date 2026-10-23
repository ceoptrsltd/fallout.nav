// Галерея HUD: все варианты компаса, машина, спидометр, индикаторы — в физическом размере, с анимацией.
// Фон — процедурная «спутниковая» текстура (без сети), тёмная подложка или «камера».

import { compassStyles, drawHud, drawTrail, drawVehicle, loadUi } from "./index.js";
import { canvasScale, getPxPerMm, onPxPerMmChange, renderCalibrator } from "./physical.js";
import { drawVehicleScaled } from "./vehicle.js";

const W = 768;
const H = 576;
const SAFE = { x: W * 0.05, y: H * 0.05, w: W * 0.9, h: H * 0.9 };
const PX_PER_M = 2.2; // масштаб «карты» галереи
const DEG = Math.PI / 180;

// ── процедурные фоны ─────────────────────────────────────────────────────

function rng(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Периодический value-noise fbm, size×size, значения 0..1. */
function fbm(size, seed, octaves = 5, base = 4) {
  const r = rng(seed);
  const out = new Float32Array(size * size);
  let amp = 1;
  let total = 0;
  for (let o = 0; o < octaves; o++) {
    const n = base << o;
    const lat = new Float32Array(n * n);
    for (let i = 0; i < lat.length; i++) lat[i] = r();
    for (let y = 0; y < size; y++) {
      const fy = (y / size) * n;
      const y0 = Math.floor(fy);
      const ty = fy - y0;
      const sy = ty * ty * (3 - 2 * ty);
      const ya = (y0 % n) * n;
      const yb = ((y0 + 1) % n) * n;
      for (let x = 0; x < size; x++) {
        const fx = (x / size) * n;
        const x0 = Math.floor(fx);
        const tx = fx - x0;
        const sx = tx * tx * (3 - 2 * tx);
        const xa = x0 % n;
        const xb = (x0 + 1) % n;
        const a = lat[ya + xa] + (lat[ya + xb] - lat[ya + xa]) * sx;
        const b = lat[yb + xa] + (lat[yb + xb] - lat[yb + xa]) * sx;
        out[y * size + x] += (a + (b - a) * sy) * amp;
      }
    }
    total += amp;
    amp *= 0.5;
  }
  for (let i = 0; i < out.length; i++) out[i] /= total;
  return out;
}

function makeSatTexture(T = 1024) {
  const c = document.createElement("canvas");
  c.width = c.height = T;
  const g = c.getContext("2d");
  const n = fbm(T, 7, 6, 4);
  const m = fbm(T, 99, 3, 8);
  const img = g.createImageData(T, T);
  const pal = [
    [42, 58, 34], [58, 78, 42], [86, 96, 58], [122, 112, 78], [150, 138, 104], [104, 100, 72],
  ];
  for (let i = 0; i < T * T; i++) {
    const v = Math.min(0.999, Math.max(0, (n[i] - 0.25) * 1.9));
    const f = v * (pal.length - 1);
    const k = Math.floor(f);
    const t = f - k;
    const a = pal[k];
    const b = pal[Math.min(pal.length - 1, k + 1)];
    const grain = (m[i] - 0.5) * 50 + (((i * 2654435761) >>> 24) / 255 - 0.5) * 22;
    img.data[i * 4] = a[0] + (b[0] - a[0]) * t + grain;
    img.data[i * 4 + 1] = a[1] + (b[1] - a[1]) * t + grain;
    img.data[i * 4 + 2] = a[2] + (b[2] - a[2]) * t + grain * 0.8;
    img.data[i * 4 + 3] = 255;
  }
  g.putImageData(img, 0, 0);
  const r = rng(3);
  // поля с бороздами
  for (let i = 0; i < 9; i++) {
    const x = 80 + r() * (T - 360);
    const y = 80 + r() * (T - 360);
    const w = 120 + r() * 160;
    const h = 90 + r() * 140;
    g.save();
    g.translate(x + w / 2, y + h / 2);
    g.rotate((r() - 0.5) * 0.5);
    g.fillStyle = ["rgba(170,160,110,.55)", "rgba(70,95,45,.55)", "rgba(120,100,70,.5)"][i % 3];
    g.fillRect(-w / 2, -h / 2, w, h);
    g.strokeStyle = "rgba(0,0,0,.12)";
    g.lineWidth = 2;
    for (let k = -w / 2; k < w / 2; k += 6) {
      g.beginPath();
      g.moveTo(k, -h / 2);
      g.lineTo(k, h / 2);
      g.stroke();
    }
    g.restore();
  }
  // деревья
  for (let i = 0; i < 1400; i++) {
    const x = r() * T;
    const y = r() * T;
    const rr = 2 + r() * 5;
    g.fillStyle = "rgba(0,0,0,.35)";
    g.beginPath();
    g.arc(x + 2, y + 2, rr, 0, 2 * Math.PI);
    g.fill();
    g.fillStyle = `rgba(${30 + r() * 30},${55 + r() * 30},${25 + r() * 15},.95)`;
    g.beginPath();
    g.arc(x, y, rr, 0, 2 * Math.PI);
    g.fill();
  }
  // дороги (периодические: горизонталь, вертикаль, синусоида)
  const road = (path, w) => {
    g.lineCap = "butt";
    g.strokeStyle = "rgba(30,30,28,.9)";
    g.lineWidth = w + 4;
    g.stroke(path);
    g.strokeStyle = "#77766f";
    g.lineWidth = w;
    g.stroke(path);
    g.setLineDash([12, 12]);
    g.strokeStyle = "#e9e6d2";
    g.lineWidth = 2;
    g.stroke(path);
    g.setLineDash([]);
  };
  const p1 = new Path2D();
  p1.moveTo(0, 300);
  p1.lineTo(T, 300);
  road(p1, 16);
  const p2 = new Path2D();
  p2.moveTo(620, 0);
  p2.lineTo(620, T);
  road(p2, 14);
  const p3 = new Path2D();
  for (let x = 0; x <= T; x += 8) {
    const y = 760 + 70 * Math.sin((x / T) * 4 * Math.PI);
    if (x === 0) p3.moveTo(x, y);
    else p3.lineTo(x, y);
  }
  road(p3, 12);
  // постройки у перекрёстка
  for (let i = 0; i < 26; i++) {
    const x = 520 + r() * 220;
    const y = 200 + r() * 200;
    if (Math.abs(y - 300) < 22 || Math.abs(x - 620) < 22) continue;
    const w = 14 + r() * 26;
    const h = 12 + r() * 22;
    g.fillStyle = "rgba(0,0,0,.45)";
    g.fillRect(x + 4, y + 4, w, h);
    g.fillStyle = ["#b9b3a2", "#8f2f24", "#6a6f72", "#d4cfc0"][i % 4];
    g.fillRect(x, y, w, h);
  }
  return c;
}

function makeDarkTexture(T = 512) {
  const c = document.createElement("canvas");
  c.width = c.height = T;
  const g = c.getContext("2d");
  const n = fbm(T, 5, 4, 4);
  const img = g.createImageData(T, T);
  for (let i = 0; i < T * T; i++) {
    const v = 14 + n[i] * 22;
    img.data[i * 4] = v;
    img.data[i * 4 + 1] = v + 2;
    img.data[i * 4 + 2] = v - 2;
    img.data[i * 4 + 3] = 255;
  }
  g.putImageData(img, 0, 0);
  g.strokeStyle = "#3a3e33";
  g.lineWidth = 6;
  for (const k of [128, 384]) {
    g.beginPath();
    g.moveTo(0, k);
    g.lineTo(T, k);
    g.moveTo(k + 60, 0);
    g.lineTo(k + 60, T);
    g.stroke();
  }
  return c;
}

function drawCamera(g, dist) {
  const sky = g.createLinearGradient(0, 0, 0, 250);
  sky.addColorStop(0, "#9fb1c0");
  sky.addColorStop(1, "#d9d3bd");
  g.fillStyle = sky;
  g.fillRect(0, 0, W, 250);
  const gr = g.createLinearGradient(0, 250, 0, H);
  gr.addColorStop(0, "#8a7a55");
  gr.addColorStop(1, "#4d4130");
  g.fillStyle = gr;
  g.fillRect(0, 250, W, H - 250);
  g.fillStyle = "#3b3b38";
  g.beginPath();
  g.moveTo(360, 250);
  g.lineTo(408, 250);
  g.lineTo(700, H);
  g.lineTo(68, H);
  g.closePath();
  g.fill();
  // разметка «едет» на зрителя
  g.fillStyle = "#e8e4d0";
  for (let k = 0; k < 8; k++) {
    const z = ((k + (dist * 0.08) % 1) / 8) ** 2;
    const y = 250 + z * (H - 250);
    const hw = 1 + z * 6;
    g.fillRect(384 - hw, y, hw * 2, 4 + z * 30);
  }
  // горы
  g.fillStyle = "#6f7377";
  g.beginPath();
  g.moveTo(0, 250);
  for (let x = 0; x <= W; x += 32) g.lineTo(x, 250 - 30 - 25 * Math.sin(x * 0.013) - 12 * Math.sin(x * 0.051));
  g.lineTo(W, 250);
  g.fill();
}

// ── симуляция поездки ────────────────────────────────────────────────────

const sim = { t: 0, x: 300, y: 300, course: 30, veh: 30, comp: 30, bear: 30, bearV: 0, bearTarget: 30, speed: 12, dist: 0, hist: [] };

// Эффект поворота как у B (docs/contracts/display.md): компас — почти без сглаживания (~0.1 с),
// иконка догоняет курс быстро,
// карта — критически демпфированной пружиной, мелкие изменения (deadband) карту не крутят.
const RIG = { compassLag: 0.1, vehicleLag: 0.25, mapLag: 1.5, deadband: 4 };
const TURN = { every: 7, dur: 1.6, pattern: [90, 90, -90, 90, -90, -90, 90, 90] };
const smooth = (x) => (x <= 0 ? 0 : x >= 1 ? 1 : x * x * (3 - 2 * x));

/** Курс (развёрнутый, без скачка 360→0) в момент t. */
function courseAt(t) {
  if (ui.motion === "sweep") return 30 + t * 10 + 22 * Math.sin(t * 0.55);
  let c = 30;
  const n = Math.floor(t / TURN.every);
  for (let k = 0; k < n; k++) c += TURN.pattern[k % TURN.pattern.length];
  c += TURN.pattern[n % TURN.pattern.length] * smooth((t - n * TURN.every - (TURN.every - TURN.dur)) / TURN.dur);
  return c + 1.5 * Math.sin(t * 1.3); // лёгкое «рыскание» — его гасит deadband карты
}

function stepSim(dt) {
  sim.t += dt;
  const t = sim.t;
  const target = courseAt(t);
  // развёртка, чтобы фильтры не крутили «длинной дорогой»
  sim.course = sim.veh + ((((target - sim.veh) % 360) + 540) % 360) - 180;
  if (dt > 0) {
    sim.comp += (sim.course - sim.comp) * (1 - Math.exp(-dt / RIG.compassLag));
    sim.veh += (sim.course - sim.veh) * (1 - Math.exp(-dt / RIG.vehicleLag));
    if (Math.abs(sim.course - sim.bearTarget) > RIG.deadband) sim.bearTarget = sim.course;
    else if (Math.abs(sim.course - sim.bearTarget) > 0.5) sim.bearTarget += (sim.course - sim.bearTarget) * dt * 0.3;
    const w = 4 / RIG.mapLag;
    const err = sim.bear - sim.bearTarget;
    const acc = -w * w * err - 2 * w * sim.bearV;
    sim.bearV += acc * dt;
    sim.bear += sim.bearV * dt;
  }
  sim.speed = Math.max(0, 14 + 13 * Math.sin(t * 0.23) + 4 * Math.sin(t * 0.9));
  const d = sim.speed * dt * PX_PER_M;
  sim.dist += sim.speed * dt;
  sim.x += Math.sin(sim.course * DEG) * d;
  sim.y -= Math.cos(sim.course * DEG) * d;
  const last = sim.hist[sim.hist.length - 1];
  if (!last || t - last.t > 0.2) sim.hist.push({ x: sim.x, y: sim.y, t });
  while (sim.hist.length && t - sim.hist[0].t > 60) sim.hist.shift();
}


const COND = {
  ok: {},
  nofix: { hasFix: false, fix: "none", sats: 2, headingSource: "mag" },
  noconn: { connected: false },
  nohead: { headingDeg: null, headingSource: "none" },
  offline: { internet: false },
  norec: { recording: false },
  all: { hasFix: false, fix: "none", sats: 0, connected: false, internet: false, recording: false, headingDeg: null, headingSource: "none", speedMps: null, timeSynced: false },
};

const ui = { units: "kmh", cond: "ok", bg: "sat", paused: false, motion: "turns" };

// предварительный «прогон», чтобы сразу был след
for (let i = 0; i < 600; i++) stepSim(1 / 20);

function makeState(cfg, now) {
  const o = { ...COND[cfg.cond ?? ui.cond], ...(cfg.override ?? {}) };
  const orientation = cfg.orient;
  const src = sim.speed > 3.5 ? "gps" : sim.speed > 2.5 ? "blend" : "mag";
  const norm = (a) => ((a % 360) + 360) % 360;
  const headingDeg = "headingDeg" in o ? o.headingDeg : norm(sim.course);
  const vehicleHeadingDeg = headingDeg == null ? null : norm(sim.veh);
  const compassHeadingDeg = headingDeg == null ? null : norm(sim.comp);
  const headingSource = o.headingSource ?? src;
  const bearingDeg = orientation === "north_up" ? 0 : norm(sim.bear);
  const sats = o.sats ?? 9 + Math.round(3 * Math.sin(sim.t / 7));
  return {
    now,
    telemetry: {
      type: "telemetry",
      t: Date.now() / 1000,
      gps: { fix: o.fix ?? "3d", sats_used: sats, sats_visible: sats + 4, speed_mps: sim.speed, course_deg: norm(sim.course) },
      heading: { deg: headingDeg, source: headingSource },
      display: { mode: cfg.mode ?? "map", reason: "speed", target_zoom: 18, orientation },
      camera: { state: "recording", recording: o.recording ?? true },
      system: { internet: o.internet ?? true, time_synced: o.timeSynced ?? true },
      radio: typeof cfg.radio === "function" ? cfg.radio(now) : cfg.radio ?? null,
      errors: [],
    },
    settings: {
      display: {
        orientation,
        compass_style: cfg.style,
        safe_area_pct: 5,
        speed_units: cfg.units ?? ui.units,
        vehicle_anchor_y: 0.67,
        show_trail: true,
        trail_seconds: 60,
        screen: { w_mm: 92, h_mm: 69 },
        radio_panel: true,
      },
    },
    view: {
      mode: cfg.mode ?? "map",
      orientation,
      bearingDeg,
      headingDeg,
      vehicleHeadingDeg,
      compassHeadingDeg,
      headingSource,
      speedMps: "speedMps" in o ? o.speedMps : sim.speed,
      zoom: 18,
      hasFix: o.hasFix ?? true,
      connected: o.connected ?? true,
      internet: o.internet ?? true,
      recording: o.recording ?? true,
    },
    canvas: { w: W, h: H, safe: SAFE, pxPerMm: W / 92 },
  };
}

// ── радио (docs/contracts/radio.md, §5) ─────────────────────────────────

const RADIO = { state: "playing", station: "Wasteland FM", track: "Sally Chrome - Atomic Sunset", genre: "Oldies", volume: 60, output: "Boombox", output_connected: true };
const LONG = "The Neon Mirage Orchestra - Strip Lights at Midnight (Extended Wasteland Mix)";
const cycle = (list, period) => (now) => list[Math.floor(now / period) % list.length];
const RADIO_VIEWS = [
  [{ radio: RADIO }, "<b>Playing</b> — station and track"],
  [{ radio: { ...RADIO, track: LONG } }, "<b>Long title</b> — slow marquee after a pause, seamless loop"],
  [{ radio: cycle([RADIO, { ...RADIO, track: "Dusty Ray & The Fallouts - Six Feet of Sand" }], 4) }, "<b>Track change</b> every 4 s — a scan sweep wipes in the new title, the needle kicks"],
  [{ radio: cycle([RADIO, { ...RADIO, station: "Mojave Wave 101", track: "Iron Coyote - Highway of Dust", genre: "Rock" }], 5) }, "<b>Station change</b> every 5 s — the needle tunes across the dial"],
  [{ radio: { ...RADIO, state: "connecting", track: null } }, "<b>Connecting</b> — TUNING…, the needle searches"],
  [{ radio: { ...RADIO, state: "waiting_output", track: null, output_connected: false } }, "<b>No speaker</b> — waiting for the Bluetooth speaker (no data is streamed)"],
  [{ radio: { ...RADIO, station: "Rust Belt Radio", track: null, genre: "Jazz" } }, "<b>No track titles</b> from the stream — the genre is shown"],
  [{ radio: cycle([RADIO, { ...RADIO, volume: 85 }, { ...RADIO, volume: 30 }], 3.5) }, "<b>Volume</b> changes — shown for 2.5 s"],
  [{ radio: { ...RADIO, station: "Radio Paradise - Main Mix (EU) 320k AAC", track: LONG }, cond: "all" }, "<b>Everything down</b> + long station name (fits, then ellipsis)"],
  [{ radio: RADIO, bg: "cam", mode: "camera" }, "<b>Camera mode</b> with the radio"],
  [{ radio: RADIO, style: "v2", orient: "north_up" }, "<b>v2 compass, north up</b>"],
];

// ── холсты ───────────────────────────────────────────────────────────────

const views = [];
let tex = null;

function addView(parent, cfg, caption) {
  const fig = document.createElement("figure");
  const wrap = document.createElement("div");
  wrap.className = "screen";
  const cv = document.createElement("canvas");
  cv.width = cfg.w ?? W;
  cv.height = cfg.h ?? H;
  wrap.appendChild(cv);
  fig.appendChild(wrap);
  const cap = document.createElement("figcaption");
  cap.innerHTML = caption;
  fig.appendChild(cap);
  parent.appendChild(fig);
  views.push({ cfg, cv, g: cv.getContext("2d") });
}

function sizeViews() {
  const k = canvasScale(92, W, getPxPerMm()) * parseFloat(document.getElementById("zoom").value);
  for (const v of views) {
    v.cv.style.width = `${v.cv.width * k}px`;
    v.cv.style.height = `${v.cv.height * k}px`;
  }
}

function drawWorld(g, cfg, state, anchor) {
  const bg = cfg.bg ?? ui.bg;
  if (bg === "cam" || state.view.mode === "camera") {
    drawCamera(g, sim.dist);
    return;
  }
  const pat = bg === "dark" ? tex.darkPat : tex.satPat;
  g.save();
  g.translate(anchor.x, anchor.y);
  g.rotate(-state.view.bearingDeg * DEG);
  g.translate(-sim.x, -sim.y);
  g.fillStyle = pat;
  g.fillRect(sim.x - 700, sim.y - 700, 1400, 1400);
  g.restore();
}

function trailPoints(state, anchor) {
  const b = -state.view.bearingDeg * DEG;
  const cs = Math.cos(b);
  const sn = Math.sin(b);
  return sim.hist.map((p) => {
    const dx = p.x - sim.x;
    const dy = p.y - sim.y;
    return { x: anchor.x + dx * cs - dy * sn, y: anchor.y + dx * sn + dy * cs, age_s: sim.t - p.t };
  });
}

const perf = { sum: 0, n: 0, max: 0, shown: 0 };

function renderMain(v, now) {
  const { cfg, g } = v;
  const state = makeState(cfg, now);
  const nu = state.view.orientation === "north_up";
  const anchor = { x: W / 2, y: nu ? H / 2 : H * state.settings.display.vehicle_anchor_y };
  drawWorld(g, cfg, state, anchor);
  if (state.view.mode !== "camera" && (cfg.bg ?? ui.bg) !== "cam") {
    drawTrail(g, trailPoints(state, anchor), state);
    // как у B: heading_up → vehicleHeadingDeg − bearingDeg, north_up → vehicleHeadingDeg
    const vh = sim.veh;
    drawVehicle(g, anchor.x, anchor.y, nu ? vh : vh - state.view.bearingDeg, state);
  }
  const t0 = performance.now();
  drawHud(g, state);
  const dt = performance.now() - t0;
  perf.sum += dt;
  perf.n++;
  perf.max = Math.max(perf.max, dt);
  if (cfg.safeGuide) {
    g.save();
    g.setLineDash([6, 6]);
    g.strokeStyle = "rgba(255,255,255,.35)";
    g.lineWidth = 1;
    g.strokeRect(SAFE.x, SAFE.y, SAFE.w, SAFE.h);
    g.restore();
  }
}

function renderVehicleCloseup(v, now) {
  const { g } = v;
  g.fillStyle = tex.satPat;
  g.fillRect(0, 0, W, H);
  g.fillStyle = "rgba(0,0,0,.35)";
  g.fillRect(0, 0, W, H);
  drawVehicleScaled(g, 220, 288, 7.5, 0);
  drawVehicleScaled(g, 560, 288, 4.5, 90 + 20 * Math.sin(now * 0.6));
  g.font = '26px "FnavRusso", sans-serif';
  g.fillStyle = "#ecf7d8";
  g.textAlign = "center";
  g.fillText("x7.5", 220, 548);
  g.fillText("x4.5", 560, 548);
}

function renderVehicleLife(v, now) {
  const { g } = v;
  g.fillStyle = tex.satPat;
  g.fillRect(0, 0, W, H);
  const st = makeState({ style: "v1", orient: "north_up", cond: "ok" }, now);
  const stNo = makeState({ style: "v1", orient: "north_up", cond: "nofix" }, now);
  for (let i = 0; i < 8; i++) {
    const x = 110 + (i % 4) * 180;
    const y = 150 + Math.floor(i / 4) * 160;
    drawVehicle(g, x, y, i * 45 + (now * 20) % 45, st);
  }
  drawVehicle(g, 200, 470, 0, stNo);
  drawVehicle(g, 560, 470, 0, st);
  g.font = '25px "FnavRusso", sans-serif';
  g.textAlign = "left";
  g.lineWidth = 5;
  g.strokeStyle = "#000";
  g.fillStyle = "#ecf7d8";
  for (const [t, x] of [["NO FIX", 240], ["OK", 600]]) {
    g.strokeText(t, x, 480);
    g.fillText(t, x, 480);
  }
}

function frame(ts) {
  const now = ts / 1000;
  if (!ui.paused) stepSim(Math.min(0.05, frame.last ? now - frame.last : 0));
  frame.last = now;
  for (const v of views) {
    if (v.cfg.kind === "closeup") renderVehicleCloseup(v, now);
    else if (v.cfg.kind === "life") renderVehicleLife(v, now);
    else renderMain(v, now);
  }
  if (now - perf.shown > 1 && perf.n) {
    document.getElementById("perf").textContent =
      `drawHud: avg ${(perf.sum / perf.n).toFixed(2)} ms, max ${perf.max.toFixed(2)} ms`;
    perf.sum = perf.n = perf.max = 0;
    perf.shown = now;
  }
  requestAnimationFrame(frame);
}

// ── запуск ───────────────────────────────────────────────────────────────

async function main() {
  const res = await loadUi();
  console.info("[gallery] loadUi:", res);
  const sat = makeSatTexture();
  const dark = makeDarkTexture();
  const g0 = document.createElement("canvas").getContext("2d");
  tex = { satPat: g0.createPattern(sat, "repeat"), darkPat: g0.createPattern(dark, "repeat") };

  const ORIENT = { heading_up: "Heading up", north_up: "North up" };
  const host = document.getElementById("compasses");
  for (const s of compassStyles) {
    for (const o of ["heading_up", "north_up"]) {
      addView(host, { style: s.id, orient: o, safeGuide: true },
        `<b>${s.id} · ${s.name}</b> — ${ORIENT[o]} <span class="muted">(${o})</span><br><span class="muted">${s.description}</span>`);
    }
  }
  const vh = document.getElementById("vehicle");
  addView(vh, { kind: "closeup" }, "<b>Wasteland muscle car</b> — close-up (not to scale): spiked ram grille, supercharger, armour plates, twin racing stripes, hub spikes, quad exhausts, spare tyre");
  addView(vh, { kind: "life" }, "<b>1:1</b> — north-up rotations; bottom left — no GPS fix (translucent, blinking)");

  const sh = document.getElementById("states");
  addView(sh, { style: "v1", orient: "heading_up", cond: "nofix" }, "<b>NO GPS</b> — car blinks, heading from magnetometer (MAG)");
  addView(sh, { style: "v2", orient: "north_up", cond: "all" }, "<b>Everything down</b>: no link, GPS, heading, internet, recording; clock not synced (dim)");
  addView(sh, { style: "v3", orient: "heading_up", cond: "nohead", units: "mps" }, "<b>Heading unknown</b>, speed in m/s");
  addView(sh, { style: "v1", orient: "heading_up", cond: "ok", bg: "cam", mode: "camera" }, "<b>Camera mode</b> — HUD over video");

  const rh = document.getElementById("radio");
  for (const [cfg, cap] of RADIO_VIEWS) addView(rh, { style: "v1", orient: "heading_up", cond: "ok", ...cfg }, cap);

  sizeViews();
  renderCalibrator(document.getElementById("calibrator"), { onChange: sizeViews });
  onPxPerMmChange(sizeViews);
  document.getElementById("zoom").addEventListener("change", sizeViews);
  for (const id of ["bg", "cond", "units", "motion"]) {
    document.getElementById(id).addEventListener("change", (e) => (ui[id] = e.target.value));
  }
  const tog = (id, cls) => {
    const b = document.getElementById(id);
    b.addEventListener("click", () => {
      const on = document.body.classList.toggle(cls);
      b.classList.toggle("on", on);
    });
  };
  tog("bw", "bw");
  tog("crt", "crt");
  document.getElementById("pause").addEventListener("click", (e) => {
    ui.paused = !ui.paused;
    e.target.classList.toggle("on", ui.paused);
    e.target.textContent = ui.paused ? "Play" : "Pause";
  });
  // ?bw=1 / ?crt=1 / ?zoom=2 / ?pause=1 — для скриншотов
  const q = new URLSearchParams(location.search);
  if (q.get("bw") === "1") document.getElementById("bw").click();
  if (q.get("crt") === "1") document.getElementById("crt").click();
  if (q.get("zoom")) {
    document.getElementById("zoom").value = q.get("zoom");
    sizeViews();
  }
  if (q.get("bg")) ui.bg = document.getElementById("bg").value = q.get("bg");
  if (q.get("motion")) ui.motion = document.getElementById("motion").value = q.get("motion");
  // ?t=34.6 — промотать симуляцию до момента t и поставить на паузу (стоп-кадр поворота)
  if (q.get("t")) {
    const tEnd = parseFloat(q.get("t"));
    while (sim.t < tEnd) stepSim(1 / 60);
    document.getElementById("pause").click();
  }
  if (q.get("cond")) ui.cond = document.getElementById("cond").value = q.get("cond");
  requestAnimationFrame(frame);
}

main().catch((e) => {
  console.error(e);
  document.body.insertAdjacentHTML("afterbegin", `<pre style="color:#f66">${e.stack ?? e}</pre>`);
});
