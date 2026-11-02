// Уровень (крен и тангаж по MPU-6050) — виджет на месте спидометра, пока машина стоит. Контракт:
// docs/contracts/level.md («Экран»). Данные — state.telemetry.level = {sensor_ok, calibrated, pitch_deg, roll_deg,
// wheels_cm: {fl, fr, rl, rr} | null}; вкл/выкл — settings.level.show_on_screen.
//
// Плашка в том же углу и том же стиле, что спидометр (той же ширины — плашка радио/погоды не сдвигается; выше —
// в левой колонке над спидометром свободно): две строки «силуэт + угол» — машина сбоку (тангаж) и сзади (крен),
// наклонённые на усиленный угол (1–2° видно), цифры со знаком и словами, какая сторона выше; внизу — схема сверху
// с четырьмя колёсами и сколько подложить, см (ненулевые); оба угла ≤ 0.3° — рамка «LEVEL» с галочкой. Без
// калибровки — бирка UNCAL над правым краем плашки. Монохромный кинескоп: смысл — формой и яркостью.
//
// drawLevelPanel() рисует виджет без состояния HUD — его же показывает страница Level в админке.

import { C, DEG, blit, capHeight, chamferPath, fontNum, fontStencil, outlinedText, plateSprite, setting, sprite, textWidth } from "./theme.js";

export const LEVEL_EPS = 0.3; // «ровно»: оба угла по модулю ≤ 0.3°
export const SHOW_BELOW_KMH = 1; // показать: медленнее 1 км/ч …
export const SHOW_AFTER_S = 3; // … не меньше 3 с подряд
export const HIDE_ABOVE_KMH = 3; // спрятать сразу быстрее 3 км/ч

const PLATE = { w: 236, h: 224 };
const TAG_OVER = 18; // бирка UNCAL выступает над плашкой на столько
/** Габарит виджета (плашка + выступ бирки UNCAL сверху). Ширина — как у спидометра. */
export const LEVEL_BOX = { w: PLATE.w, h: PLATE.h + TAG_OVER };

const VIS_MAX = 22; // предел усиленного угла силуэта, °
const VIS_GAIN = 5; // усиление малых углов: 1° → ≈5°

// ── чистые помощники (tests/display/level.test.mjs) ───────────────────────

const r1 = (v) => Math.round(v * 10) / 10;

/** Угол для показа: «+2.4°», «-1.2°», «0.0°»; нет данных — «--.-°». */
export function fmtAngle(a) {
  if (!Number.isFinite(a)) return "--.-°";
  const v = r1(a);
  if (v === 0) return "0.0°";
  return `${v > 0 ? "+" : "-"}${Math.abs(v).toFixed(1)}°`;
}

/** Ось «ровно» (по округлённому до 0.1° значению — как на экране). */
export const axisLevel = (a) => Number.isFinite(a) && Math.abs(r1(a)) <= LEVEL_EPS + 1e-9;

/** Машина стоит ровно: оба угла по модулю ≤ 0.3°. */
export function isLevel(pitch, roll) {
  return axisLevel(pitch) && axisLevel(roll);
}

/** Тангаж словами: > 0 — перед выше зада. */
export function pitchWord(p) {
  if (!Number.isFinite(p)) return "PITCH";
  if (axisLevel(p)) return "LEVEL";
  return p > 0 ? "NOSE UP" : "NOSE DOWN";
}

/** Крен словами: > 0 — правый борт выше левого. */
export function rollWord(r) {
  if (!Number.isFinite(r)) return "ROLL";
  if (axisLevel(r)) return "LEVEL";
  return r > 0 ? "RIGHT HIGH" : "LEFT HIGH";
}

/** Угол наклона силуэта: малые углы усилены (1° → ≈5°), большие плавно упираются в 22°. Знак сохраняется. */
export function exaggerate(deg) {
  if (!Number.isFinite(deg)) return 0;
  return VIS_MAX * Math.tanh((VIS_GAIN * deg) / VIS_MAX);
}

