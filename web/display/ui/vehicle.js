// Иконка машины, вид сверху: «пустошный бронированный маслкар».
// Оригинальный векторный рисунок (Path2D/canvas). Нос — вверх (−y). Длина ≈ 54 px на холсте 768×576.

import { C, DEG, pulse, sprite } from "./theme.js";

const SW = 48; // размер спрайта, логические px
const SH = 66;
const VSS = 3; // детальность спрайта (поворот в north_up без «лесенки»)

/** Силуэт кузова (без колёс), центр машины в (0,0): длинный капот, выпуклые крылья, широкая корма. */
function bodyPath() {
  const p = new Path2D();
  p.moveTo(0, -23.5);
  // правая сторона (x>0), затем зеркально
  p.lineTo(7, -23.5);
  p.quadraticCurveTo(10.5, -23.3, 11.3, -20.5); // скруглённый нос
  p.bezierCurveTo(12.6, -18, 12.7, -11, 11.6, -8.5); // переднее крыло
  p.lineTo(10.9, -6); // талия (двери)
  p.lineTo(10.9, 4);
  p.bezierCurveTo(13.6, 6.5, 13.8, 17, 12.8, 20); // заднее крыло — шире
  p.quadraticCurveTo(12, 23.2, 9, 23.5); // корма
  p.lineTo(-9, 23.5);
  p.quadraticCurveTo(-12, 23.2, -12.8, 20);
  p.bezierCurveTo(-13.8, 17, -13.6, 6.5, -10.9, 4);
  p.lineTo(-10.9, -6);
  p.lineTo(-11.6, -8.5);
  p.bezierCurveTo(-12.7, -11, -12.6, -18, -11.3, -20.5);
  p.quadraticCurveTo(-10.5, -23.3, -7, -23.5);
  p.closePath();
  return p;
}

/** Решётка-таран спереди (клин). */
function ramPath() {
  const p = new Path2D();
  p.moveTo(-11.5, -22);
  p.lineTo(-4.5, -28.8);
  p.lineTo(4.5, -28.8);
  p.lineTo(11.5, -22);
  p.lineTo(8.5, -22);
  p.lineTo(3.2, -26.4);
  p.lineTo(-3.2, -26.4);
  p.lineTo(-8.5, -22);
  p.closePath();
  return p;
}

function wheels() {
  // [x, y, w, h] — левые; правые зеркально. Колёса почти спрятаны под крыльями.
  return [
    [-14.2, -19.5, 4.6, 10.5],
    [-15.2, 7.5, 5.2, 11.8],
  ];
}

/** Шипы на ступицах и клыки тарана: тонкие треугольники наружу. */
function spikesPath() {
  const p = new Path2D();
  const tri = (x0, y0, x1, y1, tx, ty) => {
    p.moveTo(x0, y0);
    p.lineTo(tx, ty);
    p.lineTo(x1, y1);
    p.closePath();
  };
  for (const s of [-1, 1]) {
    tri(s * 14, -15.9, s * 14, -12.9, s * 20, -14.4); // передняя ступица
    tri(s * 15, 11.9, s * 15, 15.3, s * 21.5, 13.6); // задняя ступица
    tri(s * 10.2, -23.2, s * 12.2, -21.2, s * 13.8, -28); // клыки тарана
  }
  return p;
}

const EXHAUST_X = [-9.2, -6.6, 6.6, 9.2];

