// Компас HUD: три варианта дизайна (v1, v2, v3).
// Ориентация:
//   heading_up — карта повёрнута на bearingDeg: шкала вращается (вверху — курс), курсовая черта неподвижна;
//   north_up   — шкала неподвижна (С вверху), вращается/едет стрелка курса.
// Общая формула: шкала повёрнута на −bearingDeg; стрелка курса — на (headingDeg − bearingDeg).

import {
  C, CARDINALS, DEG, blit, blitCentered, chamferPath, fixedDigits, fontLabel, fontNum, fontStencil,
  norm360, outlinedText, plateSprite, point16, rhumb, rivet, screw, sprite, textWidth,
} from "./theme.js";

export const compassStyles = [
  { id: "v1", name: "Riveted dial", description: "Round gauge with a riveted steel bezel, digital heading in the centre" },
  { id: "v2", name: "Heading tape", description: "Pip-Boy style heading tape across the top" },
  { id: "v3", name: "Brass compass", description: "Brass housing, compass rose and needle, engraved name plate" },
];

// ── общие части ─────────────────────────────────────────────────────────

function readHeading(state) {
  const v = state?.view ?? {};
  const h = v.headingDeg;
  const known = typeof h === "number" && Number.isFinite(h) && v.headingSource !== "none";
  const bearing = Number.isFinite(v.bearingDeg) ? v.bearingDeg : 0;
  const orientation =
    v.orientation ?? state?.settings?.display?.orientation ?? state?.telemetry?.display?.orientation ?? "heading_up";
  // курс иконки машины (быстрый); если B его не передал — цифровой курс
  const vehicle = known && Number.isFinite(v.vehicleHeadingDeg) ? v.vehicleHeadingDeg : known ? h : null;
  return {
    known,
    heading: known ? norm360(h) : null,
    bearing,
    northUp: orientation === "north_up",
    source: known ? v.headingSource ?? "gps" : "none",
    // угол указателя машины относительно верхней метки шкалы, −180…+180
    rel: vehicle == null ? null : norm360(vehicle - bearing + 180) - 180,
  };
}

function headingStr(hd) {
  if (hd.heading == null) return "---";
  return String(Math.round(hd.heading) % 360).padStart(3, "0");
}

const TAG_TEXT = { gps: "GPS", mag: "MAG", blend: "BLEND", none: "NO HDG" };

/**
 * Плашка источника курса. Форма тоже кодирует источник (для ч/б):
 * GPS — светлая заливка, MAG — тёмная с рамкой, BLEND — наполовину, нет курса — светлая «аварийная».
 * (x, y) — центр. Возвращает ширину.
 */
export function drawSourceTag(ctx, x, y, source) {
  const text = TAG_TEXT[source] ?? String(source).toUpperCase();
  const font = fontLabel(25);
  const w = Math.ceil(textWidth(ctx, font, text) + 22);
  const h = 32;
  const x0 = Math.round(x - w / 2);
  const y0 = Math.round(y - h / 2);
  chamferPath(ctx, x0, y0, w, h, 6);
  const light = source === "gps" || source === "none";
  ctx.fillStyle = light ? (source === "none" ? C.hazard : C.ink) : C.plateSolid;
  ctx.fill();
  if (source === "blend") {
    ctx.save();
    ctx.clip();
    ctx.fillStyle = C.ink;
    ctx.beginPath();
    ctx.moveTo(x0, y0 + h);
    ctx.lineTo(x0 + w * 0.42, y0 + h);
    ctx.lineTo(x0 + w * 0.42 - h, y0);
    ctx.lineTo(x0, y0);
    ctx.fill();
    ctx.restore();
  }
  ctx.lineWidth = 3;
  ctx.strokeStyle = C.dark;
  chamferPath(ctx, x0, y0, w, h, 6);
  ctx.stroke();
  if (!light) {
    ctx.lineWidth = 2;
    ctx.strokeStyle = C.ink;
    chamferPath(ctx, x0 + 2.5, y0 + 2.5, w - 5, h - 5, 5);
    ctx.stroke();
  }
  if (source === "blend") {
    // половина — светлая: текст с обводкой, чтобы читался на обеих половинах
    outlinedText(ctx, text, x, y, font, { fill: C.ink, lw: 4 });
  } else {
    outlinedText(ctx, text, x, y, font, { fill: light ? C.dark : C.ink, lw: light ? 0 : 4 });
  }
  return w;
}

