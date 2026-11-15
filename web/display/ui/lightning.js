// Молния вокруг плашки станций (ui/power.js) — «треск»: раз в PERIOD_S секунд на BURST_S секунды по рамке
// мерцают короткие ломаные дуги с ветками и искрами наружу; между вспышками ничего не рисуется
// (HUD с перерисовкой по изменению не перерисовывается зря, map/retained.js). Всё детерминировано от now: случайность —
// из генератора, засеянного номером цикла и кадра (одинаковое now → одинаковые вызовы).
// Монохромный кинескоп: яркая сердцевина (C.ink) с тёмной обводкой и слабым ореолом, линии ≥ 2 px.

import { C } from "./theme.js";

export const PERIOD_S = 6;
export const BURST_S = 0.7;
const EDGE = 5; // настолько молнии можно выйти за зону безопасности (рамка плашки стоит у самого её края)
const FLICKER_S = 0.05; // кадр «дрожания» молнии
const GAP = -1; // молния бежит по самой рамке плашки: плашка стоит у края зоны безопасности, снаружи её низ и
// правый край обрезались бы

// ── детерминированная случайность ───────────────────────────────────────

function rng(seed) {
  let a = (seed >>> 0) || 1;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const seedOf = (...xs) => xs.reduce((h, x) => Math.imul(h ^ (Math.floor(x) + 0x9e3779b9), 0x85ebca6b) >>> 0, 0x1234567);

// ── периметр скошенной плашки ───────────────────────────────────────────

/** Вершины контура плашки, сдвинутого наружу на GAP (по часовой, от левого конца верхней грани). */
function outline(rect, chamfer) {
  const x = rect.x - GAP, y = rect.y - GAP, w = rect.w + 2 * GAP, h = rect.h + 2 * GAP, c = chamfer + GAP * 0.6;
  return [[x + c, y], [x + w - c, y], [x + w, y + c], [x + w, y + h - c], [x + w - c, y + h], [x + c, y + h],
    [x, y + h - c], [x, y + c]];
}

function perimeter(rect, chamfer) {
  const v = outline(rect, chamfer);
  const segs = [];
  let len = 0;
  for (let i = 0; i < v.length; i++) {
    const [ax, ay] = v[i];
    const [bx, by] = v[(i + 1) % v.length];
    const l = Math.hypot(bx - ax, by - ay);
    segs.push({ ax, ay, bx, by, l, s0: len });
    len += l;
  }
  return { segs, len };
}

/** Точка на периметре по длине s (по кругу) и нормаль наружу. */
function at(per, s) {
  const L = per.len;
  s = ((s % L) + L) % L;
  let g = per.segs[per.segs.length - 1];
  for (const sg of per.segs) {
    if (s < sg.s0 + sg.l) {
      g = sg;
      break;
    }
  }
  const k = (s - g.s0) / g.l;
  const dx = (g.bx - g.ax) / g.l, dy = (g.by - g.ay) / g.l;
  // контур по часовой (экранные оси, y вниз): нормаль наружу — (dy, −dx)
  return { x: g.ax + dx * (g.l * k), y: g.ay + dy * (g.l * k), nx: dy, ny: -dx };
}

/** Ломаная вдоль периметра от s0 до s1 (s1 > s0) с шагом ≤ step. */
function along(per, s0, s1, step = 6) {
  const n = Math.max(1, Math.ceil((s1 - s0) / step));
  const pts = [];
  for (let i = 0; i <= n; i++) pts.push(at(per, s0 + ((s1 - s0) * i) / n));
  return pts;
}

/** «Зубцы» молнии: каждую точку, кроме концов, сдвинуть по нормали на ±amp (иногда — дальше наружу). */
function jag(pts, r, amp) {
  return pts.map((p, i) => {
    if (i === 0 || i === pts.length - 1) return { ...p };
    const d = (r() * 2 - 1) * amp + (r() < 0.15 ? amp * 1.2 : 0);
    return { ...p, x: p.x + p.nx * d, y: p.y + p.ny * d };
  });
}

// ── рисование ───────────────────────────────────────────────────────────

function path(ctx, pts) {
  ctx.beginPath();
  ctx.moveTo(pts[0].x, pts[0].y);
  for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i].x, pts[i].y);
}

/** Разряд: ореол, тёмная обводка, яркая сердцевина. a — общая яркость 0…1. */
function bolt(ctx, pts, w, a = 1) {
  if (pts.length < 2 || a <= 0.02) return;
  path(ctx, pts);
  ctx.globalAlpha = 0.16 * a;
  ctx.lineWidth = w + 7;
  ctx.strokeStyle = C.ink;
  ctx.stroke();
  ctx.globalAlpha = 0.85 * a;
  ctx.lineWidth = w + 3;
  ctx.strokeStyle = C.dark;
  ctx.stroke();
  ctx.globalAlpha = a;
  ctx.lineWidth = w;
  ctx.strokeStyle = C.ink;
  ctx.stroke();
  ctx.globalAlpha = 1;
}

