// Спидометр: крупные цифры (км/ч или м/с) + сегментная шкала, на металлической плашке.

import { C, blit, fixedDigits, fontLabel, fontNum, outlinedText, plateSprite, setting, textWidth } from "./theme.js";

export const SPEED_BOX = { w: 236, h: 116 };

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
