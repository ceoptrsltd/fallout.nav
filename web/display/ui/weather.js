// Погода: плашка «сейчас + 2 дня» на месте плашки радио, пока радио выключено, и ветер вокруг машины на карте.
// Данные — state.weather (сообщение {"type":"weather"} от fnav-core: fnav/core/weather.py, Open-Meteo + кэш).
// Монохромный кинескоп: погода читается по форме значка (солнце, облако, капли, снежинки, молния, туман),
// цвет — только акцент. Устаревший прогноз (нет интернета дольше 3 ч) — цифры тусклее.

import { radioRect } from "./radio.js";
import { indicatorsWidth } from "./status.js";
import { C, DEG, blit, fontNum, outlinedText, plateSprite, setting, sprite, textWidth } from "./theme.js";

const PAD = 12;
const ICON = { w: 38, h: 34 };
const DAY_ICON = { w: 26, h: 23 };
const DAYS_LBL = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"];

// ── чистые помощники (tests/display/weather.test.mjs) ─────────────────────

/** Код погоды WMO → вид значка. */
export function weatherKind(code) {
  if (!Number.isFinite(code)) return "cloudy";
  if (code === 0) return "clear";
  if (code <= 2) return "partly";
  if (code === 3) return "cloudy";
  if (code === 45 || code === 48) return "fog";
  if (code >= 51 && code <= 57) return "drizzle";
  if ((code >= 61 && code <= 67) || (code >= 80 && code <= 82)) return "rain";
  if ((code >= 71 && code <= 77) || code === 85 || code === 86) return "snow";
  if (code >= 95) return "thunder";
  return "cloudy";
}

/** «12°», «-3°»; нет данных — «--°». */
export function fmtTemp(t) {
  if (!Number.isFinite(t)) return "--°";
  const r = Math.round(t);
  return `${r === 0 ? 0 : r}°`;
}

/** Показывать ли плашку погоды (когда нет плашки радио — решает index.js). */
export function weatherVisible(state) {
  const w = state?.weather;
  return !!w && Number.isFinite(w.temp_c) && setting(state, "display.weather_panel", true) !== false;
}

/** Куда дует ветер на экране, радианы по часовой от «вверх»: с учётом поворота карты. null — штиль/нет данных. */
export function windScreenAngle(state) {
  const w = state?.weather;
  if (!w || !Number.isFinite(w.wind_from_deg) || !Number.isFinite(w.wind_ms)) return null;
  return ((w.wind_from_deg + 180 - (state?.view?.bearingDeg || 0)) % 360) * DEG;
}

// ── значки (векторные, в спрайтах; рисуются в рамке 32×30 и масштабируются) ──

function cloud(c, ox = 0, oy = 0, k = 1) {
  c.save();
  c.translate(ox, oy);
  c.scale(k, k);
  c.beginPath();
  c.arc(10, 16, 6, 0, 2 * Math.PI);
  c.arc(17, 11.5, 8, 0, 2 * Math.PI);
  c.arc(24.5, 16, 5.5, 0, 2 * Math.PI);
  c.rect(10, 14, 14.5, 8);
  c.lineJoin = "round";
  c.lineWidth = 4 / k;
  c.strokeStyle = C.dark;
  c.stroke();
  c.fillStyle = C.ink;
  c.fill("nonzero");
  c.restore();
}

function sun(c, x, y, r, rays = true) {
  if (rays) {
    c.lineCap = "round";
    for (const [lw, col] of [[4.5, C.dark], [2.2, C.ink]]) {
      c.lineWidth = lw;
      c.strokeStyle = col;
      for (let i = 0; i < 8; i++) {
        const a = (i / 8) * 2 * Math.PI;
        c.beginPath();
        c.moveTo(x + Math.cos(a) * (r + 2.5), y + Math.sin(a) * (r + 2.5));
        c.lineTo(x + Math.cos(a) * (r + 5.5), y + Math.sin(a) * (r + 5.5));
        c.stroke();
      }
    }
  }
  c.beginPath();
  c.arc(x, y, r, 0, 2 * Math.PI);
  c.lineWidth = 3;
  c.strokeStyle = C.dark;
  c.stroke();
  c.fillStyle = C.accent;
  c.fill();
}

