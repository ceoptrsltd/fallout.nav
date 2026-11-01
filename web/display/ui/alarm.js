// Охрана на экране (docs/contracts/alarm.md §4, «Экран»). Данные — state.telemetry.alarm =
// {state, arm_in_s, level_g, sensor_ok, siren, seq, last: {kind, ts, title} | null} (null — сервиса нет).
//
// - Значок в стопке индикаторов справа внизу (рисует status.js): «ARM 45» — обратный отсчёт, «ARMED»,
//   «ALARM» — сработала (мигает инверсией).
// - Крупная мигающая плашка IMPACT / TILTED / MOVING / PERSON / CAMERA — 10 с после нового alarm.seq, пока состояние
//   «triggered» (события без тревоги — video_back, test — её не показывают); снятие с охраны убирает её сразу.
// - Вспышка ALARM ON / ALARM OFF на 2.5 с при переключении off ↔ не off (как вспышка режима радио): при играющем
//   радио — в строке трека плашки радио (новейшая из вспышек побеждает), иначе — своя плашка на месте радио
//   (radio.js, drawAlarmFlash).
//
// Состояние анимаций — своё у каждого холста (в галерее их несколько): updateAlarm(ctx, state) раз за кадр
// (drawHud, до drawRadio), дальше alarmFlash()/drawAlarmBanner() читают его.

import { C, blinkOn, blit, capHeight, chamferPath, fontLabel, fontStencil, sprite, textWidth } from "./theme.js";

export const BANNER_S = 10; // крупная надпись после нового события
export const FLASH_S = 2.5; // ALARM ON / ALARM OFF — как вспышка режима радио
const BANNER_WORD = { impact: "IMPACT", tilt: "TILTED", move: "MOVING", approach: "PERSON", face: "FACE", loiter: "PERSON",
  video_lost: "CAMERA" };
// события без тревоги: плашку не показывают (камера снова передаёт видео, проверка уведомлений)
const QUIET_KINDS = new Set(["video_back", "test"]);

// ── чистые помощники (tests/display/alarm.test.mjs) ───────────────────────

const isOn = (a) => !!a && typeof a.state === "string" && a.state !== "off";

/** Значок в стопке индикаторов: null (выключена / нет сервиса) или {text, kind}. */
export function alarmIndicator(alarm) {
  if (!isOn(alarm)) return null;
  if (alarm.state === "arming") {
    const n = Number.isFinite(alarm.arm_in_s) ? Math.max(0, Math.ceil(alarm.arm_in_s)) : null;
    return { text: n === null ? "ARM" : `ARM ${n}`, kind: "arming" };
  }
  if (alarm.state === "triggered") return { text: "ALARM", kind: "triggered" };
  return { text: "ARMED", kind: "armed" };
}

/** Аварийка мигает сейчас (реле на её кнопке, alarm.md «Аварийка»; telemetry.alarm.hazard). */
export const hazardOn = (alarm) => !!alarm && typeof alarm === "object" && alarm.hazard === true;

/** Частота мигания знака аварийки — как у настоящей (≈ 90 раз в минуту). */
export const HAZARD_HZ = 1.5;

/**
 * Знак аварийки (треугольник в треугольнике) с центром (x, y), высота ≈ s. lit — фаза «горит»: светлый
 * залитый знак с тёмным внутренним треугольником (на ч/б — самое яркое пятно); иначе — тусклый контур.
 */
export function drawHazardSign(ctx, x, y, s, lit) {
  const k = s / 20;
  const tri = (r) => {
    ctx.beginPath();
    ctx.moveTo(0, -r);
    ctx.lineTo(r * 0.866, r * 0.5);
    ctx.lineTo(-r * 0.866, r * 0.5);
    ctx.closePath();
  };
  ctx.save();
  ctx.translate(x, y + 1.5 * k);
  ctx.scale(k, k);
  ctx.lineJoin = "round";
  tri(13);
  ctx.lineWidth = 5;
  ctx.strokeStyle = C.shadow;
  ctx.stroke();
  if (lit) {
    ctx.fillStyle = C.hazard;
    ctx.fill();
    tri(5.5);
    ctx.lineWidth = 2.6;
    ctx.strokeStyle = C.dark;
    ctx.stroke();
  } else {
    ctx.lineWidth = 2.4;
    ctx.strokeStyle = C.inkDim;
    ctx.stroke();
    tri(5.5);
    ctx.lineWidth = 2;
    ctx.stroke();
  }
  ctx.restore();
}

