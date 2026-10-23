// Часы: ЧЧ:ММ крупно, справа — день недели и дата. Компактная плашка в левом верхнем углу.
//
// Время — местное время машины: telemetry.system.utc_offset_min (часовой пояс fnav-core), без него —
// часы браузера (на устройстве это одно и то же; удалённо так не показываются часы зрителя).
// Платы без RTC: пока системное время не синхронизировано (ни NTP, ни GPS) — цифры тусклые.

import { C, blit, fixedDigits, fontLabel, fontNum, outlinedText, plateSprite, spriteGeneration, textWidth } from "./theme.js";

const DAYS = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"];
const MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];

export const CLOCK_H = 62;
const PAD = 12;
const SEP = 10; // поля вокруг вертикальной черты между временем и датой
const COLON_W = 14;
const fTime = () => fontNum(52, 700);
const fDate = () => fontLabel(25);

const pad2 = (n) => String(n).padStart(2, "0");

/** Части даты для показа: { hh, mm, day, date, synced }; ms — время (по умолчанию view.wallMs или сейчас). */
export function clockParts(state, ms = state?.view?.wallMs ?? Date.now()) {
  const sys = state?.telemetry?.system ?? {};
  const off = sys.utc_offset_min;
  const utc = Number.isFinite(off);
  const d = new Date(utc ? ms + off * 60000 : ms);
  const [h, m, wd, dd, mo] = utc
    ? [d.getUTCHours(), d.getUTCMinutes(), d.getUTCDay(), d.getUTCDate(), d.getUTCMonth()]
    : [d.getHours(), d.getMinutes(), d.getDay(), d.getDate(), d.getMonth()];
  return { hh: pad2(h), mm: pad2(m), day: DAYS[wd], date: `${dd} ${MONTHS[mo]}`, synced: sys.time_synced !== false };
}

// размеры — по самым широким значениям, чтобы плашка не «дышала» от дня к дню
let metrics = null;
function measure(ctx) {
  if (metrics && metrics.gen === spriteGeneration()) return metrics;
  const cell = Math.round(textWidth(ctx, fTime(), "0") + 2);
  const timeW = 4 * cell + COLON_W;
  let dateW = 0;
  for (const s of [...DAYS, ...MONTHS.map((mo) => `28 ${mo}`)]) dateW = Math.max(dateW, textWidth(ctx, fDate(), s));
  dateW = Math.ceil(dateW);
  metrics = { gen: spriteGeneration(), cell, timeW, dateW, w: PAD + timeW + SEP + 2 + SEP + dateW + PAD };
  return metrics;
}

/** Ширина плашки часов (постоянная). */
export function clockWidth(ctx) {
  return measure(ctx).w;
}

const overlaps = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

/**
 * Часы в левом верхнем углу зоны безопасности; если там компас (вариант v2 по центру) — под ним.
 * avoid — прямоугольник компаса или null. Возвращает занятый прямоугольник.
 */
export function drawClock(ctx, state, safe, avoid = null) {
  const { cell, timeW, w } = measure(ctx);
  const h = CLOCK_H;
  const x = Math.round(safe.x + 3);
  let y = Math.round(safe.y + 3);
  if (avoid && overlaps({ x, y, w, h }, avoid)) y = Math.round(avoid.y + avoid.h + 8);

  const p = clockParts(state);
  const ink = p.synced ? C.ink : C.inkDim;
  blit(ctx, plateSprite(w, h, { chamfer: 8, rivets: false }), x, y);

  // ЧЧ:ММ в фиксированных ячейках; двоеточие — два квадрата, как точка на спидометре
  const cy = y + h / 2;
  const xh = x + PAD + 2 * cell;
  fixedDigits(ctx, p.hh, xh, cy, fTime(), cell, { lw: 6, fill: ink });
  fixedDigits(ctx, p.mm, xh + COLON_W + 2 * cell, cy, fTime(), cell, { lw: 6, fill: ink });
  ctx.fillStyle = ink;
  for (const dy of [-10, 5]) ctx.fillRect(xh + COLON_W / 2 - 3, cy + dy, 6, 6);

  // черта и столбик «день / дата»
  const xs = x + PAD + timeW + SEP;
  ctx.fillStyle = C.metal;
  ctx.fillRect(xs, y + 11, 2, h - 22);
  const xd = xs + 2 + SEP;
  outlinedText(ctx, p.day, xd, y + 19, fDate(), { align: "left", lw: 4, fill: p.synced ? C.accent : C.inkDim });
  outlinedText(ctx, p.date, xd, y + h - 19, fDate(), { align: "left", lw: 4, fill: ink });
  return { x, y, w, h };
}