function moon(c, x, y, r) {
  c.save();
  c.beginPath();
  c.arc(x, y, r, 0, 2 * Math.PI);
  c.arc(x + r * 0.55, y - r * 0.35, r * 0.85, 0, 2 * Math.PI, true);
  c.lineWidth = 3;
  c.strokeStyle = C.dark;
  c.stroke();
  c.fillStyle = C.accent;
  c.fill("evenodd");
  c.restore();
}

function strokes(c, segs, lw = 2.4) {
  c.lineCap = "round";
  for (const [w, col] of [[lw + 2.6, C.dark], [lw, C.ink]]) {
    c.lineWidth = w;
    c.strokeStyle = col;
    for (const [x1, y1, x2, y2] of segs) {
      c.beginPath();
      c.moveTo(x1, y1);
      c.lineTo(x2, y2);
      c.stroke();
    }
  }
}

function drawIcon(c, kind, day) {
  const up = ["rain", "drizzle", "snow", "thunder"].includes(kind) ? -3 : 0; // осадки — облако выше
  switch (kind) {
    case "clear":
      if (day) sun(c, 16, 15, 7);
      else moon(c, 15, 15, 9);
      break;
    case "partly":
      if (day) sun(c, 11, 10, 5.5);
      else moon(c, 11, 10, 6.5);
      cloud(c, 5, 6, 0.82);
      break;
    case "fog":
      cloud(c, 0, -5, 0.9);
      strokes(c, [[5, 22, 27, 22], [8, 27, 24, 27]]);
      break;
    case "drizzle":
      cloud(c, 0, up);
      strokes(c, [[10, 24, 10, 24.1], [17, 26, 17, 26.1], [24, 24, 24, 24.1]], 3);
      break;
    case "rain":
      cloud(c, 0, up);
      strokes(c, [[11, 22, 9, 28], [17.5, 22, 15.5, 28], [24, 22, 22, 28]]);
      break;
    case "snow":
      cloud(c, 0, up);
      for (const [x, y] of [[10, 25], [17, 27], [24, 25]]) {
        strokes(c, [[x - 2.5, y, x + 2.5, y], [x - 1.3, y - 2.2, x + 1.3, y + 2.2], [x - 1.3, y + 2.2, x + 1.3, y - 2.2]], 1.6);
      }
      break;
    case "thunder":
      cloud(c, 0, up);
      c.beginPath();
      c.moveTo(18, 18);
      c.lineTo(13, 25);
      c.lineTo(17, 25);
      c.lineTo(14, 30);
      c.lineTo(22, 22);
      c.lineTo(18, 22);
      c.lineTo(20, 18);
      c.closePath();
      c.lineJoin = "round";
      c.lineWidth = 3;
      c.strokeStyle = C.dark;
      c.stroke();
      c.fillStyle = C.accent;
      c.fill();
      break;
    default:
      cloud(c, 0, 1);
  }
}

function iconSprite(kind, day, w, h) {
  return sprite(`wx|${kind}|${day ? 1 : 0}`, w, h, (c) => {
    c.scale(w / 32, h / 30);
    drawIcon(c, kind, day);
  });
}

/** Стрелка «куда дует» (центр x,y; длина len; угол a — по часовой от «вверх»). */
function windArrow(ctx, x, y, a, len, lw = 2.4) {
  const dx = Math.sin(a), dy = -Math.cos(a);
  const hx = x + dx * len / 2, hy = y + dy * len / 2;
  const tx = x - dx * len / 2, ty = y - dy * len / 2;
  const head = len * 0.38;
  const path = () => {
    ctx.beginPath();
    ctx.moveTo(tx, ty);
    ctx.lineTo(hx, hy);
    for (const s of [-1, 1]) {
      const b = a + Math.PI + s * 0.5;
      ctx.moveTo(hx, hy);
      ctx.lineTo(hx + Math.sin(b) * head, hy - Math.cos(b) * head);
    }
  };
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  path();
  ctx.lineWidth = lw + 2.8;
  ctx.strokeStyle = C.shadow;
  ctx.stroke();
  path();
  ctx.lineWidth = lw;
  ctx.strokeStyle = C.ink;
  ctx.stroke();
}