/** Треугольный указатель: остриём к центру; (cx,cy) — центр шкалы, angRad — от верха по часовой. */
function pointerTri(ctx, cx, cy, angRad, rTip, rBase, halfW, fill, outline = C.dark) {
  ctx.save();
  ctx.translate(cx, cy);
  ctx.rotate(angRad);
  ctx.beginPath();
  ctx.moveTo(0, -rTip);
  ctx.lineTo(halfW, -rBase);
  ctx.lineTo(-halfW, -rBase);
  ctx.closePath();
  ctx.lineJoin = "round";
  ctx.lineWidth = 5;
  ctx.strokeStyle = outline;
  ctx.stroke();
  ctx.fillStyle = fill;
  ctx.fill();
  ctx.restore();
}

/**
 * «Бегунок» курса машины на круговой шкале: сплошной пятиугольник остриём к центру.
 * angRad — от верха по часовой; rOut — внешний край (у делений).
 */
function headingBug(ctx, cx, cy, angRad, rOut, depth = 21, halfW = 10) {
  ctx.save();
  ctx.translate(cx, cy);
  ctx.rotate(angRad);
  const y0 = -rOut;
  const y1 = -rOut + depth - 7;
  const y2 = -rOut + depth;
  ctx.beginPath();
  ctx.moveTo(-halfW, y0);
  ctx.lineTo(halfW, y0);
  ctx.lineTo(halfW, y1);
  ctx.lineTo(0, y2);
  ctx.lineTo(-halfW, y1);
  ctx.closePath();
  ctx.lineJoin = "round";
  ctx.lineWidth = 4.5;
  ctx.strokeStyle = C.dark;
  ctx.stroke();
  ctx.fillStyle = C.ink;
  ctx.fill();
  ctx.restore();
}

/** Деления по кругу: 5° / 10° / 30°. skipCardinal — не рисовать на 0/90/180/270 (там буквы). */
function ringTicks(c, cx, cy, rOut, { l5 = 6, l10 = 11, l30 = 17, skipCardinal = false, color = C.ink } = {}) {
  c.lineCap = "butt";
  for (let d = 0; d < 360; d += 5) {
    if (skipCardinal && (d % 90 === 0 || d % 90 === 5 || d % 90 === 85)) continue;
    const a = d * DEG;
    const len = d % 30 === 0 ? l30 : d % 10 === 0 ? l10 : l5;
    const w = d % 30 === 0 ? 3.5 : d % 10 === 0 ? 2.5 : 2;
    const s = Math.sin(a);
    const k = -Math.cos(a);
    c.beginPath();
    c.moveTo(cx + s * rOut, cy + k * rOut);
    c.lineTo(cx + s * (rOut - len), cy + k * (rOut - len));
    c.lineWidth = w;
    c.strokeStyle = d % 10 === 0 ? color : "rgba(236,247,216,0.78)";
    c.stroke();
  }
}

// ── v1 · Циферблат ──────────────────────────────────────────────────────

const V1 = { R: 92, face: 79 };