/** Подложить, см: «2.5», «4», «12»; шаг 0.5. */
export function fmtCm(cm) {
  const v = Math.round(cm * 2) / 2;
  return Number.isInteger(v) ? String(v) : v.toFixed(1);
}

/** Колёса, под которые надо подложить (ненулевые, после округления до 0.5 см): {fl: "2.5", …}. */
export function wheelRaises(wheels) {
  const out = {};
  for (const k of ["fl", "fr", "rl", "rr"]) {
    const v = wheels?.[k];
    if (Number.isFinite(v) && Math.round(v * 2) / 2 > 0) out[k] = fmtCm(v);
  }
  return out;
}

/** Новое состояние «показывать ли» (гистерезис по скорости). */
export function newLevelTrack() {
  return { slowSince: null, shown: false };
}

/**
 * Показывать ли виджет: включён и датчик в порядке; скорость < 1 км/ч не меньше 3 с подряд (или скорости нет) —
 * показать; > 3 км/ч — спрятать сразу; между ними — как было. Меняет и возвращает track.
 */
export function levelVisibility(track, { now, speedKmh, enabled, sensorOk }) {
  if (!enabled || !sensorOk) {
    track.shown = false;
    track.slowSince = null;
    return track;
  }
  const v = Number.isFinite(speedKmh) ? Math.abs(speedKmh) : null;
  if (v !== null && v > HIDE_ABOVE_KMH) {
    track.shown = false;
    track.slowSince = null;
  } else if (v === null || v < SHOW_BELOW_KMH) {
    if (track.slowSince === null || now < track.slowSince) track.slowSince = now;
    if (now - track.slowSince >= SHOW_AFTER_S) track.shown = true;
  } else {
    track.slowSince = null; // 1…3 км/ч: показанный остаётся, отсчёт «стоим» начнётся заново
  }
  return track;
}

/** Входные данные гистерезиса из состояния HUD. */
export function levelInputs(state) {
  const l = state?.telemetry?.level;
  const mps = state?.view?.speedMps;
  return {
    now: state?.now ?? 0,
    speedKmh: Number.isFinite(mps) ? mps * 3.6 : null,
    enabled: setting(state, "level.show_on_screen", true) !== false,
    // нет связи с fnav-core — и уровень, и скорость устарели: остаётся спидометр с "--"
    sensorOk: !!l && l.sensor_ok === true && state?.view?.connected !== false,
  };
}

const tracks = new WeakMap();

/** Показывать ли виджет на этом холсте сейчас (учитывает этот кадр). */
export function levelVisible(ctx, state) {
  const key = ctx?.canvas ?? ctx;
  let t = tracks.get(key);
  if (!t) {
    t = newLevelTrack();
    tracks.set(key, t);
  }
  return levelVisibility(t, levelInputs(state)).shown;
}

/** Для отладки/тестов: сбросить состояние холста. */
export function resetLevel(ctx) {
  tracks.delete(ctx?.canvas ?? ctx);
}

// ── силуэты (спрайты; рисуются вокруг точки опоры — середины «земли» под колёсами) ──

const SIL = { w: 132, h: 76, py: 54 }; // спрайт силуэта с землёй; py — где земля (точка поворота)
const SHADOW = "rgba(0,0,0,0.9)";

/** Земля под машиной: светлая линия и штриховка ниже. */
function ground(c, x0, x1, y) {
  c.save();
  c.beginPath();
  c.rect(x0, y, x1 - x0, 14);
  c.clip();
  c.strokeStyle = "rgba(236,247,216,0.32)";
  c.lineWidth = 2;
  for (let x = x0 - 14; x < x1; x += 9) {
    c.beginPath();
    c.moveTo(x, y + 14);
    c.lineTo(x + 14, y);
    c.stroke();
  }
  c.restore();
  c.fillStyle = C.ink;
  c.fillRect(x0, y - 1, x1 - x0, 3);
}