// ── плашка ────────────────────────────────────────────────────────────────

/**
 * Плашка погоды на месте плашки радио: слева — сейчас (значок, температура, ветер со стрелкой «куда дует»
 * относительно экрана), справа — завтра и послезавтра (день, значок, макс/мин). Возвращает прямоугольник или null.
 */
export function drawWeather(ctx, state, safe, liftPx = 0) {
  if (!weatherVisible(state)) return null;
  const w = state.weather;
  const r = radioRect(safe, indicatorsWidth(ctx), liftPx);
  const { x, y, h } = r;
  const x1 = x + r.w - PAD;
  blit(ctx, plateSprite(r.w, h, { chamfer: 9 }), x, y);
  const ink = w.stale ? C.inkDim : C.ink;

  // сейчас: значок + температура (ширина блока — постоянная, по «88°»)
  const tf = fontNum(42, 700);
  const tempW = Math.ceil(textWidth(ctx, tf, "88°"));
  const ix = x + PAD - 2;
  blit(ctx, iconSprite(weatherKind(w.code), w.is_day !== false, ICON.w, ICON.h), ix, y + 8);
  const tx = ix + ICON.w + 4;
  const temp = fmtTemp(w.temp_c);
  const tfit = textWidth(ctx, tf, temp) <= tempW + 4 ? tf : fontNum(34, 700); // «-12°» — чуть мельче
  outlinedText(ctx, temp, tx, y + 27, tfit, { align: "left", lw: 5, fill: ink });
  // ветер: стрелка «куда дует» (как струйки на карте) + скорость
  const wy = y + h - 19;
  const a = windScreenAngle(state);
  if (a !== null && Number.isFinite(w.wind_ms)) {
    if (w.wind_ms >= 0.5) windArrow(ctx, ix + 12, wy, a, 18);
    outlinedText(ctx, `${Math.round(w.wind_ms)} m/s`, ix + 28, wy, fontNum(25, 600), { align: "left", lw: 4, fill: ink });
  }

  // черта
  const xs = tx + tempW + 6;
  ctx.fillStyle = C.metal;
  ctx.fillRect(xs, y + 12, 2, h - 24);

  // завтра, послезавтра: «MON [значок] 14°/6°» (день — узкими цифрами спидометра: место под мин/макс)
  const lf = fontNum(25, 600);
  const nf = fontNum(25, 600);
  const lblW = Math.max(...DAYS_LBL.map((d) => textWidth(ctx, lf, d)));
  const dx0 = xs + 9;
  (w.days || []).slice(0, 2).forEach((d, k) => {
    const cy = y + 24 + k * 36;
    outlinedText(ctx, d.weekday, dx0, cy, lf, { align: "left", lw: 4, fill: k === 0 ? C.accent : ink });
    const icx = dx0 + lblW + 5;
    blit(ctx, iconSprite(weatherKind(d.code), true, DAY_ICON.w, DAY_ICON.h), icx, Math.round(cy - DAY_ICON.h / 2 - 1));
    const room = x1 - (icx + DAY_ICON.w + 5);
    const hi = fmtTemp(d.t_max), lo = `/${fmtTemp(d.t_min)}`;
    if (textWidth(ctx, nf, hi + lo) <= room) {
      outlinedText(ctx, lo, x1, cy, nf, { align: "right", lw: 4, fill: C.inkDim });
      outlinedText(ctx, hi, x1 - textWidth(ctx, nf, lo), cy, nf, { align: "right", lw: 4, fill: ink });
    } else {
      outlinedText(ctx, hi, x1, cy, nf, { align: "right", lw: 4, fill: ink }); // не помещается — только максимум
    }
  });
  return r;
}