/** Ветка от точки p наружу (с наклоном), длиной len. */
function branch(ctx, p, r, len, w, a) {
  const tilt = (r() * 2 - 1) * 0.9;
  const dx = p.nx + -p.ny * tilt, dy = p.ny + p.nx * tilt;
  const n = Math.hypot(dx, dy) || 1;
  const pts = [{ x: p.x, y: p.y }];
  const steps = 3;
  for (let i = 1; i <= steps; i++) {
    const k = (len * i) / steps;
    const j = (r() * 2 - 1) * len * 0.18;
    pts.push({ x: p.x + (dx / n) * k - (dy / n) * j, y: p.y + (dy / n) * k + (dx / n) * j });
  }
  bolt(ctx, pts, w, a);
}

/** Искра: короткий штрих, летит наружу от p; age 0…1 — доля жизни. Вниз и вправо — короче (край зоны безопасности). */
function spark(ctx, p, r, age, a) {
  if (age < 0 || age > 1) return;
  const ang = Math.atan2(p.ny, p.nx) + (r() * 2 - 1) * 1.1;
  let vx = Math.cos(ang), vy = Math.sin(ang);
  if (vy > 0) vy *= 0.45;
  if (vx > 0) vx *= 0.6;
  const dist = 6 + 26 * r();
  const d0 = dist * age, d1 = dist * Math.min(1, age + 0.25);
  const fade = a * (1 - age);
  bolt(ctx, [{ x: p.x + vx * d0, y: p.y + vy * d0 }, { x: p.x + vx * d1, y: p.y + vy * d1 }], 2, fade);
}

// ── треск ───────────────────────────────────────────────────────────────

/** Треск: на каждом кадре несколько коротких дуг в случайных местах рамки, ветки и искры наружу. */
function crackle(ctx, per, t, burst, cycle) {
  const L = per.len;
  const frame = Math.floor(t / FLICKER_S);
  const r = rng(seedOf(cycle, frame, 50));
  const env = Math.sin(Math.PI * Math.min(1, t / burst)); // нарастает и гаснет
  // слабое свечение всей рамки
  bolt(ctx, along(per, 0, L, 12), 2, 0.25 * env);
  const n = 3 + Math.floor(r() * 3);
  for (let i = 0; i < n; i++) {
    const s = r() * L;
    const span = 26 + r() * 50;
    const pts = jag(along(per, s, s + span, 5), r, 3.5);
    const a = env * (0.6 + 0.4 * r());
    bolt(ctx, pts, 2.5, a);
    if (r() < 0.7) branch(ctx, pts[Math.floor(pts.length / 2)], r, 7 + 12 * r(), 2, a * 0.8);
    spark(ctx, pts[pts.length - 1], r, r() * 0.6, a);
  }
}

/** Идёт ли вспышка: { cycle, t } или null (между вспышками). */
export function lightningPhase(now, opts = {}) {
  const period = opts.period ?? PERIOD_S;
  const burst = Math.min(opts.burst ?? BURST_S, period);
  if (!Number.isFinite(now) || now < 0) return null;
  const cycle = Math.floor(now / period);
  const t = now - cycle * period;
  return t < burst ? { cycle, t, burst } : null;
}

/**
 * Молния вокруг плашки rect ({x, y, w, h} — как вернул drawPower). Между вспышками — ничего не рисует, null.
 * opts: chamfer (скос плашки, 9), period / burst (с), bounds ({x, y, w, h}: зона безопасности — за неё не дальше EDGE px).
 * Возвращает прямоугольник, в котором рисовала.
 */
export function drawPlateLightning(ctx, rect, now, opts = {}) {
  const ph = lightningPhase(now, opts);
  if (!ph || !rect) return null;
  const per = perimeter(rect, opts.chamfer ?? 9);
  const pad = 40;
  let box = { x: rect.x - pad, y: rect.y - pad, w: rect.w + 2 * pad, h: rect.h + 2 * pad };
  // зона безопасности + EDGE px: плашка стоит вплотную к её краю, а молния по рамке чуть выступает
  const b = opts.bounds && { x: opts.bounds.x - EDGE, y: opts.bounds.y - EDGE, w: opts.bounds.w + 2 * EDGE, h: opts.bounds.h + 2 * EDGE };
  if (b) {
    const x0 = Math.max(box.x, b.x), y0 = Math.max(box.y, b.y);
    box = { x: x0, y: y0, w: Math.min(box.x + box.w, b.x + b.w) - x0, h: Math.min(box.y + box.h, b.y + b.h) - y0 };
  }
  ctx.save();
  ctx.beginPath();
  ctx.rect(box.x, box.y, box.w, box.h);
  ctx.clip();
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  crackle(ctx, per, ph.t, ph.burst, ph.cycle);
  ctx.restore();
  return box;
}