function v1Bezel() {
  const { R, face } = V1;
  const S = 2 * R + 8;
  return sprite("v1-bezel", S, S, (c) => {
    const cx = S / 2;
    const cy = S / 2;
    // корпус: тёмный обод + металлическое кольцо с «шлифовкой»
    c.beginPath();
    c.arc(cx, cy, R + 2, 0, 2 * Math.PI);
    c.fillStyle = C.dark;
    c.fill();
    const g = c.createLinearGradient(cx - R, cy - R, cx + R, cy + R);
    g.addColorStop(0, C.metalHi);
    g.addColorStop(0.35, C.metal);
    g.addColorStop(0.55, C.metalLo);
    g.addColorStop(0.8, C.metal);
    g.addColorStop(1, C.metalLo);
    c.beginPath();
    c.arc(cx, cy, R, 0, 2 * Math.PI);
    c.arc(cx, cy, face + 1, 0, 2 * Math.PI, true);
    c.fillStyle = g;
    c.fill("evenodd");
    // ржавые пятна на ободе
    c.save();
    c.beginPath();
    c.arc(cx, cy, R, 0, 2 * Math.PI);
    c.arc(cx, cy, face + 1, 0, 2 * Math.PI, true);
    c.clip("evenodd");
    c.fillStyle = "rgba(140,90,50,0.45)";
    for (const [a, r] of [[0.7, 7], [2.6, 5], [4.1, 8], [5.4, 4]]) {
      c.beginPath();
      c.arc(cx + Math.sin(a) * (R - 6), cy - Math.cos(a) * (R - 6), r, 0, 2 * Math.PI);
      c.fill();
    }
    c.restore();
    // циферблат
    c.beginPath();
    c.arc(cx, cy, face, 0, 2 * Math.PI);
    c.fillStyle = "rgba(9,11,7,0.80)";
    c.fill();
    c.lineWidth = 2.5;
    c.strokeStyle = C.dark;
    c.stroke();
    // заклёпки (между ними — места под стороны света не нужны: обод узкий)
    for (let i = 0; i < 8; i++) {
      const a = (i * 45 + 22.5) * DEG;
      rivet(c, cx + Math.sin(a) * (R - 6.5), cy - Math.cos(a) * (R - 6.5), 3);
    }
    // внутреннее кольцо под центральным табло
    c.beginPath();
    c.arc(cx, cy, 44, 0, 2 * Math.PI);
    c.lineWidth = 2;
    c.strokeStyle = "rgba(236,247,216,0.22)";
    c.stroke();
  });
}

function v1Scale() {
  const { face } = V1;
  const S = 2 * face + 4;
  return sprite("v1-scale", S, S, (c) => {
    const cx = S / 2;
    const cy = S / 2;
    ringTicks(c, cx, cy, face - 2, { l5: 7, l10: 12, l30: 17, skipCardinal: true });
  });
}

function drawV1(ctx, state, safe) {
  const { R, face } = V1;
  const cx = Math.round(safe.x + safe.w - R - 3);
  const cy = Math.round(safe.y + R + 3);
  const hd = readHeading(state);
  const rot = -hd.bearing * DEG;
  const dim = hd.known ? 1 : 0.5;

  blitCentered(ctx, v1Bezel(), cx, cy);
  blitCentered(ctx, v1Scale(), cx, cy, rot, dim);

  // стороны света — вертикально (читаемость важнее «настоящего» вращения)
  const rL = face - 16;
  for (let i = 0; i < 4; i++) {
    const a = rot + i * 90 * DEG;
    const x = cx + Math.sin(a) * rL;
    const y = cy - Math.cos(a) * rL;
    if (i === 0) {
      // север: светлый «жетон» с тёмной буквой + треугольник на ободе
      ctx.beginPath();
      ctx.arc(x, y, 16, 0, 2 * Math.PI);
      ctx.fillStyle = C.accent;
      ctx.fill();
      ctx.lineWidth = 3;
      ctx.strokeStyle = C.dark;
      ctx.stroke();
      outlinedText(ctx, CARDINALS[0], x, y, fontStencil(26), { fill: C.dark, lw: 0 });
    } else {
      ctx.globalAlpha = dim;
      outlinedText(ctx, CARDINALS[i], x, y, fontStencil(28), { lw: 5 });
      ctx.globalAlpha = 1;
    }
  }
  pointerTri(ctx, cx, cy, rot, face - 8, R + 3, 8, C.accent);

  // курсовая черта (heading_up) или стрелка курса (north_up)
  if (!hd.northUp) {
    // неподвижная верхняя метка на ободе + бегунок машины на шкале (на повороте уходит вбок)
    pointerTri(ctx, cx, cy, 0, face - 1, R + 5, 9, C.ink);
    if (hd.rel != null) headingBug(ctx, cx, cy, hd.rel * DEG, face - 1);
  } else if (hd.rel != null) {
    pointerTri(ctx, cx, cy, hd.rel * DEG, face - 16, R + 6, 12, C.ink);
  }

  // центральное табло: курс + румб
  const fN = fontNum(38);
  const cell = Math.round(textWidth(ctx, fN, "0"));
  const digitsW = cell * 3;
  const xr = cx + digitsW / 2 - 3;
  fixedDigits(ctx, headingStr(hd), xr, cy - 10, fN, cell, { fill: hd.known ? C.ink : C.inkDim, lw: 5 });
  outlinedText(ctx, "°", xr + 6, cy - 21, fontNum(24, 600), { lw: 4 });
  outlinedText(ctx, hd.known ? rhumb(hd.heading) : "?", cx, cy + 22, fontLabel(25), {
    fill: hd.known ? C.ink : C.inkDim,
    lw: 5,
  });

  drawSourceTag(ctx, cx, cy + R + 6, hd.source);
  return { x: cx - R - 2, y: cy - R - 2, w: 2 * R + 4, h: 2 * R + 26 };
}