// ── ветер на карте ────────────────────────────────────────────────────────

// постоянный «случайный» рисунок струек: смещение поперёк ветра (−1…1), фаза, своя скорость
const N_STREAKS = 18;
const STREAKS = Array.from({ length: N_STREAKS }, (_, i) => {
  const f = (k) => ((Math.sin((i + 1) * 12.9898 * k) * 43758.5453) % 1 + 1) % 1;
  return { lat: (i / (N_STREAKS - 1)) * 2 - 1 + (f(1) - 0.5) * 0.08, phase: f(2), k: 0.8 + 0.4 * f(3), len: 0.8 + 0.4 * f(4) };
});
const R_OUT = 120; // радиус «облака» струек вокруг машины, px
const R_IN = 34; // у самой машины струек нет — иконка остаётся чистой

const smooth = (a, b, v) => {
  const t = Math.max(0, Math.min(1, (v - a) / (b - a)));
  return t * t * (3 - 2 * t);
};

/**
 * Ветер вокруг машины: струйки со стрелками летят туда, куда дует ветер, — с учётом поворота карты, то есть
 * относительно машины (встречный — сверху вниз в heading-up). Скорость и длина струек — от силы ветра.
 * Только на карте; display.wind_on_map. Машина — в state.view.vehicleX/Y.
 */
export function drawWind(ctx, state) {
  const w = state?.weather;
  const v = state?.view ?? {};
  if (!w || v.mode === "camera" || setting(state, "display.wind_on_map", true) === false) return;
  const a = windScreenAngle(state);
  if (a === null || !(w.wind_ms >= 0.5)) return;
  const cw = state?.canvas?.w ?? 768, ch = state?.canvas?.h ?? 576;
  const cx = Number.isFinite(v.vehicleX) ? v.vehicleX : cw / 2;
  const cy = Number.isFinite(v.vehicleY) ? v.vehicleY : ch * 0.67;
  const dx = Math.sin(a), dy = -Math.cos(a);
  const px = -dy, py = dx;
  const speed = Math.max(18, Math.min(140, 18 + 7 * w.wind_ms)); // px/с
  const baseLen = Math.max(18, Math.min(44, 16 + 2 * w.wind_ms));
  const t = state?.now ?? 0;
  const fade = w.stale ? 0.6 : 0.95;
  ctx.save();
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  for (const s of STREAKS) {
    const lat = s.lat * R_OUT;
    const along = ((s.phase * 2 * R_OUT + t * speed * s.k) % (2 * R_OUT)) - R_OUT;
    const d = Math.hypot(along, lat);
    if (d >= R_OUT) continue;
    const alpha = Math.min(1, 1.25 * (1 - (d / R_OUT) ** 2)) * smooth(R_IN, R_IN + 16, d) * fade;
    if (alpha < 0.04) continue;
    const len = baseLen * s.len;
    const hx = cx + dx * along + px * lat, hy = cy + dy * along + py * lat;
    const path = () => {
      ctx.beginPath();
      ctx.moveTo(hx - dx * len, hy - dy * len);
      ctx.lineTo(hx, hy);
      for (const sg of [-1, 1]) {
        const b = a + Math.PI + sg * 0.55;
        ctx.moveTo(hx, hy);
        ctx.lineTo(hx + Math.sin(b) * 7, hy - Math.cos(b) * 7);
      }
    };
    path();
    ctx.globalAlpha = alpha * 0.8;
    ctx.lineWidth = 6.5;
    ctx.strokeStyle = C.shadow;
    ctx.stroke();
    ctx.globalAlpha = alpha;
    ctx.lineWidth = 3;
    ctx.strokeStyle = C.ink;
    ctx.stroke();
  }
  ctx.restore();
}
