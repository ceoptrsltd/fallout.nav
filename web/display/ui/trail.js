// След машины: две колеи «следов шин» (пунктир-протектор), затухающие по возрасту точки.

import { setting } from "./theme.js";

const BUCKETS = 6; // ступени прозрачности (каждая — одна пара stroke-вызовов)
const TRACK = 8.5; // полуширина колеи, px холста

/**
 * points = [{x, y, age_s}] в координатах холста, в порядке движения (старые → новые или наоборот —
 * порядок неважен для вида, важна лишь связность соседних точек).
 */
export function drawTrail(ctx, points, state) {
  if (!points || points.length < 2) return;
  if (setting(state, "display.show_trail", true) === false) return;
  const maxAge = Math.max(1, setting(state, "display.trail_seconds", 60));
  const n = points.length;

  // нормали в точках (усреднённые по соседним отрезкам)
  const nx = new Float32Array(n);
  const ny = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const a = points[Math.max(0, i - 1)];
    const b = points[Math.min(n - 1, i + 1)];
    let dx = b.x - a.x;
    let dy = b.y - a.y;
    const L = Math.hypot(dx, dy) || 1;
    nx[i] = -dy / L;
    ny[i] = dx / L;
  }
  const bucketOf = (p) => {
    const f = 1 - Math.min(1, Math.max(0, (p.age_s ?? 0) / maxAge));
    return f <= 0 ? -1 : Math.min(BUCKETS - 1, Math.floor(f * BUCKETS));
  };

  ctx.save();
  ctx.lineJoin = "round";
  // проходим по участкам одной «ступени» — внутри участка пунктир непрерывный
  let i = 0;
  while (i < n - 1) {
    const b = bucketOf(points[i]);
    let j = i + 1;
    while (j < n - 1 && bucketOf(points[j]) === b) j++;
    if (b >= 0) {
      const alpha = Math.pow((b + 1) / BUCKETS, 1.3);
      const path = new Path2D();
      for (const side of [-1, 1]) {
        for (let k = i; k <= j; k++) {
          const p = points[k];
          const x = p.x + nx[k] * TRACK * side;
          const y = p.y + ny[k] * TRACK * side;
          if (k === i) path.moveTo(x, y);
          else path.lineTo(x, y);
        }
      }
      // тёмная подложка колеи
      ctx.setLineDash([]);
      ctx.lineCap = "round";
      ctx.lineWidth = 6.5;
      ctx.strokeStyle = `rgba(0,0,0,${(0.5 * alpha).toFixed(3)})`;
      ctx.stroke(path);
      // светлый «протектор»
      ctx.setLineDash([4, 3.5]);
      ctx.lineCap = "butt";
      ctx.lineWidth = 3.5;
      ctx.strokeStyle = `rgba(236,247,216,${(0.85 * alpha).toFixed(3)})`;
      ctx.stroke(path);
    }
    i = j;
  }
  ctx.restore();
}
