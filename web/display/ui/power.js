// Электростанции — плашка справа внизу (docs/contracts/power.md, docs/contracts/display.md).
// Данные — state.telemetry.power = {main, second, third: {soc_pct, voltage_v, charging, low, …}}; плашку выключает
// настройка power.show_on_screen.
// Слева крупно заряд главной станции, %; справа две строки — напряжения второй и третьей, выровнены по десятичной
// точке; черта — посередине между ними. Нет данных — прочерки; заряд ≤ 20 % (low) — значение мигает; главная
// заряжается — молния под знаком %. Ширина постоянная (по самым широким значениям) — соседи не «дышат».

import { C, blinkOn, blit, fontNum, outlinedText, plateSprite, setting, spriteGeneration, textWidth } from "./theme.js";

export const POWER_H = 84; // как плашка радио/погоды (ui/radio.js RADIO_H): две строки индикаторов + поля
const PAD = 16;
const BIG = () => fontNum(46, 700);
const BIG_3 = () => fontNum(38, 700); // «100»
const PCT = () => fontNum(24, 600);
const VF = () => fontNum(27, 600);
const VU = () => fontNum(20, 600);
const PCT_COL = 16; // место под «%» и молнию
const SEP = 2;

/** Плашка на экране: есть telemetry.power и не выключена в настройках (power.show_on_screen). */
export function powerVisible(state) {
  return !!state?.telemetry?.power && setting(state, "power.show_on_screen", true) !== false;
}

/** «91» (из 91.3), нет данных — «--». */
export function socText(slot) {
  const v = slot?.soc_pct;
  return Number.isFinite(v) ? String(Math.max(0, Math.min(100, Math.round(v)))) : "--";
}

/** «20.8» → ["20", ".8"]; нет данных — ["--", ".-"]. */
export function voltParts(slot) {
  const v = slot?.voltage_v;
  if (!Number.isFinite(v)) return ["--", ".-"];
  const [i, f] = v.toFixed(1).split(".");
  return [i, "." + f];
}

let metrics = null;
function measure(ctx) {
  if (metrics && metrics.gen === spriteGeneration()) return metrics; // шрифты загрузились — заново
  const bigW = Math.ceil(textWidth(ctx, BIG(), "88"));
  const intW = Math.ceil(textWidth(ctx, VF(), "88"));
  const fracW = Math.ceil(textWidth(ctx, VF(), ".8") + 1 + textWidth(ctx, VU(), "V"));
  // черта посередине между «88%» и вольтами: по 10 px с каждой стороны при самых широких значениях
  metrics = { gen: spriteGeneration(), bigW, intW, fracW, w: PAD + bigW + 2 + PCT_COL + 10 + SEP + 10 + intW + fracW + PAD };
  return metrics;
}

/** Ширина плашки (постоянная). */
export function powerWidth(ctx) {
  return measure(ctx).w;
}

function bolt(ctx, cx, cy) {
  ctx.save();
  ctx.beginPath();
  ctx.moveTo(cx + 3, cy - 10);
  ctx.lineTo(cx - 5, cy + 2);
  ctx.lineTo(cx, cy + 2);
  ctx.lineTo(cx - 3, cy + 10);
  ctx.lineTo(cx + 5, cy - 2);
  ctx.lineTo(cx, cy - 2);
  ctx.closePath();
  ctx.lineJoin = "round";
  ctx.lineWidth = 4;
  ctx.strokeStyle = C.dark;
  ctx.stroke();
  ctx.fillStyle = C.accent;
  ctx.fill();
  ctx.restore();
}

/** Плашка в правом нижнем углу зоны безопасности. Возвращает прямоугольник или null (выключена). */
export function drawPower(ctx, state, safe) {
  if (!powerVisible(state)) return null;
  const p = state.telemetry.power;
  const now = state?.now ?? 0;
  const m = measure(ctx);
  const x = Math.round(safe.x + safe.w - m.w);
  const y = Math.round(safe.y + safe.h - POWER_H);
  blit(ctx, plateSprite(m.w, POWER_H, { chamfer: 9 }), x, y);
  const cy = y + POWER_H / 2;
  const ink = (slot, known) => (!known ? C.inkDim : slot?.low && !blinkOn(now, 1, 0.6) ? C.inkDim : C.ink);

  // главная: крупный %
  const main = p.main;
  const pct = socText(main);
  const mainInk = ink(main, pct !== "--");
  const big = textWidth(ctx, BIG(), pct) > m.bigW + 2 ? BIG_3() : BIG();
  const xn = x + PAD;
  outlinedText(ctx, pct, xn, cy + 2, big, { align: "left", lw: 6, fill: mainInk });
  const xp = xn + textWidth(ctx, big, pct) + 1;
  outlinedText(ctx, "%", xp, cy - 11, PCT(), { align: "left", lw: 4, fill: mainInk });
  if (main?.charging) bolt(ctx, xp + 8, cy + 15);
  const leftEnd = xp + textWidth(ctx, PCT(), "%");

  // справа: вольты, по десятичной точке; блок прижат к правому краю
  const xr = x + m.w - PAD;
  const xPoint = xr - m.fracW;
  const rightStart = xPoint - m.intW;
  ctx.fillStyle = C.metal;
  ctx.fillRect(Math.round((leftEnd + rightStart) / 2 - SEP / 2), y + 12, SEP, POWER_H - 24);
  [p.second, p.third].forEach((slot, k) => {
    const ry = y + 24 + k * 36;
    const [i, f] = voltParts(slot);
    const fill = ink(slot, i !== "--");
    outlinedText(ctx, i, xPoint, ry, VF(), { align: "right", lw: 4, fill });
    outlinedText(ctx, f, xPoint, ry, VF(), { align: "left", lw: 4, fill });
    outlinedText(ctx, "V", xPoint + textWidth(ctx, VF(), f) + 1, ry + 2, VU(), { align: "left", lw: 4, fill });
  });
  return { x, y, w: m.w, h: POWER_H };
}