function wheel(c, x, y, r) {
  c.beginPath();
  c.arc(x, y, r + 1.6, 0, 2 * Math.PI);
  c.fillStyle = SHADOW;
  c.fill();
  c.beginPath();
  c.arc(x, y, r, 0, 2 * Math.PI);
  c.fillStyle = C.dark;
  c.fill();
  c.lineWidth = 2;
  c.strokeStyle = C.ink;
  c.stroke();
  c.beginPath();
  c.arc(x, y, r * 0.38, 0, 2 * Math.PI);
  c.fillStyle = C.ink;
  c.fill();
}

/** Кемпер-альков на Peugeot Boxer (1994–2006) сбоку, нос вправо: высокий жилой кузов с длинным свесом за задним
 *  колесом и люком на крыше, альков (спальник) над кабиной с округлым носом, наклонное лобовое, короткий покатый
 *  капот. Видна левая сторона — входная дверь жилого кузова справа, её нет; окна и багажный люк. */
function sideSprite() {
  return sprite("level-side", SIL.w, SIL.h, (c) => {
    const ox = SIL.w / 2;
    const oy = SIL.py;
    ground(c, 4, SIL.w - 4, oy);
    c.save();
    c.translate(ox, oy);
    const R = 7; // радиус колеса
    const ar = R + 2.5; // радиус арки
    const fx = 29; // переднее колесо — под кабиной
    const rx = -15; // заднее — за ним длинный свес кузова
    const sill = -6; // низ кабины
    const skirt = -10; // юбка жилого кузова
    const top = -42; // крыша
    // арка над колесом от линии низа y до неё же
    const arch = (x, y) => {
      const a = Math.asin((y + R) / ar);
      c.arc(x, -R, ar, a, Math.PI - a, true);
    };
    const body = () => {
      c.beginPath();
      c.moveTo(-44, skirt);
      c.lineTo(-44, top + 3);
      c.quadraticCurveTo(-44, top, -41, top);
      c.lineTo(29, top);
      c.quadraticCurveTo(38, top, 39.5, top + 5.5); // округлый нос алькова
      c.lineTo(38.5, -34.5);
      c.quadraticCurveTo(37.5, -32, 34, -32);
      c.lineTo(28, -32); // низ алькова → верх лобового
      c.lineTo(35, -20); // лобовое
      c.lineTo(41.5, -17); // капот
      c.quadraticCurveTo(44, -16, 44, -13.5);
      c.lineTo(44, sill);
      arch(fx, sill);
      c.lineTo(14, sill);
      c.lineTo(12, skirt);
      arch(rx, skirt);
      c.closePath();
    };
    body();
    c.lineJoin = "round";
    c.lineWidth = 4;
    c.strokeStyle = SHADOW;
    c.stroke();
    c.fillStyle = C.ink;
    c.fill();
    // люк на крыше
    c.fillStyle = SHADOW;
    c.fillRect(-24, top - 3.5, 12, 4);
    c.fillStyle = C.ink;
    c.fillRect(-23, top - 2.5, 10, 3);
    // окна и линии — тёмным по светлому
    c.fillStyle = C.dark;
    c.beginPath(); // окно двери кабины — до стойки лобового
    c.moveTo(18, -30);
    c.lineTo(27.5, -30);
    c.lineTo(31.6, -23.5);
    c.lineTo(18, -23.5);
    c.closePath();
    c.fill();
    c.fillRect(19, -40, 9, 4.5); // окошко алькова
    c.fillRect(14, -32.5, 14.5, 1.5); // низ алькова над кабиной
    c.fillRect(14, -32.5, 1.5, 24); // стык кабины и жилого кузова
    c.fillRect(-39, -37, 13, 8.5); // окна жилого кузова
    c.fillRect(-8, -37, 15, 8.5);
    c.lineWidth = 1.5;
    c.strokeStyle = C.dark;
    c.strokeRect(-40.25, -22.75, 10.5, 8.5); // багажный люк
    c.fillRect(37.5, -10, 6.5, 2.5); // бампер
    c.fillRect(-44, -13, 5, 2);
    c.fillRect(41, -15.5, 3, 2.5); // фара
    wheel(c, fx, -R, R);
    wheel(c, rx, -R, R);
    c.restore();
  });
}

