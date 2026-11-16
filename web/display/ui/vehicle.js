// Иконка машины, вид сверху: наш кемпер — альковный Peugeot Boxer (нарисован по фотографии крыши, 2026-11-16).
// Оригинальный векторный рисунок (Path2D/canvas). Нос — вверх (−y). Длина ≈ 56 px на холсте 768×576.
// Светлый силуэт с тёмным контуром (читается и на тёмной, и на светлой карте); на крыше: спереди две одинаковые
// солнечные панели поперёк, по центру кондиционер, справа от него панель вдоль борта, сзади два люка; из-под алькова
// выглядывает кромка капота, по бокам зеркала.

import { C, DEG, pulse, sprite } from "./theme.js";

const SW = 48; // размер спрайта, логические px
const SH = 66;
const VSS = 3; // детальность спрайта (поворот в north_up без «лесенки»)
const K = 0.72; // рисунок в единицах 40 × 78 (центр — 20, 39) → длина ≈ 56 px
const LIGHT = "#f2e9c8";

/** Прямоугольник со скруглёнными углами (Path2D). */
function rr(x, y, w, h, r) {
  const p = new Path2D();
  p.moveTo(x + r, y);
  p.lineTo(x + w - r, y);
  p.quadraticCurveTo(x + w, y, x + w, y + r);
  p.lineTo(x + w, y + h - r);
  p.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  p.lineTo(x + r, y + h);
  p.quadraticCurveTo(x, y + h, x, y + h - r);
  p.lineTo(x, y + r);
  p.quadraticCurveTo(x, y, x + r, y);
  p.closePath();
  return p;
}

/** Кромка капота, выглядывающая из-под алькова. */
function hoodPath() {
  const p = new Path2D();
  p.moveTo(8.5, 11);
  p.lineTo(8.5, 7.5);
  p.quadraticCurveTo(8.5, 3, 14, 3);
  p.lineTo(26, 3);
  p.quadraticCurveTo(31.5, 3, 31.5, 7.5);
  p.lineTo(31.5, 11);
  p.closePath();
  return p;
}

function drawCar(c) {
  c.translate(SW / 2, SH / 2);
  c.scale(K, K);
  c.translate(-20, -39);
  c.lineJoin = "round";
  const part = (path, lw) => {
    c.fillStyle = LIGHT;
    c.fill(path);
    c.lineWidth = lw;
    c.strokeStyle = C.dark;
    c.stroke(path);
  };
  const dark = (path) => {
    c.fillStyle = C.dark;
    c.fill(path);
  };
  const frame = (path, lw) => {
    c.lineWidth = lw;
    c.strokeStyle = C.dark;
    c.stroke(path);
  };
  part(rr(1.5, 9.5, 5, 3.6, 1.2), 1.2); // зеркала
  part(rr(33.5, 9.5, 5, 3.6, 1.2), 1.2);
  part(hoodPath(), 2.4);
  part(rr(3, 9, 34, 66, 4), 2.4); // кузов с альковом
  dark(rr(6.5, 13.5, 27, 11, 1)); // две солнечные панели спереди
  dark(rr(6.5, 26.5, 27, 11, 1));
  frame(rr(13.5, 41.5, 13, 13, 2.6), 1.8); // кондиционер
  dark(rr(28.5, 41, 5.5, 19, 0.8)); // панель вдоль правого борта
  frame(rr(7, 59, 8.5, 10, 1.6), 1.5); // люки
  frame(rr(18, 59, 8.5, 10, 1.6), 1.5);
}

function vehicleSprite() {
  return sprite("vehicle", SW, SH, drawCar, VSS);
}

const DR_ALPHA = 0.5; // счисление пути (тоннель): тусклая, но ровная — позиция расчётная, не потерянная

/** Прозрачность иконки: с фиксом — 1; без него — мигает; при счислении пути (view.deadReckoning) — ровно тусклая. */
export function vehicleAlpha(state) {
  const v = state?.view ?? {};
  if (v.hasFix !== false) return 1;
  return v.deadReckoning ? DR_ALPHA : 0.22 + 0.5 * pulse(state?.now ?? 0, 1);
}

/**
 * Нарисовать машину в (x,y), rotationDeg — поворот на экране (0 = нос вверх). (x,y) — центр машины
 * (антенна GPS впереди и правее, app.js vehicleCenter). Прозрачность — vehicleAlpha.
 */
export function drawVehicle(ctx, x, y, rotationDeg, state) {
  const s = vehicleSprite();
  if (!s) return;
  const alpha = vehicleAlpha(state);
  ctx.save();
  ctx.translate(x, y);
  ctx.rotate((rotationDeg || 0) * DEG);
  ctx.globalAlpha *= alpha;
  ctx.drawImage(s.canvas, -SW / 2, -SH / 2, SW, SH);
  ctx.restore();
}

/** Для галереи: векторная машина в масштабе k (крупный план, без спрайта). */
export function drawVehicleScaled(ctx, x, y, k, rotationDeg = 0) {
  ctx.save();
  ctx.translate(x, y);
  ctx.rotate(rotationDeg * DEG);
  ctx.scale(k, k);
  ctx.translate(-SW / 2, -SH / 2);
  drawCar(ctx);
  ctx.restore();
}

export const VEHICLE_SIZE = { w: SW, h: SH, length: 56 };
