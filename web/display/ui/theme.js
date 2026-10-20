// Общая «тема» HUD fallout.nav: палитра, шрифты, подписи, кэш спрайтов и мелкие помощники рисования.
// Всё рассчитано на монохромный кинескоп: смысл держится на яркости и форме, не на цвете.

/** Язык сторон света и румбов: "en" → N/E/S/W, WSW (интерфейс — английский); "ru" → С/В/Ю/З, ЗЮЗ. */
export const CARDINAL_LANG = "en";

const POINTS16 = {
  ru: ["С", "ССВ", "СВ", "ВСВ", "В", "ВЮВ", "ЮВ", "ЮЮВ", "Ю", "ЮЮЗ", "ЮЗ", "ЗЮЗ", "З", "ЗСЗ", "СЗ", "ССЗ"],
  en: ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"],
};

/** Подпись для румба по индексу 0..15 (0 = север, шаг 22.5°). */
export function point16(i) {
  return POINTS16[CARDINAL_LANG][((i % 16) + 16) % 16];
}

/** Румб (16 точек) по курсу в градусах. */
export function rhumb(deg) {
  return point16(Math.round(norm360(deg) / 22.5));
}

/** Стороны света [N, E, S, W] на выбранном языке. */
export const CARDINALS = [point16(0), point16(4), point16(8), point16(12)];

// ── палитра ─────────────────────────────────────────────────────────────
// INK — «люминофор»: почти белый с лёгкой зеленцой; на ч/б — самый яркий.
export const C = {
  ink: "#ecf7d8",
  inkDim: "rgba(236,247,216,0.42)",
  inkFaint: "rgba(236,247,216,0.16)",
  accent: "#ffc23d", // янтарь: север, акценты. В ч/б ≈ яркость ink, поэтому север ещё и выделен формой
  dark: "#0a0c08",
  shadow: "rgba(0,0,0,0.88)",
  plate: "rgba(10,12,8,0.70)",
  plateSolid: "#14160f",
  metal: "#878a7c",
  metalHi: "#d2d5c4",
  metalLo: "#3b3d35",
  rust: "#8c5a32",
  brass: "#c9a45c",
  brassHi: "#f1d898",
  brassLo: "#5f4a22",
  rec: "#ff3a2a",
  hazard: "#f2e7c4", // светлая плашка предупреждений (в ч/б — почти белая)
};

// ── шрифты ──────────────────────────────────────────────────────────────
// Имена FontFace регистрирует loadUi(); дальше — запасные системные шрифты.
export const FONT_LABEL = '"FnavRusso", "Russo One", "Arial Black", "Segoe UI Black", "DejaVu Sans", sans-serif';
export const FONT_NUM = '"FnavOswald", "Oswald", "Arial Narrow", "Roboto Condensed", "DejaVu Sans Condensed", sans-serif';
export const FONT_STENCIL = '"FnavStencil", "Black Ops One", "FnavRusso", "Arial Black", "DejaVu Sans", sans-serif';

export const fontLabel = (px) => `${px}px ${FONT_LABEL}`;
export const fontNum = (px, w = 700) => `${w} ${px}px ${FONT_NUM}`;
export const fontStencil = (px) => `${px}px ${FONT_STENCIL}`;

/** Минимальная высота шрифта (кегль) на холсте 768×576 — ≈3 мм на RV‑50. */
export const MIN_TEXT_PX = 25;

// ── математика ──────────────────────────────────────────────────────────
export const DEG = Math.PI / 180;
export function norm360(d) {
  return ((d % 360) + 360) % 360;
}
/** Мигание: true в «включённой» фазе. hz — частота, duty — доля включения. */
export function blinkOn(now, hz = 1, duty = 0.5) {
  const ph = (now * hz) % 1;
  return ph < duty;
}
/** Плавная пульсация 0..1 (для полупрозрачного мигания). */
export function pulse(now, hz = 1) {
  return 0.5 - 0.5 * Math.cos(now * hz * 2 * Math.PI);
}

// ── кэш спрайтов ────────────────────────────────────────────────────────
/** Во сколько раз спрайты детальнее логического холста (для HiDPI и чистого поворота). */
export const SS = 2;

const sprites = new Map();
let spriteEpoch = 0;

export function makeCanvas(w, h) {
  if (typeof OffscreenCanvas !== "undefined") return new OffscreenCanvas(w, h);
  if (typeof document !== "undefined") {
    const c = document.createElement("canvas");
    c.width = w;
    c.height = h;
    return c;
  }
  return null;
}

/**
 * Пререндеренный спрайт размера w×h (логические px). draw(ctx) рисует в логических координатах.
 * Возвращает { canvas, w, h } или null, если холст недоступен.
 */
export function sprite(key, w, h, draw, ss = SS) {
  const k = `${spriteEpoch}|${key}|${w}x${h}@${ss}`;
  let s = sprites.get(k);
  if (s !== undefined) return s;
  const cw = Math.ceil(w * ss);
  const ch = Math.ceil(h * ss);
  const canvas = makeCanvas(cw, ch);
  if (!canvas) {
    sprites.set(k, null);
    return null;
  }
  const c = canvas.getContext("2d");
  c.scale(ss, ss);
  draw(c);
  s = { canvas, w, h };
  sprites.set(k, s);
  return s;
}

