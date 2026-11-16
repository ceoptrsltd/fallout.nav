// Электростанции — плашка справа внизу (docs/contracts/power.md, docs/contracts/display.md).
// Данные — state.telemetry.power = {main, second, third: {soc_pct, voltage_v, charging, low, …}}; плашку выключает
// настройка power.show_on_screen.
// Слева крупно заряд главной станции, %; заряжается — процент мельче, под ним молния и мощность зарядки «412W»; справа две колонки — вторая (Powerbank) и третья (свинцовый аккумулятор):
// сверху заряд, %, под ним напряжение; черта — посередине. Третья заряжается от солнца — третьей строкой молния и ватты. Нет данных — прочерки; заряд ≤ 20 % (low) — мигает;
// Powerbank заряжается — третья строка по очереди: молния с ваттами (WATTS_SHOW_S) и разница ячеек; опасная разница — только она.
// у третьей процент «примерно» (soc_approx: не из покоя сейчас — держится с покоя до зарядки или оценка) — тусклый;
// заряжается — анимация: у главной пульсирует молния под знаком %, в колонке между % и напряжением бегут сегменты
// заряда (как на телефоне). У Powerbank мелко третьей строкой — разница ячеек «Δ60mV» (опасная — мигает).
// Раз в несколько секунд по рамке плашки пробегает «треск» молнии (ui/lightning.js).
// Ширина постоянная (по самым широким значениям) — соседи не «дышат».

import { drawPlateLightning } from "./lightning.js";
import { C, blinkOn, blit, fontNum, outlinedText, plateSprite, setting, spriteGeneration, textWidth } from "./theme.js";

export const POWER_H = 84; // как плашка радио/погоды (ui/radio.js RADIO_H): две строки индикаторов + поля
const PAD = 16; // поля слева и справа — одинаковые
const BIG = () => fontNum(44, 700);
const BIG_3 = () => fontNum(38, 700); // «100»
const PCT = () => fontNum(24, 600);
const BIG_CHG = () => fontNum(34, 700); // главная заряжается: процент мельче, под ним — мощность
const BIG_CHG_3 = () => fontNum(30, 700);
const PCT_CHG = () => fontNum(19, 600);
const WATTS = () => fontNum(17, 500);
const CF = () => fontNum(26, 600); // % в колонках
const CF_3 = () => fontNum(22, 600); // «100%»
const CV = () => fontNum(19, 500); // напряжение под ним
const CD = () => fontNum(16, 500); // Powerbank: разница ячеек, ещё ниже
const COL_GAP = 14;
const SEP = 2;
const SEP_GAP = 12; // от черты до главной станции и до колонок, не меньше
// строки колонки (середины): %, напряжение, третья (ватты / разница ячеек / полоса зарядки) — с равными просветами
const ROW_PCT = 24;
const ROW_VOLT = 48;
const ROW_3 = 68;

/** Плашка на экране: есть telemetry.power и не выключена в настройках (power.show_on_screen). */
export function powerVisible(state) {
  return !!state?.telemetry?.power && setting(state, "power.show_on_screen", true) !== false;
}

/** Целая часть, как на самой станции: «83» из 83.5 (не 84); нет данных — «--». */
export function socText(slot) {
  const v = slot?.soc_pct;
  return Number.isFinite(v) ? String(Math.max(0, Math.min(100, Math.floor(v)))) : "--";
}

/** Разница ячеек «Δ60mV»; нет — "". */
export function deltaText(slot) {
  const d = slot?.cell_delta_mv;
  return Number.isFinite(d) ? `Δ${Math.round(d)}mV` : "";
}

/** Мощность зарядки «412W»; нет данных — «--W». */
export function wattText(slot) {
  const w = slot?.input_w;
  return Number.isFinite(w) ? `${Math.max(0, Math.round(w))}W` : "--W";
}

/** «20.8V»; нет данных — «--.-V». */
export function voltText(slot) {
  const v = slot?.voltage_v;
  return Number.isFinite(v) ? `${v.toFixed(1)}V` : "--.-V";
}