// ── v2 · Лента курса ────────────────────────────────────────────────────

const V2 = { W: 440, H: 54, cap: 20, ppd: 3.6, from: -75, to: 435 };

function v2Strip() {
  const { H, ppd, from, to } = V2;
  const w = (to - from) * ppd;
  return sprite("v2-strip", w, H, (c) => {
    for (let d = from; d <= to; d += 5) {
      const x = (d - from) * ppd;
      const n = norm360(d);
      const len = n % 30 === 0 ? 19 : n % 10 === 0 ? 12 : 7;
      c.beginPath();
      c.moveTo(x, H - 2);
      c.lineTo(x, H - 2 - len);
      c.lineWidth = n % 30 === 0 ? 3.5 : n % 10 === 0 ? 2.5 : 2;
      c.strokeStyle = n % 10 === 0 ? C.ink : "rgba(236,247,216,0.78)";
      c.stroke();
      if (n % 30 === 0) {
        const y = 15;
        if (n === 0) {
          chamferPath(c, x - 16, y - 14, 32, 28, 5);
          c.fillStyle = C.accent;
          c.fill();
          c.lineWidth = 2.5;
          c.strokeStyle = C.dark;
          c.stroke();
          outlinedText(c, CARDINALS[0], x, y, fontStencil(26), { fill: C.dark, lw: 0 });
        } else if (n % 90 === 0) {
          outlinedText(c, CARDINALS[n / 90], x, y, fontStencil(28), { lw: 0 });
        } else {
          outlinedText(c, String(n), x, y, fontNum(26, 600), { lw: 0 });
        }
      }
    }
  });
}

/** Неподвижная шкала для north_up: −180…+180, N в центре. */
function v2Fixed() {
  const { W, H, cap } = V2;
  const inner = W - 2 * cap;
  const margin = 18;
  const ppd = (inner - 2 * margin) / 360;
  const s = sprite("v2-fixed", inner, H, (c) => {
    const x0 = inner / 2;
    for (let d = -180; d <= 180; d += 5) {
      const x = x0 + d * ppd;
      const n = norm360(d);
      const len = n % 30 === 0 ? 17 : n % 10 === 0 ? 11 : 6;
      c.beginPath();
      c.moveTo(x, H - 2);
      c.lineTo(x, H - 2 - len);
      c.lineWidth = n % 30 === 0 ? 3 : 2;
      c.strokeStyle = n % 10 === 0 ? C.ink : "rgba(236,247,216,0.7)";
      c.stroke();
    }
    for (let d = -180; d <= 180; d += 45) {
      const x = x0 + d * ppd;
      const n = norm360(d);
      const y = 15;
      if (n === 0) {
        chamferPath(c, x - 16, y - 14, 32, 28, 5);
        c.fillStyle = C.accent;
        c.fill();
        c.lineWidth = 2.5;
        c.strokeStyle = C.dark;
        c.stroke();
        outlinedText(c, CARDINALS[0], x, y, fontStencil(26), { fill: C.dark, lw: 0 });
      } else if (n % 90 === 0) {
        outlinedText(c, CARDINALS[n / 90], x, y, fontStencil(28), { lw: 0 });
      } else {
        outlinedText(c, point16(n / 22.5), x, y, fontLabel(25), { fill: C.ink, lw: 0 });
      }
    }
  });
  return { s, ppd };
}

