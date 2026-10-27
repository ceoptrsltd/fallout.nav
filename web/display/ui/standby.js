// Экран «на охране» (settings.display.armed_standby): пока машина на охране (когда — решает map/standby.js),
// на экран никто не смотрит — вместо карты и HUD чёрный фон, тусклое ALARM ON с замком и часы. app.js рисует его
// раз в секунду (картинка меняется раз в минуту), без карты, тайлов, камеры, ветра и опроса геймпада.
// «Ставят на охрану» (arming) и «сработала» (triggered) — обычный экран (сработала — с крупной плашкой).
//
// Блок раз в минуту сдвигается на несколько px: статичная картинка часами не выжигает люминофор кинескопа.

import { drawLock } from "./alarm.js";
import { CLOCK_H, clockWidth, drawClock } from "./clock.js";
import { C, fontStencil, outlinedText, textWidth } from "./theme.js";

const TEXT = "ALARM ON";
const LOCK = 40; // высота замка, px
const GAP = 16; // замок — надпись
const DRIFT_X = [0, 24, -18, 10, -26, 16, -8, 22, -14, 4]; // сдвиги по минутам, px
const DRIFT_Y = [0, -14, 10, -6, 16, -18, 6, 12, -10, -2];

/** Сдвиг блока в минуту ms (время экрана): шаг раз в минуту, повтор через 10 минут. */
export function standbyDrift(ms) {
  const m = Math.floor((Number.isFinite(ms) ? ms : 0) / 60000);
  const i = ((m % 10) + 10) % 10;
  return { dx: DRIFT_X[i], dy: DRIFT_Y[i] };
}

/** Весь экран «на охране» (чёрный фон на весь холст). Возвращает прямоугольник блока. */
export function drawStandby(ctx, state) {
  const cw = state?.canvas?.w ?? 768;
  const ch = state?.canvas?.h ?? 576;
  ctx.save();
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, cw, ch);

  const { dx, dy } = standbyDrift(state?.view?.wallMs ?? Date.now());
  const font = fontStencil(54);
  const tw = textWidth(ctx, font, TEXT);
  const lockW = LOCK * 0.8;
  const rowW = lockW + GAP + tw;
  const cWidth = clockWidth(ctx);
  const blockH = 64 + 26 + CLOCK_H;
  const cx = Math.round(cw / 2 + dx);
  const y0 = Math.round((ch - blockH) / 2 + dy);

  // замок + ALARM ON — тусклые: экран не светит в салон
  const rx = cx - rowW / 2;
  const ty = y0 + 32;
  drawLock(ctx, rx + lockW / 2, ty - 2, LOCK, false, C.inkDim, C.dark);
  outlinedText(ctx, TEXT, rx + lockW + GAP, ty, font, { align: "left", lw: 0, fill: C.inkDim });

  // часы — та же плашка, что в HUD; drawClock ставит её в (safe.x + 3, safe.y + 3)
  const clockY = y0 + 64 + 26;
  ctx.globalAlpha = 0.8;
  drawClock(ctx, state, { x: Math.round(cx - cWidth / 2) - 3, y: clockY - 3, w: cWidth, h: CLOCK_H });
  ctx.restore();
  return { x: Math.round(cx - Math.max(rowW, cWidth) / 2), y: y0, w: Math.ceil(Math.max(rowW, cWidth)), h: blockH };
}