/** Кемпер сзади: широкий жилой кузов со скруглённой крышей и люком, заднее окно, лестница на крышу, вертикальные
 *  фонари по углам, бампер с номером, колёса из-под кузова, зеркала кабины на кронштейнах по бокам. */
function rearSprite() {
  return sprite("level-rear", SIL.w, SIL.h, (c) => {
    const ox = SIL.w / 2;
    const oy = SIL.py;
    ground(c, 4, SIL.w - 4, oy);
    c.save();
    c.translate(ox, oy);
    // колёса (шины) — тёмные со светлой обводкой, выглядывают из-под кузова; колея уже кузова
    for (const sx of [-1, 1]) {
      const x0 = sx * 19 - 7.5;
      c.fillStyle = SHADOW;
      c.fillRect(x0 - 1.5, -12, 18, 12);
      c.fillStyle = C.dark;
      c.fillRect(x0, -11, 15, 11);
      c.lineWidth = 2;
      c.strokeStyle = C.ink;
      c.strokeRect(x0 + 1, -10, 13, 9);
      c.fillStyle = C.ink;
      c.fillRect(x0 + 3, -7, 9, 2); // протектор
    }
    const top = -42;
    const body = () => {
      c.beginPath();
      c.moveTo(-29, -12);
      c.lineTo(-29, top + 5);
      c.quadraticCurveTo(-29, top, -24, top);
      c.lineTo(24, top);
      c.quadraticCurveTo(29, top, 29, top + 5);
      c.lineTo(29, -12);
      c.closePath();
    };
    // зеркала на кронштейнах — шире кузова
    c.fillStyle = SHADOW;
    c.fillRect(-35.5, -31.5, 7.5, 10);
    c.fillRect(28, -31.5, 7.5, 10);
    body();
    c.lineJoin = "round";
    c.lineWidth = 4;
    c.strokeStyle = SHADOW;
    c.stroke();
    c.fillStyle = C.ink;
    c.fill();
    c.fillRect(-34.5, -30.5, 5.5, 8);
    c.fillRect(29, -30.5, 5.5, 8);
    // люк на крыше
    c.fillStyle = SHADOW;
    c.fillRect(-8, top - 3.5, 16, 4);
    c.fillStyle = C.ink;
    c.fillRect(-7, top - 2.5, 14, 3);
    // заднее окно, лестница, фонари
    c.fillStyle = C.dark;
    c.fillRect(-13, -37, 24, 9);
    c.fillRect(15, top + 1.5, 1.5, 25); // лестница: стойки и ступеньки
    c.fillRect(20.5, top + 1.5, 1.5, 25);
    for (let y = top + 4; y < -17; y += 4.5) c.fillRect(15, y, 7, 1.5);
    c.fillRect(-27, -26, 3.5, 11);
    c.fillRect(23.5, -26, 3.5, 11);
    // бампер: тёмный зазор и светлая полоса шире кузова, номер — тёмный прямоугольник
    c.fillStyle = SHADOW;
    c.fillRect(-31, -14.5, 62, 7.5);
    c.fillStyle = C.ink;
    c.fillRect(-30, -13.5, 60, 5.5);
    c.fillStyle = C.dark;
    c.fillRect(-7, -13, 14, 4.5);
    c.restore();
  });
}

/** Галочка (для LEVEL). */
function check(ctx, x, y, s) {
  const path = () => {
    ctx.beginPath();
    ctx.moveTo(x - s * 0.5, y);
    ctx.lineTo(x - s * 0.15, y + s * 0.38);
    ctx.lineTo(x + s * 0.55, y - s * 0.42);
  };
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  path();
  ctx.lineWidth = 9;
  ctx.strokeStyle = SHADOW;
  ctx.stroke();
  path();
  ctx.lineWidth = 4.5;
  ctx.strokeStyle = C.ink;
  ctx.stroke();
}

/** Бирка «UNCAL»: светлая, тёмные трафаретные буквы (как предупреждения). */
function uncalSprite() {
  return sprite("level-uncal", 96, 27, (c) => {
    chamferPath(c, 1.5, 1.5, 93, 24, 6);
    c.fillStyle = C.hazard;
    c.fill();
    c.lineWidth = 3;
    c.strokeStyle = C.dark;
    c.stroke();
  });
}