function v2Frame() {
  const { W, H, cap } = V2;
  return sprite("v2-frame", W, H + 6, (c) => {
    // окно ленты
    chamferPath(c, cap - 2, 1.5, W - 2 * cap + 4, H + 1, 4);
    c.fillStyle = "rgba(9,11,7,0.78)";
    c.fill();
    c.lineWidth = 3;
    c.strokeStyle = C.dark;
    c.stroke();
    // торцевые «заглушки» из металла с заклёпками
    for (const x0 of [0, W - cap]) {
      const g = c.createLinearGradient(x0, 0, x0 + cap, 0);
      g.addColorStop(0, C.metalLo);
      g.addColorStop(0.5, C.metalHi);
      g.addColorStop(1, C.metalLo);
      chamferPath(c, x0 + 1.5, 1.5, cap - 3, H + 3, 5);
      c.fillStyle = g;
      c.fill();
      c.lineWidth = 3;
      c.strokeStyle = C.dark;
      c.stroke();
      rivet(c, x0 + cap / 2, 12, 3);
      rivet(c, x0 + cap / 2, H - 8, 3);
    }
  });
}

function drawV2(ctx, state, safe) {
  const { W, H, cap, ppd, from } = V2;
  const hd = readHeading(state);
  const cx = Math.round(safe.x + safe.w / 2);
  const x0 = cx - W / 2;
  const y0 = Math.round(safe.y);
  const dim = hd.known ? 1 : 0.5;

  blit(ctx, v2Frame(), x0, y0);
  const ix = x0 + cap;
  const iw = W - 2 * cap;

  ctx.save();
  ctx.beginPath();
  ctx.rect(ix, y0, iw, H + 2);
  ctx.clip();
  ctx.globalAlpha = dim;
  let px = null; // x указателя курса машины
  const lim = iw / 2 - 6;
  if (!hd.northUp) {
    const strip = v2Strip();
    const center = norm360(hd.bearing);
    if (strip) ctx.drawImage(strip.canvas, cx - (center - from) * ppd, y0, strip.w, strip.h);
    if (hd.rel != null) px = cx + Math.max(-lim, Math.min(lim, hd.rel * ppd));
  } else {
    const { s, ppd: p2 } = v2Fixed();
    blit(ctx, s, ix, y0);
    if (hd.rel != null) px = ix + iw / 2 + Math.max(-lim, Math.min(lim, hd.rel * p2));
  }
  ctx.globalAlpha = 1;
  // указатель: вертикальная черта через ленту
  if (px != null) {
    ctx.fillStyle = C.dark;
    ctx.fillRect(px - 3.5, y0, 7, H + 2);
    ctx.fillStyle = C.ink;
    ctx.fillRect(px - 1.5, y0 + 1, 3, H);
  }
  ctx.restore();

  // heading_up: неподвижная центральная метка (зубец сверху ленты)
  if (!hd.northUp) {
    ctx.beginPath();
    ctx.moveTo(cx - 9, y0 + 1);
    ctx.lineTo(cx + 9, y0 + 1);
    ctx.lineTo(cx, y0 + 12);
    ctx.closePath();
    ctx.lineJoin = "round";
    ctx.lineWidth = 4;
    ctx.strokeStyle = C.dark;
    ctx.stroke();
    ctx.fillStyle = C.ink;
    ctx.fill();
  }
  // треугольник под лентой — у указателя машины
  if (px != null) {
    ctx.beginPath();
    ctx.moveTo(px, y0 + H - 9);
    ctx.lineTo(px + 11, y0 + H + 8);
    ctx.lineTo(px - 11, y0 + H + 8);
    ctx.closePath();
    ctx.lineJoin = "round";
    ctx.lineWidth = 5;
    ctx.strokeStyle = C.dark;
    ctx.stroke();
    ctx.fillStyle = C.ink;
    ctx.fill();
  }

  // табло под центром: курс + румб; источник — справа
  const bw = 182;
  const bh = 46;
  const by = y0 + H + 6;
  blit(ctx, plateSprite(bw, bh, { chamfer: 7, rivets: false }), cx - bw / 2, by);
  const fN = fontNum(38);
  const cell = Math.round(textWidth(ctx, fN, "0") + 1);
  const xr = cx - bw / 2 + 12 + cell * 3;
  fixedDigits(ctx, headingStr(hd), xr, by + bh / 2, fN, cell, { fill: hd.known ? C.ink : C.inkDim, lw: 4 });
  outlinedText(ctx, "°", xr + 7, by + bh / 2 - 10, fontNum(24, 600), { lw: 3 });
  outlinedText(ctx, hd.known ? rhumb(hd.heading) : "?", cx + bw / 2 - 40, by + bh / 2, fontLabel(25), {
    fill: hd.known ? C.ink : C.inkDim,
    lw: 4,
  });
  const tagW = textWidth(ctx, fontLabel(25), TAG_TEXT[hd.source] ?? "GPS") + 22;
  drawSourceTag(ctx, cx + bw / 2 + 8 + tagW / 2, by + bh / 2, hd.source);
  return { x: x0, y: y0, w: W, h: H + 6 + bh };
}