/** Слово крупной плашки по виду последнего события. */
export function bannerWord(kind) {
  return BANNER_WORD[kind] ?? "ALARM";
}

/** Новое состояние отслеживания. */
export function newAlarmTrack() {
  return { seen: false, seq: null, on: false, seqT0: -1, flashT0: -1, flashOn: false };
}

/**
 * Учесть телеметрию охраны в момент now (секунды). Первое наблюдение — только запомнить seq и состояние
 * (не «новое» событие и не вспышка). Смена seq запоминает время (плашка — пока «triggered» и не прошло 10 с),
 * переключение off ↔ не off — вспышку. Снятие с охраны гасит плашку сразу. Возвращает тот же track.
 */
export function updateAlarmTrack(track, alarm, now) {
  if (!alarm || typeof alarm !== "object") return track; // сервиса нет — ничего не меняем
  const on = isOn(alarm);
  const seq = Number.isFinite(alarm.seq) ? alarm.seq : null;
  if (!track.seen) {
    track.seen = true;
    track.seq = seq;
    track.on = on;
    return track;
  }
  if (seq !== null && seq !== track.seq) {
    track.seqT0 = now;
    track.seq = seq;
  }
  if (on !== track.on) {
    track.flashT0 = now;
    track.flashOn = on;
    track.on = on;
  }
  if (!on) track.seqT0 = -1; // сняли с охраны — плашка не вернётся
  return track;
}

/** Крупная плашка сейчас: {word} или null. */
export function alarmBanner(track, alarm, now) {
  if (!alarm || alarm.state !== "triggered" || track.seqT0 < 0 || QUIET_KINDS.has(alarm.last?.kind)) return null;
  const dt = now - track.seqT0;
  if (dt < 0 || dt >= BANNER_S) return null;
  return { word: bannerWord(alarm.last?.kind), t0: track.seqT0 };
}

/** Вспышка ALARM ON / ALARM OFF сейчас: {on, t0} или null. */
export function alarmFlash(track, now) {
  if (!track || track.flashT0 < 0) return null;
  const dt = now - track.flashT0;
  return dt >= 0 && dt < FLASH_S ? { on: track.flashOn, t0: track.flashT0 } : null;
}

/**
 * Какую из вспышек показать: последнюю начавшуюся среди ещё идущих. items — [{t0, dur, ...}];
 * t0 < 0 — вспышки не было. Возвращает элемент или null.
 */
export function newestFlash(items, now) {
  let best = null;
  for (const it of items) {
    if (!it || !(it.t0 >= 0) || now - it.t0 < 0 || now - it.t0 >= it.dur) continue;
    if (!best || it.t0 > best.t0) best = it;
  }
  return best;
}

// ── отслеживание по холстам ───────────────────────────────────────────────

const tracks = new WeakMap();

/** Состояние охраны для этого холста (создаётся при первом обращении). */
export function alarmTracker(ctx) {
  const key = ctx?.canvas ?? ctx;
  let t = tracks.get(key);
  if (!t) {
    t = newAlarmTrack();
    tracks.set(key, t);
  }
  return t;
}

/** Раз за кадр (drawHud): учесть state.telemetry.alarm. */
export function updateAlarm(ctx, state) {
  return updateAlarmTrack(alarmTracker(ctx), state?.telemetry?.alarm ?? null, state?.now ?? 0);
}

/** Вспышка ALARM ON/OFF для этого холста (после updateAlarm в этом кадре). */
export function alarmFlashFor(ctx, now) {
  return alarmFlash(alarmTracker(ctx), now);
}

