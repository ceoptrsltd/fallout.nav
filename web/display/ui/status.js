// Индикаторы и крупные предупреждения: спутники — плашкой под компасом (drawSatsTag, её рисует compass.js),
// запись — плашкой справа от часов (drawRecTag); справа внизу над плашкой станций (ui/power.js) — строки
// «нет сети», охрана, аварийка (drawIndicators); слева — NO GPS, NO LINK (drawWarnings).

import { HAZARD_HZ, alarmIndicator, drawHazardSign, drawLock, hazardOn } from "./alarm.js";
import { POWER_H, powerVisible, powerWidth } from "./power.js";
import { C, blinkOn, blit, chamferPath, fontLabel, fontNum, fontStencil, hazardSprite, outlinedText, plateSprite, sprite, textWidth } from "./theme.js";

const ROW_H = 36;
const FONT = () => fontLabel(25);
const SATS_FONT = () => fontNum(27, 600); // «39/20» — узкие цифры, как на спидометре
const SATS_WIDEST = "88/88";

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

function rowWidth(ctx, text, iconW, font = FONT()) {
  return 14 + iconW + (iconW ? 8 : 0) + Math.ceil(textWidth(ctx, font, text)) + 14;
}

/** Строка GPS: спутники видимые / используемые («39/20»); есть ли фикс — по значку (залит / тусклый). */
export function satsText(gps) {
  const g = gps ?? {};
  const n = (v) => (Number.isFinite(v) ? v : 0);
  return `${n(g.sats_visible)}/${n(g.sats_used)}`;
}

/** Ширина колонки строк справа внизу без плашки станций (постоянная — плашка радио/погоды не «дышит»). */
export function indicatorsWidth(ctx) {
  return Math.max(rowWidth(ctx, SATS_WIDEST, 26, SATS_FONT()), rowWidth(ctx, "REC", 22));
}

/** Сколько занято справа внизу: плашка станций, а без неё — колонка строк. Левее — радио или погода. */
export function rightColumnWidth(ctx, state) {
  return powerVisible(state) ? powerWidth(ctx) : indicatorsWidth(ctx);
}

/** Высота строк индикаторов и зазор между ними. */
export const INDICATOR_ROW = { h: ROW_H, gap: 6 };

/** Светлая («инверсная») плашка строки — для мигания сработавшей охраны. */
function invPlate(w, h) {
  return sprite("row-inverse", w, h, (c) => {
    chamferPath(c, 1.5, 1.5, w - 3, h - 3, 6);
    c.fillStyle = C.hazard;
    c.fill();
    c.lineWidth = 3;
    c.strokeStyle = C.dark;
    c.stroke();
  });
}

/** Строка-индикатор справа: (xr — правый край, y — верх). Возвращает ширину. */
function row(ctx, xr, y, text, iconW, drawIcon, { dim = false, font = FONT(), minW = 0, invert = false } = {}) {
  const w = Math.max(minW, rowWidth(ctx, text, iconW, font));
  const x = Math.round(xr - w);
  blit(ctx, invert ? invPlate(w, ROW_H) : plateSprite(w, ROW_H, { chamfer: 6, rivets: false }), x, y);
  if (drawIcon) drawIcon(x + 14 + iconW / 2, y + ROW_H / 2);
  outlinedText(ctx, text, x + w - 14, y + ROW_H / 2, font, {
    align: "right",
    fill: invert ? C.dark : dim ? C.inkDim : C.ink,
    lw: invert ? 0 : 4,
  });
  return w;
}

/**
 * Строки справа внизу — над плашкой станций (ui/power.js), а без неё — от низа зоны безопасности: «нет сети»,
 * охрана (ARM 45 / ARMED / ALARM), мигающий знак аварийки. Возвращает занятый прямоугольник.
 */