// ── v3 · Бронзовый компас ───────────────────────────────────────────────

const V3 = { R: 94, face: 78 };

function v3Bezel() {
  const { R, face } = V3;
  const S = 2 * R + 8;
  return sprite("v3-bezel", S, S, (c) => {
    const cx = S / 2;
    const cy = S / 2;
    c.beginPath();
    c.arc(cx, cy, R + 2.5, 0, 2 * Math.PI);
    c.fillStyle = C.dark;
    c.fill();
    const g = c.createRadialGradient(cx - R * 0.35, cy - R * 0.45, R * 0.2, cx, cy, R);
    g.addColorStop(0, C.brassHi);
    g.addColorStop(0.75, C.brass);
    g.addColorStop(0.93, C.brassLo);
    g.addColorStop(1, "#3a2c12");
    c.beginPath();
    c.arc(cx, cy, R, 0, 2 * Math.PI);
    c.arc(cx, cy, face + 2, 0, 2 * Math.PI, true);
    c.fillStyle = g;
    c.fill("evenodd");
    // накатка по внешнему краю
    c.strokeStyle = "rgba(40,28,8,0.55)";
    c.lineWidth = 1.5;
    for (let d = 0; d < 360; d += 4) {
      const a = d * DEG;
      c.beginPath();
      c.moveTo(cx + Math.sin(a) * (R - 1), cy - Math.cos(a) * (R - 1));
      c.lineTo(cx + Math.sin(a) * (R - 5), cy - Math.cos(a) * (R - 5));
      c.stroke();
    }
    // зелёная патина (в ч/б — просто тёмные пятна)
    c.fillStyle = "rgba(60,90,60,0.35)";
    for (const [a, r] of [[1.1, 6], [3.3, 4], [4.9, 7]]) {
      c.beginPath();
      c.arc(cx + Math.sin(a) * (R - 9), cy - Math.cos(a) * (R - 9), r, 0, 2 * Math.PI);
      c.fill();
    }
    // стекло/циферблат
    c.beginPath();
    c.arc(cx, cy, face + 2, 0, 2 * Math.PI);
    c.fillStyle = "rgba(14,12,6,0.82)";
    c.fill();
    c.lineWidth = 3;
    c.strokeStyle = C.dark;
    c.stroke();
    for (let i = 0; i < 4; i++) {
      const a = (45 + i * 90) * DEG;
      screw(c, cx + Math.sin(a) * (R - 8), cy - Math.cos(a) * (R - 8), 4.2, 0.4 + i);
    }
  });
}