/** Сбросить кэш (после загрузки шрифтов). */
export function clearSprites() {
  sprites.clear();
  spriteEpoch++;
}

/** Нарисовать спрайт с центром в (x,y), повернув на rad. */
export function blitCentered(ctx, s, x, y, rad = 0, alpha = 1) {
  if (!s) return;
  ctx.save();
  ctx.translate(x, y);
  if (rad) ctx.rotate(rad);
  if (alpha !== 1) ctx.globalAlpha *= alpha;
  ctx.drawImage(s.canvas, -s.w / 2, -s.h / 2, s.w, s.h);
  ctx.restore();
}

/** Нарисовать спрайт левым верхним углом в (x,y). */
export function blit(ctx, s, x, y) {
  if (s) ctx.drawImage(s.canvas, x, y, s.w, s.h);
}

// ── метрики шрифтов ─────────────────────────────────────────────────────
const capCache = new Map();
/** Высота прописных/цифр (px) для данного font — по measureText, с кэшем. */
export function capHeight(ctx, font, sample = "0") {
  const k = `${spriteEpoch}|${font}|${sample}`;
  let v = capCache.get(k);
  if (v !== undefined) return v;
  ctx.save();
  ctx.font = font;
  const m = ctx.measureText(sample);
  ctx.restore();
  const px = parseFloat(/(\d+(?:\.\d+)?)px/.exec(font)?.[1] ?? "20");
  v = m.actualBoundingBoxAscent > 0 ? m.actualBoundingBoxAscent : px * 0.72;
  capCache.set(k, v);
  return v;
}

const widthCache = new Map();
export function textWidth(ctx, font, text) {
  const k = `${spriteEpoch}|${font}|${text}`;
  let v = widthCache.get(k);
  if (v !== undefined) return v;
  ctx.save();
  ctx.font = font;
  v = ctx.measureText(text).width;
  ctx.restore();
  if (widthCache.size > 600) widthCache.clear();
  widthCache.set(k, v);
  return v;
}

// ── текст ───────────────────────────────────────────────────────────────
/**
 * Текст с тёмной обводкой (читается на пёстрой карте). y — центр высоты прописных.
 */
export function outlinedText(ctx, text, x, y, font, { fill = C.ink, stroke = C.shadow, lw = 5, align = "center" } = {}) {
  ctx.font = font;
  ctx.textAlign = align;
  ctx.textBaseline = "alphabetic";
  const yb = y + capHeight(ctx, font) / 2;
  if (lw > 0) {
    ctx.lineJoin = "round";
    ctx.lineWidth = lw;
    ctx.strokeStyle = stroke;
    ctx.strokeText(text, x, yb);
  }
  ctx.fillStyle = fill;
  ctx.fillText(text, x, yb);
}

/**
 * Цифры в фиксированных ячейках (без «прыжков» при смене значения).
 * xRight — правый край, y — центр высоты цифр. ghost — «незажжённые» восьмёрки как на табло.
 */
export function fixedDigits(ctx, str, xRight, y, font, cellW, { fill = C.ink, lw = 5, ghost = 0, ghostFill = C.inkFaint } = {}) {
  ctx.font = font;
  ctx.textAlign = "center";
  ctx.textBaseline = "alphabetic";
  const yb = y + capHeight(ctx, font) / 2;
  ctx.lineJoin = "round";
  if (ghost > 0) {
    ctx.fillStyle = ghostFill;
    for (let i = 0; i < ghost; i++) ctx.fillText("8", xRight - cellW * (i + 0.5), yb);
  }
  for (let i = 0; i < str.length; i++) {
    const ch = str[str.length - 1 - i];
    const cx = xRight - cellW * (i + 0.5);
    if (lw > 0) {
      ctx.lineWidth = lw;
      ctx.strokeStyle = C.shadow;
      ctx.strokeText(ch, cx, yb);
    }
    ctx.fillStyle = fill;
    ctx.fillText(ch, cx, yb);
  }
}

// ── «железо»: плашки, заклёпки, винты ───────────────────────────────────
export function rivet(ctx, x, y, r = 3) {
  ctx.beginPath();
  ctx.arc(x, y, r + 1, 0, 2 * Math.PI);
  ctx.fillStyle = C.dark;
  ctx.fill();
  const g = ctx.createRadialGradient(x - r * 0.4, y - r * 0.4, r * 0.1, x, y, r);
  g.addColorStop(0, C.metalHi);
  g.addColorStop(0.6, C.metal);
  g.addColorStop(1, C.metalLo);
  ctx.beginPath();
  ctx.arc(x, y, r, 0, 2 * Math.PI);
  ctx.fillStyle = g;
  ctx.fill();
}

export function screw(ctx, x, y, r, rot = 0.6, light = C.brassHi, mid = C.brass, low = C.brassLo) {
  rivetLike(ctx, x, y, r, light, mid, low);
  ctx.save();
  ctx.translate(x, y);
  ctx.rotate(rot);
  ctx.fillStyle = C.dark;
  ctx.fillRect(-r * 0.8, -r * 0.18, r * 1.6, r * 0.36);
  ctx.restore();
}