export function drawIndicators(ctx, state, safe) {
  const v = state?.view ?? {};
  const t = state?.telemetry ?? {};
  const now = state?.now ?? 0;
  const xr = safe.x + safe.w;
  const bottom = safe.y + safe.h - (powerVisible(state) ? POWER_H + 6 : 0);
  let y = bottom - ROW_H;

  // интернет: показываем только когда точно нет (null = неизвестно — молчим)
  if (v.internet === false) {
    row(ctx, xr, y, "NO NET", 24, (x, yy) => iconAntennaOff(ctx, x, yy));
    y -= ROW_H + 6;
  }

  // охрана (alarm.js): ARM 45 / ARMED / ALARM — сработала: мигает инверсией (светлая плашка, тёмные буквы)
  const al = alarmIndicator(t.alarm);
  if (al) {
    const inv = al.kind === "triggered" && blinkOn(now, 2, 0.5);
    row(ctx, xr, y, al.text, 22, (x, yy) => drawLock(ctx, x, yy, 22, false, inv ? C.dark : C.ink, inv ? C.hazard : C.shadow), {
      invert: inv,
      minW: al.kind === "arming" ? rowWidth(ctx, "ARM 88", 22) : 0, // отсчёт не «дышит» шириной
    });
    y -= ROW_H + 6;
  }
  // аварийка мигает (реле на её кнопке): мигающий знак над строкой охраны, у правого края колонки
  if (hazardOn(t.alarm)) {
    drawHazardSign(ctx, xr - 20, y + ROW_H / 2, 28, blinkOn(now, HAZARD_HZ, 0.5));
    y -= ROW_H + 6;
  }
  const top = y + ROW_H + 6;
  return { x: xr - 200, y: top, w: 200, h: bottom - top };
}

// ── спутники: плашка под компасом (на месте прежней метки источника курса) ──

const SATS_H = 32;

/** Ширина плашки спутников (постоянная, по «88/88»). */
export function satsTagWidth(ctx) {
  return Math.ceil(12 + 24 + 6 + textWidth(ctx, SATS_FONT(), SATS_WIDEST) + 12);
}

/**
 * Плашка спутников с центром (cx, cy): видимые / используемые («34/20»); без фикса — тусклые цифры и пустые панели
 * значка; тоннель (счисление пути, view.deadReckoning) — DR. Возвращает ширину.
 */
export function drawSatsTag(ctx, cx, cy, state) {
  const v = state?.view ?? {};
  const gps = state?.telemetry?.gps ?? {};
  const hasFix = v.hasFix !== false && gps.fix !== "none";
  const dr = !hasFix && v.deadReckoning === true;
  const w = satsTagWidth(ctx);
  const h = SATS_H;
  const x0 = Math.round(cx - w / 2);
  const y0 = Math.round(cy - h / 2);
  chamferPath(ctx, x0, y0, w, h, 6);
  ctx.fillStyle = C.plateSolid;
  ctx.fill();
  ctx.lineWidth = 3;
  ctx.strokeStyle = C.dark;
  ctx.stroke();
  chamferPath(ctx, x0 + 2.5, y0 + 2.5, w - 5, h - 5, 5);
  ctx.lineWidth = 2;
  ctx.strokeStyle = C.ink;
  ctx.stroke();
  iconSat(ctx, x0 + 24, cy, hasFix);
  outlinedText(ctx, dr ? "DR" : satsText(gps), x0 + w - 12, cy, dr ? FONT() : SATS_FONT(), {
    align: "right",
    lw: 4,
    fill: hasFix || dr ? C.ink : C.inkDim,
  });
  return w;
}

// ── запись: плашка справа от часов ──

const REC_H = 62; // как часы (ui/clock.js CLOCK_H)
const REC_NUM = () => fontNum(40, 700);
const REC_W = 16 + 38 + 10 + 30 + 6 + 30 + 14;

/** Габарит плашки записи. */
export const REC_BOX = { w: REC_W, h: REC_H };

/**
 * Состояние камеры для плашки записи: "rec" — пишет; "idle" — работает, но не пишет (запускается, запись
 * выключена); "absent" — камеры нет (выключена, остановлена, ошибка, нет данных).
 */
export function camState(cam) {
  if (!cam) return "absent";
  if (cam.recording) return "rec";
  return cam.state === "starting" || cam.state === "live" ? "idle" : "absent";
}