function v3Card() {
  const { face } = V3;
  const S = 2 * face + 4;
  return sprite("v3-card", S, S, (c) => {
    const cx = S / 2;
    const cy = S / 2;
    ringTicks(c, cx, cy, face - 1, { l5: 6, l10: 10, l30: 15 });
    c.beginPath();
    c.arc(cx, cy, face - 17, 0, 2 * Math.PI);
    c.lineWidth = 1.5;
    c.strokeStyle = "rgba(236,247,216,0.35)";
    c.stroke();
    // роза ветров: 4 длинных луча + 4 коротких; половинки светлая/тёмная — объём
    const ray = (deg, len, half, light, darkSide) => {
      const a = deg * DEG;
      const tx = cx + Math.sin(a) * len;
      const ty = cy - Math.cos(a) * len;
      const pa = a + Math.PI / 2;
      const lx = cx + Math.sin(pa) * half;
      const ly = cy - Math.cos(pa) * half;
      const rx = cx - Math.sin(pa) * half;
      const ry = cy + Math.cos(pa) * half;
      c.beginPath();
      c.moveTo(cx, cy);
      c.lineTo(tx, ty);
      c.lineTo(lx, ly);
      c.closePath();
      c.fillStyle = light;
      c.fill();
      c.beginPath();
      c.moveTo(cx, cy);
      c.lineTo(tx, ty);
      c.lineTo(rx, ry);
      c.closePath();
      c.fillStyle = darkSide;
      c.fill();
      c.beginPath();
      c.moveTo(lx, ly);
      c.lineTo(tx, ty);
      c.lineTo(rx, ry);
      c.lineJoin = "round";
      c.lineWidth = 2;
      c.strokeStyle = C.dark;
      c.stroke();
    };
    for (const d of [45, 135, 225, 315]) ray(d, 24, 6, "#9a9a88", "#4a4a40");
    for (const d of [90, 180, 270]) ray(d, 34, 8, C.ink, "#6f7266");
    ray(0, 36, 9, C.accent, "#a87a1c");
    // север на картушке — ещё и формой: светлый треугольник на месте метки 0°
    c.beginPath();
    c.moveTo(cx, cy - face + 1);
    c.lineTo(cx + 8, cy - face + 17);
    c.lineTo(cx - 8, cy - face + 17);
    c.closePath();
    c.lineJoin = "round";
    c.lineWidth = 4;
    c.strokeStyle = C.dark;
    c.stroke();
    c.fillStyle = C.accent;
    c.fill();
    // буквы — радиально, «вверх» наружу, как на настоящей картушке
    for (let i = 0; i < 4; i++) {
      const a = i * 90 * DEG;
      const r = face - 29;
      c.save();
      c.translate(cx + Math.sin(a) * r, cy - Math.cos(a) * r);
      c.rotate(a);
      if (i === 0) {
        outlinedText(c, CARDINALS[0], 0, 0, fontStencil(30), { fill: C.accent, lw: 5 });
      } else {
        outlinedText(c, CARDINALS[i], 0, 0, fontStencil(27), { lw: 5 });
      }
      c.restore();
    }
  });
}