/** Окно с силуэтом: тёмная вставка, пунктир «настоящего горизонта», силуэт с землёй под углом visDeg. */
function drawTiltWindow(ctx, x, y, w, h, spr, visDeg, dim) {
  ctx.save();
  chamferPath(ctx, x, y, w, h, 6);
  ctx.fillStyle = "rgba(4,6,3,0.82)";
  ctx.fill();
  ctx.lineWidth = 2;
  ctx.strokeStyle = C.metalLo;
  ctx.stroke();
  ctx.clip();
  const px = x + w / 2;
  const py = y + h - 12; // земля
  // «настоящий» горизонт — пунктир через точку опоры
  ctx.setLineDash([6, 5]);
  ctx.lineWidth = 2;
  ctx.strokeStyle = C.inkDim;
  ctx.beginPath();
  ctx.moveTo(x, py);
  ctx.lineTo(x + w, py);
  ctx.stroke();
  ctx.setLineDash([]);
  if (spr) {
    ctx.save();
    ctx.translate(px, py);
    ctx.rotate(-visDeg * DEG); // > 0 — против часовой: нос (сбоку) / правый борт (сзади) выше
    if (dim) ctx.globalAlpha *= 0.45;
    ctx.drawImage(spr.canvas, -SIL.w / 2, -SIL.py, SIL.w, SIL.h);
    ctx.restore();
  }
  // метки «настоящего горизонта» по краям окна — поверх силуэта, как риски у пузырькового уровня
  for (const [x0, dir] of [[x, 1], [x + w, -1]]) {
    ctx.beginPath();
    ctx.moveTo(x0, py - 6);
    ctx.lineTo(x0 + dir * 9, py);
    ctx.lineTo(x0, py + 6);
    ctx.closePath();
    ctx.lineJoin = "round";
    ctx.lineWidth = 3;
    ctx.strokeStyle = SHADOW;
    ctx.stroke();
    ctx.fillStyle = C.accent;
    ctx.fill();
  }
  ctx.restore();
}

/** Слово + угол справа от окна; слово ужимается кеглем, чтобы влезть в колонку. */
function drawAngleText(ctx, xr, w, yTop, word, angle, levelAxis) {
  const wf = [25, 23, 21].map((n) => fontNum(n, 600)).find((f) => textWidth(ctx, f, word) <= w) ?? fontNum(21, 600);
  outlinedText(ctx, word, xr, yTop + 15, wf, { align: "right", lw: 4, fill: levelAxis ? C.ink : C.accent });
  const txt = fmtAngle(angle);
  const nf = [42, 38, 34].map((n) => fontNum(n, 700)).find((f) => textWidth(ctx, f, txt) <= w) ?? fontNum(34, 700);
  outlinedText(ctx, txt, xr, yTop + 46, nf, { align: "right", lw: 5, fill: Number.isFinite(angle) ? C.ink : C.inkDim });
}