/** Значок видеокамеры: "on" — пишет (красный корпус), "blink" — фаза мигания (контур), "off" — перечёркнута. */
function cameraIcon(ctx, x, y, mode) {
  const bh = 19;
  ctx.save();
  ctx.lineJoin = "round";
  const body = () => {
    ctx.beginPath();
    ctx.roundRect(x - 17, y - bh / 2, 26, bh, 3);
  };
  const lens = () => {
    ctx.beginPath();
    ctx.moveTo(x + 11, y - 3);
    ctx.lineTo(x + 19, y - 8);
    ctx.lineTo(x + 19, y + 8);
    ctx.lineTo(x + 11, y + 3);
    ctx.closePath();
  };
  for (const part of [body, lens]) {
    part();
    ctx.lineWidth = 5;
    ctx.strokeStyle = C.shadow;
    ctx.stroke();
    if (mode === "on") {
      ctx.fillStyle = part === body ? C.rec : C.ink;
      ctx.fill();
    }
    ctx.lineWidth = 2.5;
    ctx.strokeStyle = mode === "on" ? C.ink : C.inkDim;
    ctx.stroke();
  }
  if (mode === "off") {
    ctx.beginPath();
    ctx.moveTo(x - 18, y + 13);
    ctx.lineTo(x + 18, y - 13);
    ctx.lineWidth = 5;
    ctx.strokeStyle = C.shadow;
    ctx.stroke();
    ctx.lineWidth = 2.5;
    ctx.strokeStyle = C.inkDim;
    ctx.stroke();
  }
  ctx.restore();
}

function camNumber(ctx, n, cx, cy, st) {
  outlinedText(ctx, String(n), cx, cy, REC_NUM(), { lw: 5, fill: st === "rec" ? C.ink : C.inkDim });
  if (st === "absent") {
    ctx.save();
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(cx - 12, cy + 16);
    ctx.lineTo(cx + 12, cy - 16);
    ctx.lineWidth = 7;
    ctx.strokeStyle = C.shadow;
    ctx.stroke();
    ctx.lineWidth = 3;
    ctx.strokeStyle = C.ink;
    ctx.stroke();
    ctx.restore();
  }
}

/**
 * Плашка записи с левым верхним углом (x, y): значок камеры (пишет хоть одна — мигает; ни одна — перечёркнут) и
 * номера камер: 1 — передняя (telemetry.camera), 2 — задняя (telemetry.camera_rear); пишет — ярко, работает без
 * записи — тускло, камеры нет — перечёркнут. Возвращает прямоугольник.
 */
export function drawRecTag(ctx, state, x, y) {
  const t = state?.telemetry ?? {};
  const now = state?.now ?? 0;
  const cams = [camState(t.camera), camState(t.camera_rear)];
  blit(ctx, plateSprite(REC_W, REC_H, { chamfer: 8, rivets: false }), x, y);
  const cy = y + REC_H / 2;
  const any = cams.includes("rec");
  cameraIcon(ctx, x + 34, cy, !any ? "off" : blinkOn(now, 1, 0.6) ? "on" : "blink");
  ctx.fillStyle = C.metal;
  ctx.fillRect(x + 58, y + 11, 2, REC_H - 22);
  camNumber(ctx, 1, x + 79, cy, cams[0]);
  camNumber(ctx, 2, x + 115, cy, cams[1]);
  return { x, y, w: REC_W, h: REC_H };
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

/** Крупные предупреждения — колонкой слева, начиная с yTop. В тоннеле (счисление пути) NO GPS не мигает: DR в строке GPS. */
export function drawWarnings(ctx, state, safe, yTop) {
  const v = state?.view ?? {};
  const now = state?.now ?? 0;
  const list = [];
  if (v.connected === false) list.push("NO LINK");
  if (v.hasFix === false && v.deadReckoning !== true) list.push("NO GPS");
  let y = yTop;
  for (const text of list) {
    warning(ctx, Math.round(safe.x + 3), Math.round(y), text, now);
    y += WARN.h + 10;
  }
  return y;
}