/** Для отладки/тестов: сбросить состояние холста. */
export function resetAlarm(ctx) {
  tracks.delete(ctx?.canvas ?? ctx);
}

// ── рисование ─────────────────────────────────────────────────────────────

/**
 * Замок с центром (x, y), высотой s: дужка (у открытого — поднята и сдвинута, одна нога свободна) и корпус
 * с замочной скважиной. Тёмная обводка — читается на пёстрой карте и на ч/б.
 */
export function drawLock(ctx, x, y, s, open = false, ink = C.ink, outline = C.shadow) {
  const k = s / 20;
  ctx.save();
  ctx.translate(x, y);
  ctx.scale(k, k);
  // дужка: от корпуса вверх, полукруг, вниз (у открытого — поднята на 4 px, правая нога не входит в корпус)
  const lift = open ? 4 : 0;
  const shackle = () => {
    ctx.beginPath();
    ctx.moveTo(-4.6, -1 - lift);
    ctx.lineTo(-4.6, -5 - lift);
    ctx.arc(0, -5 - lift, 4.6, Math.PI, 0);
    ctx.lineTo(4.6, open ? -5 : -1);
  };
  ctx.lineCap = "round";
  shackle();
  ctx.lineWidth = 6;
  ctx.strokeStyle = outline;
  ctx.stroke();
  shackle();
  ctx.lineWidth = 2.8;
  ctx.strokeStyle = ink;
  ctx.stroke();
  // корпус
  ctx.beginPath();
  ctx.rect(-7.5, -2, 15, 12);
  ctx.lineJoin = "round";
  ctx.lineWidth = 3;
  ctx.strokeStyle = outline;
  ctx.stroke();
  ctx.fillStyle = ink;
  ctx.fill();
  // скважина
  ctx.beginPath();
  ctx.arc(0, 2.6, 1.9, 0, 2 * Math.PI);
  ctx.rect(-0.9, 3, 1.8, 4);
  ctx.fillStyle = outline === C.shadow ? C.dark : outline;
  ctx.fill();
  ctx.restore();
}

/** Значок замка 26×20 для строки вспышки (как значки режима радио). */
export function lockSprite(open) {
  return sprite(open ? "alarm-lock-open" : "alarm-lock", 26, 20, (c) => drawLock(c, 12, 10, 19, open));
}

/** Плашка «внимание»: светлая с косыми полосами (inverse=false) или тёмная со светлой рамкой. */
function bannerPlate(w, h, inverse) {
  return sprite(`alarm-banner|${inverse ? 1 : 0}`, w, h, (c) => {
    chamferPath(c, 1.5, 1.5, w - 3, h - 3, 12);
    c.fillStyle = inverse ? C.dark : C.hazard;
    c.fill();
    c.save();
    c.clip();
    // косые полосы по краям — у обеих фаз (светлые на тёмной, тёмные на светлой)
    c.fillStyle = inverse ? C.hazard : C.dark;
    const band = 30;
    for (const x0 of [0, w - band]) {
      c.save();
      c.beginPath();
      c.rect(x0, 0, band, h);
      c.clip();
      for (let k = -h; k < band + h; k += 16) {
        c.beginPath();
        c.moveTo(x0 + k, h);
        c.lineTo(x0 + k + 8, h);
        c.lineTo(x0 + k + 8 + h, 0);
        c.lineTo(x0 + k + h, 0);
        c.closePath();
        c.fill();
      }
      c.restore();
    }
    c.restore();
    chamferPath(c, 1.5, 1.5, w - 3, h - 3, 12);
    c.lineWidth = 3;
    c.strokeStyle = C.dark;
    c.stroke();
    chamferPath(c, 5, 5, w - 10, h - 10, 9);
    c.lineWidth = 3;
    c.strokeStyle = inverse ? C.hazard : C.dark;
    c.stroke();
  });
}

export const BANNER_BOX = { w: 344, h: 92 };

const hits = (a, b) => !!b && a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

