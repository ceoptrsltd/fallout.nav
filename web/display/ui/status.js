// Индикаторы (GPS, REC, сеть) и крупные предупреждения (NO GPS, NO LINK).

import { C, blinkOn, blit, chamferPath, fontLabel, fontStencil, hazardSprite, outlinedText, plateSprite, textWidth } from "./theme.js";

const ROW_H = 36;
const FONT = () => fontLabel(25);

// ── пиктограммы (рисуются линиями ≥ 2 px, со светлой заливкой) ──────────

function iconSat(ctx, x, y, on) {
  // спутник: корпус + две панели + антенна
  ctx.save();
  ctx.translate(x, y);
  ctx.rotate(-0.6);
  ctx.lineWidth = 2;
  ctx.strokeStyle = C.ink;
  ctx.fillStyle = on ? C.ink : "transparent";
  ctx.beginPath();
  ctx.rect(-4, -4, 8, 8);
  ctx.fillStyle = C.ink;
  ctx.fill();
  for (const s of [-1, 1]) {
    ctx.beginPath();
    ctx.rect(s > 0 ? 6 : -13, -3.5, 7, 7);
    if (on) ctx.fill();
    ctx.stroke();
  }
  ctx.beginPath();
  ctx.moveTo(0, 4);
  ctx.lineTo(0, 9);
  ctx.stroke();
  ctx.restore();
}

function iconAntennaOff(ctx, x, y) {
  ctx.save();
  ctx.lineWidth = 2.5;
  ctx.strokeStyle = C.ink;
  ctx.lineCap = "round";
  ctx.beginPath();
  ctx.moveTo(x, y - 10);
  ctx.lineTo(x, y + 10);
  ctx.moveTo(x - 6, y + 10);
  ctx.lineTo(x + 6, y + 10);
  ctx.stroke();
  for (const r of [6, 11]) {
    ctx.beginPath();
    ctx.arc(x, y - 6, r, -Math.PI * 0.8, -Math.PI * 0.2);
    ctx.stroke();
  }
  // перечёркнуто
  ctx.lineWidth = 3.5;
  ctx.strokeStyle = C.dark;
  ctx.beginPath();
  ctx.moveTo(x - 11, y + 9);
  ctx.lineTo(x + 11, y - 11);
  ctx.stroke();
  ctx.lineWidth = 2.5;
  ctx.strokeStyle = C.ink;
  ctx.stroke();
  ctx.restore();
}

function rowWidth(ctx, text, iconW) {
  return 14 + iconW + (iconW ? 8 : 0) + Math.ceil(textWidth(ctx, FONT(), text)) + 14;
}

/**
 * Ширина двух нижних строк индикаторов (GPS, REC) с запасом на самые широкие значения — постоянная,
 * чтобы соседние элементы (плашка радио) не «дышали» при смене числа спутников.
 */
export function indicatorsWidth(ctx) {
  return Math.max(rowWidth(ctx, "3D 88", 26), rowWidth(ctx, "— 88", 26), rowWidth(ctx, "REC", 22));
}

/** Высота строк индикаторов и зазор между ними. */
export const INDICATOR_ROW = { h: ROW_H, gap: 6 };

/** Строка-индикатор справа: (xr — правый край, y — верх). Возвращает ширину. */
function row(ctx, xr, y, text, iconW, drawIcon, { dim = false } = {}) {
  const font = FONT();
  const w = rowWidth(ctx, text, iconW);
  const x = Math.round(xr - w);
  blit(ctx, plateSprite(w, ROW_H, { chamfer: 6, rivets: false }), x, y);
  if (drawIcon) drawIcon(x + 14 + iconW / 2, y + ROW_H / 2);
  outlinedText(ctx, text, x + w - 14, y + ROW_H / 2, font, {
    align: "right",
    fill: dim ? C.inkDim : C.ink,
    lw: 4,
  });
  return w;
}

/** Индикаторы в правом нижнем углу зоны безопасности. */
export function drawIndicators(ctx, state, safe) {
  const v = state?.view ?? {};
  const t = state?.telemetry ?? {};
  const now = state?.now ?? 0;
  const xr = safe.x + safe.w;
  let y = safe.y + safe.h - ROW_H;

  // GPS: тип фикса + число спутников
  const gps = t.gps ?? {};
  const fix = gps.fix === "3d" ? "3D" : gps.fix === "2d" ? "2D" : "—";
  const sats = Number.isFinite(gps.sats_used) ? gps.sats_used : 0;
  const hasFix = v.hasFix !== false && gps.fix !== "none";
  row(ctx, xr, y, `${fix} ${String(sats).padStart(2, " ")}`, 26, (x, yy) => iconSat(ctx, x, yy, hasFix), {
    dim: !hasFix,
  });
  y -= ROW_H + 6;

  // REC: мигающая заполненная точка; без записи — пустой перечёркнутый круг
  const rec = !!v.recording;
  row(ctx, xr, y, "REC", 22, (x, yy) => {
    ctx.beginPath();
    ctx.arc(x, yy, 9, 0, 2 * Math.PI);
    if (rec) {
      if (blinkOn(now, 1, 0.6)) {
        ctx.fillStyle = C.rec;
        ctx.fill();
        ctx.lineWidth = 2.5;
        ctx.strokeStyle = C.ink;
        ctx.stroke();
      } else {
        ctx.lineWidth = 2.5;
        ctx.strokeStyle = C.inkDim;
        ctx.stroke();
      }
    } else {
      ctx.lineWidth = 2.5;
      ctx.strokeStyle = C.inkDim;
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(x - 8, yy + 8);
      ctx.lineTo(x + 8, yy - 8);
      ctx.stroke();
    }
  }, { dim: !rec });
  y -= ROW_H + 6;

  // интернет: показываем только когда точно нет (null = неизвестно — молчим)
  if (v.internet === false) {
    row(ctx, xr, y, "NO NET", 24, (x, yy) => iconAntennaOff(ctx, x, yy));
    y -= ROW_H + 6;
  }
  return { x: xr - 200, y, w: 200, h: safe.y + safe.h - y };
}

const WARN = { w: 214, h: 44 };

function warning(ctx, x, y, text, now) {
  blit(ctx, hazardSprite(WARN.w, WARN.h), x, y);
  outlinedText(ctx, text, x + WARN.w / 2, y + WARN.h / 2, fontStencil(27), { fill: C.dark, lw: 0 });
  // мигающая рамка — привлекает внимание, но текст всегда виден
  if (blinkOn(now, 1.25, 0.5)) {
    chamferPath(ctx, x - 3, y - 3, WARN.w + 6, WARN.h + 6, 9);
    ctx.lineWidth = 3;
    ctx.strokeStyle = C.ink;
    ctx.stroke();
  }
}

/** Крупные предупреждения — колонкой слева, начиная с yTop. */
export function drawWarnings(ctx, state, safe, yTop) {
  const v = state?.view ?? {};
  const now = state?.now ?? 0;
  const list = [];
  if (v.connected === false) list.push("NO LINK");
  if (v.hasFix === false) list.push("NO GPS");
  let y = yTop;
  for (const text of list) {
    warning(ctx, Math.round(safe.x + 3), Math.round(y), text, now);
    y += WARN.h + 10;
  }
  return y;
}
