// Минимальный отладочный HUD — запасной вариант, если web/display/ui/index.js (агент C)
// отсутствует или упал. Тот же API, что в docs/contracts/display.md.

export const compassStyles = [{ id: "debug", name: "Debug" }];

export async function loadUi() {}

const FONT = '"DejaVu Sans Mono", "Consolas", monospace';

function plate(ctx, x, y, w, h) {
  ctx.fillStyle = "rgba(0,0,0,0.72)";
  ctx.fillRect(x, y, w, h);
  ctx.strokeStyle = "rgba(255,255,255,0.55)";
  ctx.lineWidth = 2;
  ctx.strokeRect(x + 1, y + 1, w - 2, h - 2);
}

function text(ctx, s, x, y, size, align = "left") {
  ctx.font = `bold ${size}px ${FONT}`;
  ctx.textAlign = align;
  ctx.textBaseline = "top";
  ctx.lineWidth = 4;
  ctx.strokeStyle = "#000";
  ctx.strokeText(s, x, y);
  ctx.fillStyle = "#e8f0e0";
  ctx.fillText(s, x, y);
}

const RUMBS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];

export function drawHud(ctx, state) {
  const v = state.view;
  const s = state.canvas.safe;
  const units = state.settings?.display?.speed_units || "kmh";

  // скорость — слева снизу
  plate(ctx, s.x, s.y + s.h - 84, 190, 84);
  const sp = v.speedMps == null ? "--" : units === "mps" ? v.speedMps.toFixed(1) : String(Math.round(v.speedMps * 3.6));
  text(ctx, sp, s.x + 12, s.y + s.h - 80, 52);
  text(ctx, units === "mps" ? "m/s" : "km/h", s.x + 182, s.y + s.h - 34, 25, "right");

  // курс — сверху по центру + стрелка севера
  const cx = state.canvas.w / 2;
  plate(ctx, cx - 120, s.y, 240, 64);
  const hd = v.headingDeg == null ? "---°" : String(Math.round(v.headingDeg) % 360).padStart(3, "0") + "°";
  const rumb = v.headingDeg == null ? "" : RUMBS[Math.round(v.headingDeg / 22.5) % 16];
  text(ctx, hd, cx - 108, s.y + 8, 42);
  text(ctx, rumb, cx + 60, s.y + 16, 28, "left");

  // стрелка «север» (куда на экране смотрит север)
  const nx = s.x + s.w - 44, ny = s.y + 44;
  plate(ctx, nx - 40, ny - 40, 80, 80);
  ctx.save();
  ctx.translate(nx, ny);
  ctx.rotate((-(v.bearingDeg || 0) * Math.PI) / 180);
  ctx.beginPath();
  ctx.moveTo(0, -30);
  ctx.lineTo(12, 8);
  ctx.lineTo(0, 2);
  ctx.lineTo(-12, 8);
  ctx.closePath();
  ctx.fillStyle = "#fff";
  ctx.strokeStyle = "#000";
  ctx.lineWidth = 3;
  ctx.fill();
  ctx.stroke();
  ctx.restore();
  text(ctx, "N", nx, ny + 10, 25, "center");

  // индикаторы — справа снизу
  const flags = [];
  flags.push(v.headingSource === "none" ? "HDG --" : v.headingSource.toUpperCase());
  if (v.recording) flags.push("● REC");
  if (v.internet === false) flags.push("NO INTERNET");
  flags.push("Z" + v.zoom.toFixed(1));
  plate(ctx, s.x + s.w - 230, s.y + s.h - 40 * flags.length - 8, 230, 40 * flags.length + 8);
  flags.forEach((f, i) => text(ctx, f, s.x + s.w - 12, s.y + s.h - 40 * flags.length + i * 40, 26, "right"));

  // крупные предупреждения — по центру
  const warn = !v.connected ? "NO LINK" : !v.hasFix ? "NO GPS" : null;
  if (warn && Math.floor(state.now * 2) % 2 === 0) {
    plate(ctx, cx - 150, state.canvas.h * 0.38, 300, 60);
    text(ctx, warn, cx, state.canvas.h * 0.38 + 12, 36, "center");
  }
}

export function drawVehicle(ctx, x, y, rotationDeg, state) {
  ctx.save();
  ctx.translate(x, y);
  ctx.rotate((rotationDeg * Math.PI) / 180);
  if (!state.view.hasFix) ctx.globalAlpha = Math.floor(state.now * 2) % 2 ? 0.25 : 0.6;
  ctx.beginPath();
  ctx.moveTo(0, -26);
  ctx.lineTo(17, 20);
  ctx.lineTo(0, 11);
  ctx.lineTo(-17, 20);
  ctx.closePath();
  ctx.lineJoin = "round";
  ctx.lineWidth = 7;
  ctx.strokeStyle = "#000";
  ctx.stroke();
  ctx.fillStyle = "#f2f2e8";
  ctx.fill();
  ctx.lineWidth = 2;
  ctx.strokeStyle = "#333";
  ctx.stroke();
  ctx.restore();
}

export function drawTrail(ctx, points, state) {
  if (points.length < 2) return;
  ctx.save();
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.beginPath();
  ctx.moveTo(points[0].x, points[0].y);
  for (let i = 1; i < points.length; i++) ctx.lineTo(points[i].x, points[i].y);
  ctx.strokeStyle = "rgba(0,0,0,0.6)";
  ctx.lineWidth = 8;
  ctx.stroke();
  ctx.strokeStyle = "rgba(255,255,230,0.75)";
  ctx.lineWidth = 3;
  ctx.setLineDash([10, 8]);
  ctx.stroke();
  ctx.restore();
}

/** Отладочная статистика (?debug=1): fps, тайлы, загрузки. */
export function drawStats(ctx, lines) {
  ctx.save();
  ctx.font = `bold 14px ${FONT}`;
  ctx.textAlign = "left";
  ctx.textBaseline = "top";
  const w = 330, h = 18 * lines.length + 8;
  ctx.fillStyle = "rgba(0,0,0,0.7)";
  ctx.fillRect(4, 4, w, h);
  ctx.fillStyle = "#9f9";
  lines.forEach((l, i) => ctx.fillText(l, 10, 8 + i * 18));
  ctx.restore();
}