/**
 * Где поставить плашку w×h: по центру зоны безопасности на 36 % её высоты (над машиной в heading-up); мешает
 * прямоугольник из avoid (компас, часы) — сначала сдвинуть вбок не больше чем на maxShift (компас справа —
 * влево), иначе опустить под него. Возвращает {x, y}.
 */
export function bannerPlace(safe, w, h, avoid = [], maxShift = 70) {
  let x = Math.round(safe.x + safe.w / 2 - w / 2);
  let y = Math.round(safe.y + safe.h * 0.36 - h / 2);
  for (const r of avoid) {
    if (!hits({ x, y, w, h }, r)) continue;
    const left = r.x + r.w / 2 > x + w / 2; // препятствие правее центра плашки — уходим влево
    const nx = left ? Math.round(r.x - 10 - w) : Math.round(r.x + r.w + 10);
    if (Math.abs(nx - x) <= maxShift && nx >= safe.x && nx + w <= safe.x + safe.w) x = nx;
    else y = Math.round(r.y + r.h + 10);
  }
  return { x, y };
}

/**
 * Крупная мигающая плашка «IMPACT / TILTED / MOVING» над верхней серединой карты (над машиной), в зоне
 * безопасности; avoid — прямоугольники (компас, часы), от которых отойти (bannerPlace). Мигает инверсией: светлая
 * плашка с тёмными буквами ↔ тёмная со светлыми — надпись видна всегда. Возвращает прямоугольник или null.
 */
export function drawAlarmBanner(ctx, state, safe, avoid = []) {
  const alarm = state?.telemetry?.alarm;
  const now = state?.now ?? 0;
  const b = alarmBanner(alarmTracker(ctx), alarm, now);
  if (!b) return null;
  const { w, h } = BANNER_BOX;
  const { x, y } = bannerPlace(safe, w, h, avoid);
  const inverse = !blinkOn(now - b.t0, 1.6, 0.55); // начинается светлой фазой
  ctx.save();
  blit(ctx, bannerPlate(w, h, inverse), x, y);
  // слово — самым крупным трафаретом, который помещается между полосами
  const room = w - 2 * 30 - 20;
  const px = [64, 60, 56, 50].find((n) => textWidth(ctx, fontStencil(n), b.word) <= room) ?? 50;
  const font = fontStencil(px);
  ctx.font = font;
  ctx.textAlign = "center";
  ctx.textBaseline = "alphabetic";
  ctx.fillStyle = inverse ? C.hazard : C.dark;
  ctx.fillText(b.word, x + w / 2, y + h / 2 + capHeight(ctx, font, "M") / 2);
  ctx.restore();
  return { x, y, w, h };
}

/** Текст вспышки. */
export const flashText = (on) => (on ? "ALARM ON" : "ALARM OFF");

/**
 * Вспышка ALARM ON / OFF без плашки радио: на месте радио (rect) — плашка с замком и надписью.
 * plate — готовый спрайт плашки (из radio.js, чтобы не тянуть его сюда). Рисует, если вспышка идёт.
 */
export function drawFlashPlate(ctx, rect, flash, plate) {
  const { x, y, w, h } = rect;
  blit(ctx, plate, x, y);
  const text = flashText(flash.on);
  const size = [34, 30, 27, 25].find((n) => textWidth(ctx, fontLabel(n), text) <= w - 2 * 22 - 52) ?? 25;
  const font = fontLabel(size);
  const tw = textWidth(ctx, font, text);
  const total = 40 + 14 + tw;
  const x0 = Math.round(x + (w - total) / 2);
  const yc = y + h / 2;
  drawLock(ctx, x0 + 20, yc + 1, 38, !flash.on);
  ctx.save();
  ctx.font = font;
  ctx.textAlign = "left";
  ctx.textBaseline = "alphabetic";
  ctx.lineJoin = "round";
  ctx.lineWidth = 5;
  ctx.strokeStyle = C.shadow;
  const yb = yc + capHeight(ctx, font, "A") / 2;
  ctx.strokeText(text, x0 + 54, yb);
  ctx.fillStyle = C.ink;
  ctx.fillText(text, x0 + 54, yb);
  ctx.restore();
}
