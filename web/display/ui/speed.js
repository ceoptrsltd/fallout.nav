// Спидометр: крупные цифры (км/ч или м/с) + сегментная шкала, на металлической плашке.

import { C, blinkOn, blit, capHeight, fixedDigits, fontLabel, fontNum, outlinedText, plateSprite, setting, textWidth } from "./theme.js";

export const SPEED_BOX = { w: 236, h: 116 };

const LIMIT_D = 56; // знак разрешённой скорости (telemetry.speed_limit), диаметр
const OVER_KMH = 3; // быстрее разрешённого больше чем на столько — знак мигает
const SIGN_WHITE = "#f4f1e6";
const SIGN_RED = "#d8261c";

/** Разрешённая скорость для знака у спидометра (км/ч) или null. */
export function speedLimitKmh(state) {
  const l = state?.telemetry?.speed_limit;
  if (!l || !Number.isFinite(l.kmh) || setting(state, "display.speed_limit", true) === false) return null;
  return l.kmh;
}

/** Едем быстрее разрешённого (speedKmh — по спидометру, в км/ч). */
export function overLimit(speedKmh, limitKmh) {
  return Number.isFinite(speedKmh) && Number.isFinite(limitKmh) && speedKmh > limitKmh + OVER_KMH;
}

/**
 * Знак как на дороге: белый круг, красный обод, чёрные цифры (на ч/б обод — серый, цифры читаются).
 * Превышение — мигает «наоборот»: красный круг, белые цифры. Центр (cx, cy) — над правым углом спидометра.
 */
function drawLimitSign(ctx, state, cx, cy) {
  const kmh = speedLimitKmh(state);
  if (kmh === null) return;
  const mps = state?.view?.speedMps;
  const speedKmh = Number.isFinite(mps) ? mps * 3.6 : null;
  const alert = overLimit(speedKmh, kmh) && blinkOn(state?.now ?? 0, 2, 0.5);
  const r = LIMIT_D / 2;
  ctx.save();
  ctx.beginPath();
  ctx.arc(cx, cy, r + 1.5, 0, 2 * Math.PI);
  ctx.fillStyle = C.dark;
  ctx.fill();
  ctx.beginPath();
  ctx.arc(cx, cy, r - 1, 0, 2 * Math.PI);
  ctx.fillStyle = alert ? SIGN_RED : SIGN_WHITE;
  ctx.fill();
  ctx.beginPath();
  ctx.arc(cx, cy, r - 4.5, 0, 2 * Math.PI);
  ctx.lineWidth = 7;
  ctx.strokeStyle = alert ? SIGN_WHITE : SIGN_RED;
  ctx.stroke();
  const txt = String(kmh);
  const font = fontNum(txt.length > 2 ? 25 : 30, 700);
  ctx.font = font;
  ctx.textAlign = "center";
  ctx.textBaseline = "alphabetic";
  ctx.fillStyle = alert ? SIGN_WHITE : C.dark;
  ctx.fillText(txt, cx, cy + capHeight(ctx, font) / 2);
  ctx.restore();
}

const SEGMENTS = 14;

/** (x, y) — левый верхний угол плашки. */
export function drawSpeed(ctx, state, x, y) {
  const { w, h } = SPEED_BOX;
  const units = setting(state, "display.speed_units", "kmh");
  const mps = state?.view?.speedMps;
  const known = typeof mps === "number" && Number.isFinite(mps);
  const kmh = units !== "mps";
  const value = known ? Math.max(0, kmh ? mps * 3.6 : mps) : null;
  const full = kmh ? 140 : 40; // верх сегментной шкалы

  blit(ctx, plateSprite(w, h, { chamfer: 10 }), x, y);

  let str;
  if (value == null) str = "--";
  else if (!kmh && value < 10) str = (Math.round(value * 10) / 10).toFixed(1);
  else str = String(Math.min(999, Math.round(value)));

  const fN = fontNum(86, 700);
  const cell = Math.round(textWidth(ctx, fN, "0") + 2);
  const xr = x + 16 + cell * 3;
  const cyDigits = y + 50;
  if (str.includes(".")) {
    // «7.4» м/с: целая часть + точка + десятые, ячейки фиксированы
    const [a, b] = str.split(".");
    fixedDigits(ctx, b, xr, cyDigits, fN, cell, { lw: 6 });
    fixedDigits(ctx, a, xr - cell - 6, cyDigits, fN, cell, { lw: 6 });
    ctx.fillStyle = C.ink;
    ctx.fillRect(xr - cell - 5, cyDigits + 28, 6, 7);
  } else {
    fixedDigits(ctx, str, xr, cyDigits, fN, cell, { lw: 6, fill: known ? C.ink : C.inkDim });
  }

  // единицы — справа дробью: km / h
  const ux = x + w - 34;
  outlinedText(ctx, kmh ? "km" : "m", ux, y + 30, fontLabel(25), { lw: 4 });
  ctx.fillStyle = C.ink;
  ctx.fillRect(ux - 18, y + 47, 36, 3);
  outlinedText(ctx, kmh ? "h" : "s", ux, y + 66, fontLabel(25), { lw: 4 });

  drawLimitSign(ctx, state, x + w - 34, y - 12);

  // сегментная шкала: светлые = зажжены, тёмные с рамкой = нет (различимо и в ч/б)
  const sx = x + 14;
  const sy = y + h - 22;
  const sw = w - 28;
  const gap = 3;
  const segW = (sw - gap * (SEGMENTS - 1)) / SEGMENTS;
  const lit = value == null ? 0 : Math.min(SEGMENTS, Math.ceil((value / full) * SEGMENTS - 0.001));
  for (let i = 0; i < SEGMENTS; i++) {
    const px = sx + i * (segW + gap);
    const hh = 8 + Math.round((i / (SEGMENTS - 1)) * 5);
    const py = sy + 13 - hh;
    if (i < lit) {
      ctx.fillStyle = C.ink;
      ctx.fillRect(px, py, segW, hh);
    } else {
      ctx.fillStyle = "rgba(236,247,216,0.12)";
      ctx.fillRect(px, py, segW, hh);
      ctx.strokeStyle = "rgba(236,247,216,0.35)";
      ctx.lineWidth = 1;
      ctx.strokeRect(px + 0.5, py + 0.5, segW - 1, hh - 1);
    }
  }
}