let metrics = null;
function measure(ctx) {
  if (metrics && metrics.gen === spriteGeneration()) return metrics; // шрифты загрузились — заново
  // главная: самое широкое — «88» крупно или «100» шрифтом поуже, плюс знак %
  const bigW = Math.ceil(Math.max(textWidth(ctx, BIG(), "88"), textWidth(ctx, BIG_3(), "100")) + 1 + textWidth(ctx, PCT(), "%"));
  const colW = Math.ceil(Math.max(textWidth(ctx, CF(), "88%"), textWidth(ctx, CF_3(), "100%"), textWidth(ctx, CV(), "88.8V"),
    textWidth(ctx, CD(), "Δ88mV"), 11 + textWidth(ctx, CD(), "888W")));
  // черта посередине между главной и колонками: по SEP_GAP с каждой стороны при самых широких значениях
  metrics = { gen: spriteGeneration(), bigW, colW, w: PAD + bigW + SEP_GAP + SEP + SEP_GAP + 2 * colW + COL_GAP + PAD };
  return metrics;
}

/** Ширина плашки (постоянная). */
export function powerWidth(ctx) {
  return measure(ctx).w;
}

function bolt(ctx, cx, cy, k = 1) {
  ctx.save();
  ctx.translate(cx, cy);
  ctx.scale(k, k);
  ctx.beginPath();
  ctx.moveTo(3, -10);
  ctx.lineTo(-5, 2);
  ctx.lineTo(0, 2);
  ctx.lineTo(-3, 10);
  ctx.lineTo(5, -2);
  ctx.lineTo(0, -2);
  ctx.closePath();
  ctx.lineJoin = "round";
  ctx.lineWidth = 4;
  ctx.strokeStyle = C.dark;
  ctx.stroke();
  ctx.fillStyle = C.accent;
  ctx.fill();
  ctx.restore();
}

export const WATTS_SHOW_S = 4; // Powerbank заряжается: столько секунд ватты ...
export const DELTA_SHOW_S = 2; // ... и столько — разница ячеек

/** Что в третьей строке колонки сейчас: "watts" (молния и мощность зарядки), "delta" (разница ячеек) или "". */
export function thirdLine(slot, now) {
  const watts = !!slot?.charging && Number.isFinite(slot.input_w) && slot.input_w > 0;
  const delta = deltaText(slot) !== "";
  if (watts && delta) return slot.cell_delta_bad || now % (WATTS_SHOW_S + DELTA_SHOW_S) >= WATTS_SHOW_S ? "delta" : "watts";
  return watts ? "watts" : delta ? "delta" : "";
}

export const CHARGE_SEGS = 4;
export const CHARGE_STEP_S = 0.45;

/** Сколько сегментов полосы заряда горит сейчас: 0…CHARGE_SEGS по кругу (заполняются по одному, потом снова). */
export function chargeLit(now) {
  return Math.floor(now / CHARGE_STEP_S) % (CHARGE_SEGS + 1);
}