function drawCar(c) {
  c.translate(SW / 2, SH / 2);
  c.lineJoin = "round";
  c.lineCap = "round";

  const body = bodyPath();
  const ram = ramPath();
  const spikes = spikesPath();
  const wh = wheels();

  const silhouette = (cb) => {
    cb(body);
    cb(ram);
    cb(spikes);
    for (const [x, y, w, h] of wh) {
      for (const s of [-1, 1]) {
        const p = new Path2D();
        p.rect(s < 0 ? x : -x - w, y, w, h);
        cb(p);
      }
    }
    for (const x of EXHAUST_X) {
      const p = new Path2D();
      p.rect(x - 1.2, 22, 2.4, 4.6);
      cb(p);
    }
  };

  // 1) светлый ореол — видно на тёмной карте; 2) чёрный контур — видно на светлой
  silhouette((p) => {
    c.lineWidth = 6.5;
    c.strokeStyle = "rgba(240,245,230,0.92)";
    c.stroke(p);
  });
  silhouette((p) => {
    c.lineWidth = 3.2;
    c.strokeStyle = C.dark;
    c.stroke(p);
  });

  // колёса с протектором
  for (const [x, y, w, h] of wh) {
    for (const s of [-1, 1]) {
      const xx = s < 0 ? x : -x - w;
      c.fillStyle = "#171712";
      c.fillRect(xx, y, w, h);
      c.fillStyle = "#62625a";
      for (let k = y + 1.2; k < y + h - 0.5; k += 2.4) c.fillRect(xx + 0.5, k, w - 1, 1);
    }
  }
  c.fillStyle = C.metalHi;
  c.fill(spikes);

  // выхлопные трубы
  for (const x of EXHAUST_X) {
    c.fillStyle = "#b4b6a8";
    c.fillRect(x - 1.2, 22, 2.4, 4.6);
    c.fillStyle = C.dark;
    c.beginPath();
    c.arc(x, 26.1, 0.8, 0, 2 * Math.PI);
    c.fill();
  }

  // кузов: выгоревшая краска с бликом по центру
  const g = c.createLinearGradient(-13, 0, 13, 0);
  g.addColorStop(0, "#9d9679");
  g.addColorStop(0.5, "#e2dbbd");
  g.addColorStop(1, "#968f73");
  c.fillStyle = g;
  c.fill(body);

  c.save();
  c.clip(body);
  // двойная гоночная полоса — главный «маслкар»-признак, читается и в ч/б
  c.fillStyle = "#2b2c25";
  c.fillRect(-4.6, -24, 3, 48);
  c.fillRect(1.6, -24, 3, 48);
  // ржавчина
  c.fillStyle = "rgba(130,80,40,0.6)";
  for (const [x, y, r] of [[-10, -13, 2], [10.5, 15, 2.4], [-9.5, 19, 1.5], [9.8, -1, 1.3]]) {
    c.beginPath();
    c.arc(x, y, r, 0, 2 * Math.PI);
    c.fill();
  }
  // бронелисты на дверях с заклёпками
  c.fillStyle = "#6c6958";
  for (const s of [-1, 1]) {
    c.fillRect(s > 0 ? 8.4 : -11, -6.5, 2.6, 11);
  }
  c.restore();

  // капот: «нагнетатель» (blower) с воздухозаборником, пробивающий капот
  c.fillStyle = "#8f9184";
  c.fillRect(-3.4, -17.5, 6.8, 7.5);
  c.strokeStyle = C.dark;
  c.lineWidth = 0.9;
  c.strokeRect(-3.4, -17.5, 6.8, 7.5);
  for (let y = -16.2; y < -10.5; y += 1.6) {
    c.beginPath();
    c.moveTo(-3.4, y);
    c.lineTo(3.4, y);
    c.stroke();
  }
  c.fillStyle = "#d3d5c7";
  c.fillRect(-2.8, -22, 5.6, 4.5);
  c.fillStyle = C.dark;
  for (let x = -1.8; x <= 1.9; x += 1.2) c.fillRect(x - 0.3, -21.4, 0.6, 3.4);
  c.strokeRect(-2.8, -22, 5.6, 4.5);

  // фары
  c.fillStyle = "#fbfbe8";
  c.fillRect(-10.2, -23, 2.8, 1.6);
  c.fillRect(7.4, -23, 2.8, 1.6);

  // лобовое стекло — «бойница» с решёткой
  c.fillStyle = "#1a1e20";
  c.beginPath();
  c.moveTo(-8.4, -1);
  c.lineTo(-9.6, -5.2);
  c.lineTo(9.6, -5.2);
  c.lineTo(8.4, -1);
  c.closePath();
  c.fill();
  c.strokeStyle = "#c0c4b6";
  c.lineWidth = 0.8;
  for (let x = -6; x <= 6; x += 3) {
    c.beginPath();
    c.moveTo(x, -4.9);
    c.lineTo(x * 0.92, -1.3);
    c.stroke();
  }

  // крыша — бронеплита
  c.fillStyle = "#a39e85";
  c.fillRect(-8.4, -1, 16.8, 9.5);
  c.fillStyle = "#2b2c25";
  c.fillRect(-4.6, -1, 3, 9.5);
  c.fillRect(1.6, -1, 3, 9.5);
  c.strokeStyle = C.dark;
  c.lineWidth = 1;
  c.strokeRect(-8.4, -1, 16.8, 9.5);

  // заднее стекло-щель
  c.fillStyle = "#1a1e20";
  c.fillRect(-7.8, 8.5, 15.6, 2);

  // запаска на корме
  c.beginPath();
  c.arc(0, 16, 4.6, 0, 2 * Math.PI);
  c.fillStyle = "#1b1b16";
  c.fill();
  c.lineWidth = 0.9;
  c.strokeStyle = "#6a6a60";
  c.beginPath();
  c.arc(0, 16, 3.4, 0, 2 * Math.PI);
  c.stroke();
  c.beginPath();
  c.arc(0, 16, 1.6, 0, 2 * Math.PI);
  c.fillStyle = "#9a9c8e";
  c.fill();

  // заклёпки
  c.fillStyle = C.dark;
  for (const [x, y] of [[-7.6, 0], [7.6, 0], [-7.6, 7.4], [7.6, 7.4], [-9.7, -5.5], [9.7, -5.5], [-9.7, 3.5], [9.7, 3.5],
    [-9.5, -19], [9.5, -19], [-9.8, -11], [9.8, -11]]) {
    c.beginPath();
    c.arc(x, y, 0.7, 0, 2 * Math.PI);
    c.fill();
  }

  // таран: прутья
  c.fillStyle = "#c9cbbd";
  c.fill(ram);
  c.strokeStyle = C.dark;
  c.lineWidth = 0.9;
  c.stroke(ram);
  c.lineWidth = 1.1;
  for (const x of [-6, -2, 2, 6]) {
    c.beginPath();
    c.moveTo(x, -22.8);
    c.lineTo(x * 0.62, -26.6);
    c.stroke();
  }

  // итоговый контур кузова поверх деталей
  c.lineWidth = 1.3;
  c.strokeStyle = C.dark;
  c.stroke(body);
}

function vehicleSprite() {
  return sprite("vehicle", SW, SH, drawCar, VSS);
}

/**
 * Нарисовать машину в (x,y), rotationDeg — поворот на экране (0 = нос вверх).
 * Без GPS-фикса — полупрозрачная и мигает.
 */
export function drawVehicle(ctx, x, y, rotationDeg, state) {
  const s = vehicleSprite();
  if (!s) return;
  const v = state?.view ?? {};
  let alpha = 1;
  if (v.hasFix === false) alpha = 0.22 + 0.5 * pulse(state.now ?? 0, 1);
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

export const VEHICLE_SIZE = { w: SW, h: SH, length: 55 };