/** Схема сверху (нос вверх) с четырьмя колёсами и «сколько подложить» у ненулевых. */
function drawWheels(ctx, x, y, w, h, wheels) {
  const raise = wheelRaises(wheels);
  const cx = x + w / 2 + 14;
  const cy = y + h / 2;
  const bw = 28;
  const bh = 50;
  const top = cy - bh / 2;
  // подпись слева; длинные числа слева («10.5») не должны на неё наезжать: сначала мельче подпись и цифры,
  // в крайнем случае подписи нет (цифры важнее)
  const numRight = cx - bw / 2 - 3 - 9;
  const leftNums = ["fl", "rl"].filter((k) => k in raise).map((k) => raise[k]);
  const fit = [[26, 21], [23, 17], [22, 0]].find(([n, l], i, all) => {
    const nw = Math.max(0, ...leftNums.map((t) => textWidth(ctx, fontNum(n, 700), t)));
    const lw = l ? Math.max(textWidth(ctx, fontNum(l, 600), "RAISE"), textWidth(ctx, fontNum(l, 600), "cm")) : -6;
    return i === all.length - 1 || numRight - nw >= x + 2 + lw + 6;
  });
  if (fit[1]) {
    outlinedText(ctx, "RAISE", x + 2, cy - 12, fontNum(fit[1], 600), { align: "left", lw: 4, fill: C.inkDim });
    outlinedText(ctx, "cm", x + 2, cy + 13, fontNum(fit[1], 600), { align: "left", lw: 4, fill: C.inkDim });
  }
  // кемпер сверху: жилой кузов с округлым краем алькова спереди, из-под него — капот кабины, уже кузова; люк
  ctx.save();
  const outline = (fill) => {
    ctx.lineWidth = 4;
    ctx.strokeStyle = SHADOW;
    ctx.stroke();
    ctx.lineWidth = 2;
    ctx.strokeStyle = C.ink;
    ctx.stroke();
    ctx.fillStyle = fill;
    ctx.fill();
  };
  const hw = bw / 2 - 4;
  ctx.beginPath(); // капот
  ctx.moveTo(cx - hw, top + 9);
  ctx.lineTo(cx - hw, top + 3);
  ctx.quadraticCurveTo(cx - hw, top, cx - hw + 3, top);
  ctx.lineTo(cx + hw - 3, top);
  ctx.quadraticCurveTo(cx + hw, top, cx + hw, top + 3);
  ctx.lineTo(cx + hw, top + 9);
  outline("rgba(236,247,216,0.3)");
  const bt = top + 6;
  ctx.beginPath(); // кузов с альковом
  ctx.moveTo(cx - bw / 2, top + bh);
  ctx.lineTo(cx - bw / 2, bt + 6);
  ctx.quadraticCurveTo(cx - bw / 2, bt, cx - bw / 2 + 6, bt);
  ctx.lineTo(cx + bw / 2 - 6, bt);
  ctx.quadraticCurveTo(cx + bw / 2, bt, cx + bw / 2, bt + 6);
  ctx.lineTo(cx + bw / 2, top + bh);
  ctx.closePath();
  ctx.fillStyle = C.dark;
  ctx.fill(); // капот не просвечивает под альковом
  outline("rgba(236,247,216,0.14)");
  ctx.lineWidth = 1.5;
  ctx.strokeStyle = C.ink;
  ctx.strokeRect(cx - 4.5, top + bh * 0.56, 9, 7); // люк
  // колёса и числа
  const spots = {
    fl: { wx: cx - bw / 2 - 3, wy: top + 11, left: true },
    fr: { wx: cx + bw / 2 + 3, wy: top + 11, left: false },
    rl: { wx: cx - bw / 2 - 3, wy: top + bh - 11, left: true },
    rr: { wx: cx + bw / 2 + 3, wy: top + bh - 11, left: false },
  };
  const nf = fontNum(fit[0], 700);
  for (const [k, s] of Object.entries(spots)) {
    const on = k in raise;
    ctx.fillStyle = SHADOW;
    ctx.fillRect(s.wx - 4.5, s.wy - 8.5, 9, 17);
    if (on) {
      ctx.fillStyle = C.ink;
      ctx.fillRect(s.wx - 3, s.wy - 7, 6, 14);
    } else {
      ctx.lineWidth = 2;
      ctx.strokeStyle = C.inkDim;
      ctx.strokeRect(s.wx - 2.5, s.wy - 6.5, 5, 13);
    }
    if (on) {
      const tx = s.left ? s.wx - 9 : s.wx + 9;
      outlinedText(ctx, raise[k], tx, s.wy, nf, { align: s.left ? "right" : "left", lw: 5 });
    }
  }
  ctx.restore();
}