/** Полоса заряда: CHARGE_SEGS сегментов, горящие — ярко, остальные — контур. */
function chargeBar(ctx, x, y, w, now) {
  const lit = chargeLit(now);
  const gap = 3;
  const sw = (w - gap * (CHARGE_SEGS - 1)) / CHARGE_SEGS;
  ctx.save();
  for (let i = 0; i < CHARGE_SEGS; i++) {
    const sx = Math.round(x + i * (sw + gap));
    ctx.fillStyle = C.shadow;
    ctx.fillRect(sx - 1, y - 1, Math.round(sw) + 2, 7);
    ctx.fillStyle = i < lit ? C.ink : C.inkFaint;
    ctx.fillRect(sx, y, Math.round(sw), 5);
  }
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

  // главная: крупный %; заряжается — мельче, под ним молния и мощность зарядки
  const main = p.main;
  const pct = socText(main);
  const mainInk = ink(main, pct !== "--");
  const chg = !!main?.charging;
  const [f1, f3, fp, yc] = chg ? [BIG_CHG(), BIG_CHG_3(), PCT_CHG(), cy - 9] : [BIG(), BIG_3(), PCT(), cy + 2];
  const big = pct.length > 2 ? f3 : f1;
  const xn = x + PAD;
  outlinedText(ctx, pct, xn, yc, big, { align: "left", lw: 6, fill: mainInk });
  const xp = xn + textWidth(ctx, big, pct) + 1;
  outlinedText(ctx, "%", xp, yc - (chg ? 8 : 13), fp, { align: "left", lw: 4, fill: mainInk });
  let leftEnd = xp + textWidth(ctx, fp, "%");
  if (chg) {
    const yw = y + POWER_H - 17;
    if (blinkOn(now, 1, 0.65)) bolt(ctx, xn + 5, yw, 0.75);
    const wt = wattText(main);
    outlinedText(ctx, wt, xn + 13, yw, WATTS(), { align: "left", lw: 4, fill: wt === "--W" ? C.inkDim : C.ink });
    leftEnd = Math.max(leftEnd, xn + 13 + textWidth(ctx, WATTS(), wt));
  }

  // справа: две колонки (вторая, третья), прижаты к правому краю; в колонке по центру: %, под ним напряжение
  const rightStart = x + m.w - PAD - 2 * m.colW - COL_GAP;
  ctx.fillStyle = C.metal;
  ctx.fillRect(Math.round(Math.max(leftEnd + 6, rightStart - SEP_GAP - SEP)), y + 12, SEP, POWER_H - 24); // черта стоит на месте
  [p.second, p.third].forEach((slot, k) => {
    const cx = rightStart + k * (m.colW + COL_GAP) + m.colW / 2;
    const s = socText(slot);
    const known = s !== "--";
    const fill = slot?.soc_approx && known ? C.inkDim : ink(slot, known); // свинцовый «примерно» — тусклый
    const t = `${s}%`;
    const f = textWidth(ctx, CF(), t) > m.colW + 2 ? CF_3() : CF();
    outlinedText(ctx, t, cx, y + ROW_PCT, f, { lw: 4, fill });
    outlinedText(ctx, voltText(slot), cx, y + ROW_VOLT, CV(), { lw: 4, fill: Number.isFinite(slot?.voltage_v) ? C.ink : C.inkDim });
    // заряжается: известна мощность (свинцовый — от солнечного контроллера) — мигающая молния и ватты третьей
    // строкой, как у главной; нет — бегущие сегменты между % и напряжением
    const line = thirdLine(slot, now);
    if (line === "watts") {
      const wt = wattText(slot);
      const ww = textWidth(ctx, CD(), wt);
      const x0 = Math.round(cx - (ww + 11) / 2);
      if (blinkOn(now, 1, 0.65)) bolt(ctx, x0 + 4, y + ROW_3, 0.62);
      outlinedText(ctx, wt, x0 + 11, y + ROW_3, CD(), { align: "left", lw: 3, fill: C.ink });
    } else if (slot?.charging && (slot.cell_delta_bad || !(Number.isFinite(slot.input_w) && slot.input_w > 0))) {
      // полоса зарядки — третьей строкой; она занята разницей ячеек — между % и напряжением
      chargeBar(ctx, cx - m.colW / 2 + 2, y + (line === "" ? ROW_3 - 3 : ROW_PCT + 11), m.colW - 4, now);
    }
    const d = deltaText(slot);
    if (line === "delta" && !(slot.cell_delta_bad && !blinkOn(now, 2, 0.6))) {
      outlinedText(ctx, d, cx, y + ROW_3, CD(), { lw: 3, fill: slot.cell_delta_bad ? C.ink : C.inkDim });
    }
  });
  const rect = { x, y, w: m.w, h: POWER_H };
  drawPlateLightning(ctx, rect, now, { chamfer: 9, bounds: safe });
  return rect;
}
