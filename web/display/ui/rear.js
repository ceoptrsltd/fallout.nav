// Задний вид (docs/contracts/camera.md «Задняя камера»; app.js: view.camera = "rear", view.rearSignal):
// светлая метка REAR вверху — самое яркое пятно экрана, задний вид узнаётся краем глаза и на ч/б кинескопе;
// нет картинки — по центру плашка REAR CAMERA / NO SIGNAL (аварийная, как NO GPS) или CONNECTING (тёмная).
// Видео зеркалит слой камеры (map/camera.js), HUD не зеркалится — надписи читаются как обычно.

import { C, blit, chamferPath, fontLabel, fontStencil, hazardSprite, outlinedText, plateSprite, sprite, textWidth } from "./theme.js";

export const REAR_TAG = { h: 48, minW: 132, padX: 20 };
export const REAR_MSG = { w: 400, h: 110 };
const TAG_FONT = () => fontStencil(34);
const TAG_TEXT = "REAR";

/** Задний вид на экране (режим камеры с задней камерой). */
export function rearView(state) {
  const v = state?.view;
  return v?.mode === "camera" && v?.camera === "rear";
}

const hits = (a, b) => !!b && a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;

/** Самый широкий свободный промежуток по x на высоте [y, y + h] среди avoid (с зазором), не уже w; или null. */
function widestGap(safe, y, w, h, avoid, gap) {
  const blocks = avoid
    .filter((r) => r && r.y < y + h + gap && r.y + r.h > y - gap)
    .map((r) => [r.x - gap, r.x + r.w + gap])
    .sort((a, b) => a[0] - b[0]);
  let best = null;
  let x0 = safe.x;
  const consider = (a, b) => {
    if (b - a >= w && (!best || b - a > best[1] - best[0])) best = [a, b];
  };
  for (const [a, b] of blocks) {
    consider(x0, a);
    x0 = Math.max(x0, b);
  }
  consider(x0, safe.x + safe.w);
  return best;
}

/**
 * Где метка: как можно выше — верхняя полоса зоны безопасности, иначе сразу под одним из avoid (компас, часы);
 * на этой высоте — самый широкий свободный промежуток, в нём — как можно ближе к середине экрана
 * (v1/v3: между часами и компасом; v2 — лента компаса во всю ширину: правее часов под ней).
 */
export function rearTagPlace(safe, w, h, avoid = []) {
  const gap = 10;
  const cx = safe.x + safe.w / 2;
  const rows = [Math.round(safe.y + 3), ...avoid.filter(Boolean).map((r) => Math.round(r.y + r.h + gap))]
    .filter((y) => y + h <= safe.y + safe.h)
    .sort((a, b) => a - b);
  for (const y of rows) {
    const g = widestGap(safe, y, w, h, avoid, gap);
    if (g) return { x: Math.round(Math.min(Math.max(cx - w / 2, g[0]), g[1] - w)), y, w, h };
  }
  let y = Math.round(safe.y + 3);
  for (const r of avoid) if (r && hits({ x: cx - w / 2, y, w, h }, r)) y = Math.round(r.y + r.h + gap);
  return { x: Math.round(cx - w / 2), y, w, h };
}

/** Светлая плашка метки: заливка «люминофором», тёмный контур и внутренняя рамка. */
function tagPlate(w, h) {
  return sprite("rear-tag", w, h, (c) => {
    chamferPath(c, 1.5, 1.5, w - 3, h - 3, 9);
    c.fillStyle = C.ink;
    c.fill();
    c.lineWidth = 3;
    c.strokeStyle = C.dark;
    c.stroke();
    chamferPath(c, 6, 6, w - 12, h - 12, 6);
    c.lineWidth = 2;
    c.strokeStyle = C.dark;
    c.stroke();
  });
}

/** Метка REAR вверху. avoid — прямоугольники компаса и часов. Возвращает прямоугольник метки. */
export function drawRearTag(ctx, state, safe, avoid = []) {
  const font = TAG_FONT();
  const w = Math.max(REAR_TAG.minW, Math.ceil(textWidth(ctx, font, TAG_TEXT)) + 2 * REAR_TAG.padX);
  const r = rearTagPlace(safe, w, REAR_TAG.h, avoid);
  blit(ctx, tagPlate(r.w, r.h), r.x, r.y);
  outlinedText(ctx, TAG_TEXT, r.x + r.w / 2, r.y + r.h / 2, font, { fill: C.dark, lw: 0 });
  return r;
}

/** Где плашка «нет сигнала»: по центру, ниже середины — над ней остаётся место плашке охраны (IMPACT…). */
export function rearMsgPlace(safe) {
  const { w, h } = REAR_MSG;
  return { x: Math.round(safe.x + safe.w / 2 - w / 2), y: Math.round(safe.y + safe.h * 0.56 - h / 2), w, h };
}

/**
 * Нет картинки задней камеры: signal = "no_signal" — аварийная светлая плашка NO SIGNAL; "connecting" — тёмная
 * CONNECTING. Остальное ("ok", "wait" — ждём первый кадр) — ничего. Возвращает прямоугольник или null.
 */
export function drawRearSignal(ctx, state, safe, signal = state?.view?.rearSignal) {
  if (signal !== "no_signal" && signal !== "connecting") return null;
  const r = rearMsgPlace(safe);
  const bad = signal === "no_signal";
  blit(ctx, bad ? hazardSprite(r.w, r.h) : plateSprite(r.w, r.h, { chamfer: 10 }), r.x, r.y);
  const word = bad ? "NO SIGNAL" : "CONNECTING";
  const room = r.w - 2 * 22 - 28; // между косыми полосами аварийной плашки
  const px = [46, 42, 38, 34].find((n) => textWidth(ctx, fontStencil(n), word) <= room) ?? 34;
  if (bad) {
    outlinedText(ctx, "REAR CAMERA", r.x + r.w / 2, r.y + 30, fontLabel(25), { fill: C.dark, lw: 0 });
    outlinedText(ctx, word, r.x + r.w / 2, r.y + 74, fontStencil(px), { fill: C.dark, lw: 0 });
  } else {
    outlinedText(ctx, "REAR CAMERA", r.x + r.w / 2, r.y + 30, fontLabel(25), { fill: C.inkDim, lw: 4 });
    outlinedText(ctx, word, r.x + r.w / 2, r.y + 74, fontStencil(px), { fill: C.ink, lw: 5 });
  }
  return r;
}