/** Рамка «LEVEL» с галочкой на месте схемы колёс. */
function drawLevelBadge(ctx, x, y, w, h) {
  const font = fontStencil(34);
  const tw = textWidth(ctx, font, "LEVEL");
  const bw = Math.min(w, Math.ceil(tw + 70));
  const bx = Math.round(x + (w - bw) / 2);
  const by = Math.round(y + 4);
  const bh = h - 8;
  ctx.save();
  chamferPath(ctx, bx, by, bw, bh, 8);
  ctx.fillStyle = "rgba(236,247,216,0.12)";
  ctx.fill();
  ctx.lineWidth = 6;
  ctx.strokeStyle = SHADOW;
  ctx.stroke();
  ctx.lineWidth = 3;
  ctx.strokeStyle = C.ink;
  ctx.stroke();
  check(ctx, bx + 26, by + bh / 2, 26);
  outlinedText(ctx, "LEVEL", bx + 48, by + bh / 2, font, { align: "left", lw: 6 });
  ctx.restore();
}

/**
 * Виджет уровня с левым верхним углом габарита LEVEL_BOX в (x, y): level — telemetry.level
 * ({sensor_ok, calibrated, pitch_deg, roll_deg, wheels_cm}). Не зависит от состояния HUD (для превью в админке).
 * opts.noSensor — нарисовать «нет датчика» (в HUD виджет тогда скрыт, здесь — для превью).
 */
export function drawLevelPanel(ctx, x, y, level, now = 0, opts = {}) {
  const l = level ?? {};
  const px = Math.round(x);
  const py = Math.round(y + TAG_OVER);
  const { w, h } = PLATE;
  const pitch = l.pitch_deg;
  const roll = l.roll_deg;
  const noSensor = opts.noSensor || l.sensor_ok === false;
  ctx.save();
  blit(ctx, plateSprite(w, h, { chamfer: 10 }), px, py);

  const winX = px + 12;
  const winW = 104;
  const winH = 62;
  const colR = px + w - 14;
  const colW = colR - (winX + winW + 8);
  const rows = [
    { y: py + 10, spr: sideSprite(), a: pitch, word: pitchWord(pitch) },
    { y: py + 78, spr: rearSprite(), a: roll, word: rollWord(roll) },
  ];
  for (const r of rows) {
    const known = Number.isFinite(r.a) && !noSensor;
    drawTiltWindow(ctx, winX, r.y, winW, winH, r.spr, known ? exaggerate(r.a) : 0, !known);
    drawAngleText(ctx, colR, colW, r.y, noSensor ? "NO SENSOR" : r.word, noSensor ? null : r.a, axisLevel(r.a));
  }

  // черта и нижняя часть: LEVEL или колёса
  ctx.fillStyle = C.metal;
  ctx.fillRect(px + 14, py + 148, w - 28, 2);
  const by = py + 154;
  const bh = h - 154 - 10;
  if (!noSensor && isLevel(pitch, roll)) drawLevelBadge(ctx, px + 12, by, w - 24, bh);
  else drawWheels(ctx, px + 14, by, w - 28, bh, noSensor ? null : l.wheels_cm);

  // без калибровки — бирка над правым краем плашки
  if (l.calibrated === false && !noSensor) {
    const t = uncalSprite();
    const tx = px + w - 96 - 16;
    const ty = py - TAG_OVER;
    blit(ctx, t, tx, ty);
    const f = fontStencil(22);
    ctx.font = f;
    ctx.textAlign = "center";
    ctx.textBaseline = "alphabetic";
    ctx.fillStyle = C.dark;
    ctx.fillText("UNCAL", tx + 48, ty + 13.5 + capHeight(ctx, f, "U") / 2);
  }
  ctx.restore();
  return { x: px, y: py - TAG_OVER, w, h: h + TAG_OVER };
}

/**
 * В HUD: виджет вместо спидометра, если пора (levelVisible). Левый нижний угол зоны безопасности, как у
 * спидометра. Возвращает прямоугольник или null (тогда рисуется спидометр).
 */
export function drawLevel(ctx, state, safe) {
  if (!levelVisible(ctx, state)) return null;
  const x = Math.round(safe.x);
  const y = Math.round(safe.y + safe.h - LEVEL_BOX.h);
  return drawLevelPanel(ctx, x, y, state.telemetry.level, state?.now ?? 0);
}