function rivetLike(ctx, x, y, r, light, mid, low) {
  ctx.beginPath();
  ctx.arc(x, y, r + 1, 0, 2 * Math.PI);
  ctx.fillStyle = C.dark;
  ctx.fill();
  const g = ctx.createRadialGradient(x - r * 0.4, y - r * 0.4, r * 0.1, x, y, r);
  g.addColorStop(0, light);
  g.addColorStop(0.6, mid);
  g.addColorStop(1, low);
  ctx.beginPath();
  ctx.arc(x, y, r, 0, 2 * Math.PI);
  ctx.fillStyle = g;
  ctx.fill();
}

/** Контур плашки со срезанными углами (в стиле «пустоши»). */
export function chamferPath(ctx, x, y, w, h, c = 8) {
  ctx.beginPath();
  ctx.moveTo(x + c, y);
  ctx.lineTo(x + w - c, y);
  ctx.lineTo(x + w, y + c);
  ctx.lineTo(x + w, y + h - c);
  ctx.lineTo(x + w - c, y + h);
  ctx.lineTo(x + c, y + h);
  ctx.lineTo(x, y + h - c);
  ctx.lineTo(x, y + c);
  ctx.closePath();
}

/**
 * Пререндеренная тёмная металлическая плашка w×h (логические px), с заклёпками по углам.
 * Рисуется blit(ctx, plateSprite(...), x, y).
 */
export function plateSprite(w, h, { chamfer = 8, rivets = true, fill = C.plate, border = C.metal } = {}) {
  return sprite(`plate|${chamfer}|${rivets}|${fill}|${border}`, w, h, (c) => {
    chamferPath(c, 1.5, 1.5, w - 3, h - 3, chamfer);
    c.fillStyle = fill;
    c.fill();
    // царапины/потёртости — едва заметные диагонали (стабильный псевдослучай)
    c.save();
    c.clip();
    c.strokeStyle = "rgba(236,247,216,0.05)";
    c.lineWidth = 1;
    let s = (w * 131 + h * 17) % 997;
    for (let i = 0; i < Math.round((w * h) / 900); i++) {
      s = (s * 9301 + 49297) % 233280;
      const px = (s / 233280) * w;
      s = (s * 9301 + 49297) % 233280;
      const py = (s / 233280) * h;
      c.beginPath();
      c.moveTo(px, py);
      c.lineTo(px + 10, py - 4);
      c.stroke();
    }
    c.restore();
    chamferPath(c, 1.5, 1.5, w - 3, h - 3, chamfer);
    c.lineWidth = 3;
    c.strokeStyle = C.dark;
    c.stroke();
    chamferPath(c, 2.5, 2.5, w - 5, h - 5, chamfer - 1);
    c.lineWidth = 2;
    c.strokeStyle = border;
    c.stroke();
    if (rivets && w > 40 && h > 26) {
      const ins = chamfer * 0.5 + 6;
      rivet(c, ins, ins, 2.6);
      rivet(c, w - ins, ins, 2.6);
      rivet(c, ins, h - ins, 2.6);
      rivet(c, w - ins, h - ins, 2.6);
    }
  });
}

/** Светлая «аварийная» плашка с косыми полосами по краям (для НЕТ GPS / НЕТ СВЯЗИ). */
export function hazardSprite(w, h) {
  return sprite("hazard", w, h, (c) => {
    chamferPath(c, 1.5, 1.5, w - 3, h - 3, 7);
    c.fillStyle = C.hazard;
    c.fill();
    c.save();
    c.clip();
    c.fillStyle = C.dark;
    const band = 22;
    for (const x0 of [0, w - band]) {
      c.save();
      c.beginPath();
      c.rect(x0, 0, band, h);
      c.clip();
      for (let k = -h; k < band + h; k += 12) {
        c.beginPath();
        c.moveTo(x0 + k, h);
        c.lineTo(x0 + k + 6, h);
        c.lineTo(x0 + k + 6 + h, 0);
        c.lineTo(x0 + k + h, 0);
        c.closePath();
        c.fill();
      }
      c.restore();
    }
    c.restore();
    chamferPath(c, 1.5, 1.5, w - 3, h - 3, 7);
    c.lineWidth = 3;
    c.strokeStyle = C.dark;
    c.stroke();
  });
}

/** Безопасное чтение вложенных настроек. */
export function setting(state, path, dflt) {
  let o = state?.settings;
  for (const p of path.split(".")) {
    if (o == null) return dflt;
    o = o[p];
  }
  return o ?? dflt;
}

/** Прямоугольник зоны безопасности (с запасным расчётом, если B его не передал). */
export function safeRect(state) {
  const cv = state?.canvas ?? {};
  if (cv.safe) return cv.safe;
  const w = cv.w ?? 768;
  const h = cv.h ?? 576;
  const p = setting(state, "display.safe_area_pct", 5) / 100;
  return { x: w * p, y: h * p, w: w * (1 - 2 * p), h: h * (1 - 2 * p) };
}