function v3Needle() {
  return sprite("v3-needle", 40, 170, (c) => {
    const cx = 20;
    const cy = 85;
    // передняя (курсовая) половина — светлая, хвост — тёмный
    c.beginPath();
    c.moveTo(cx, cy - 74);
    c.lineTo(cx + 9, cy);
    c.lineTo(cx - 9, cy);
    c.closePath();
    c.lineJoin = "round";
    c.lineWidth = 5;
    c.strokeStyle = C.dark;
    c.stroke();
    c.fillStyle = C.ink;
    c.fill();
    c.beginPath();
    c.moveTo(cx, cy - 74);
    c.lineTo(cx + 9, cy);
    c.lineTo(cx, cy);
    c.closePath();
    c.fillStyle = "#b9bcae";
    c.fill();
    c.beginPath();
    c.moveTo(cx - 9, cy);
    c.lineTo(cx + 9, cy);
    c.lineTo(cx + 4, cy + 34);
    c.lineTo(cx - 4, cy + 34);
    c.closePath();
    c.lineWidth = 4;
    c.strokeStyle = C.dark;
    c.stroke();
    c.fillStyle = C.metalLo;
    c.fill();
    screw(c, cx, cy, 7, 0.8);
  });
}

function v3Nameplate(w, h) {
  return sprite("v3-nameplate", w, h, (c) => {
    chamferPath(c, 1.5, 1.5, w - 3, h - 3, 8);
    const g = c.createLinearGradient(0, 0, 0, h);
    g.addColorStop(0, C.brassHi);
    g.addColorStop(0.5, C.brass);
    g.addColorStop(1, "#8d6f30");
    c.fillStyle = g;
    c.fill();
    c.lineWidth = 3;
    c.strokeStyle = C.dark;
    c.stroke();
    screw(c, 11, h / 2, 4, 0.3);
    screw(c, w - 11, h / 2, 4, 1.2);
  });
}

function drawV3(ctx, state, safe) {
  const { R, face } = V3;
  const cx = Math.round(safe.x + safe.w - R - 3);
  const cy = Math.round(safe.y + R + 3);
  const hd = readHeading(state);
  const rot = -hd.bearing * DEG;

  blitCentered(ctx, v3Bezel(), cx, cy);
  blitCentered(ctx, v3Card(), cx, cy, rot, hd.known ? 1 : 0.55);
  if (!hd.northUp) {
    // неподвижная латунная верхняя метка на ободе
    pointerTri(ctx, cx, cy, 0, face - 1, R + 4, 10, C.brassHi);
  }
  // стрелка — курс машины относительно экрана (heading_up: на повороте уходит вбок, потом к метке)
  if (hd.rel != null) blitCentered(ctx, v3Needle(), cx, cy, hd.rel * DEG);

  // табличка с курсом
  const pw = 200;
  const ph = 46;
  const py = cy + R - 6;
  const px0 = Math.round(Math.min(cx - pw / 2, safe.x + safe.w - pw));
  blit(ctx, v3Nameplate(pw, ph), px0, py);
  const fN = fontNum(38);
  const cell = Math.round(textWidth(ctx, fN, "0") + 1);
  const xr = px0 + 22 + cell * 3;
  fixedDigits(ctx, headingStr(hd), xr, py + ph / 2, fN, cell, { fill: hd.known ? C.dark : "#5a4a22", lw: 0 });
  outlinedText(ctx, "°", xr + 7, py + ph / 2 - 10, fontNum(24, 600), { fill: C.dark, lw: 0 });
  outlinedText(ctx, hd.known ? rhumb(hd.heading) : "?", px0 + pw - 22, py + ph / 2, fontLabel(25), {
    fill: C.dark,
    lw: 0,
    align: "right",
  });
  const tagW = textWidth(ctx, fontLabel(25), TAG_TEXT[hd.source] ?? "GPS") + 22;
  // источник — слева от таблички, на уровне её середины
  drawSourceTag(ctx, px0 - 6 - tagW / 2, py + ph / 2, hd.source);
  return { x: cx - R - 2, y: cy - R - 2, w: 2 * R + 4, h: 2 * R + ph };
}

// ── выбор варианта ──────────────────────────────────────────────────────

const DRAW = { v1: drawV1, v2: drawV2, v3: drawV3 };

/** Нарисовать компас выбранного стиля. Возвращает занятый прямоугольник (для раскладки). */
export function drawCompass(ctx, state, safe, styleId) {
  const fn = DRAW[styleId] ?? DRAW.v1;
  return fn(ctx, state, safe);
}
