// Плашка радио: шкала «тюнера» со стрелкой, станция, трек бегущей строкой. Контракт: docs/contracts/radio.md, §5.
// Данные — state.telemetry.radio, вкл/выкл — display.radio_panel. Место — нижняя «приборная» полоса между
// спидометром (слева) и индикаторами (справа), под машиной: не закрывает дорогу впереди, компас и предупреждения.
//
// По CPU дёшево: плашка, шкала и тексты пререндерены в спрайты (тексты — в маленьком LRU), кадр — несколько
// drawImage. Анимации — только по событию (смена трека/станции, громкость, поиск сигнала) и медленная бегущая
// строка, если трек не помещается.
// radio.shuffle (клавиша 6: следующая станция случайная) — значок справа от станции; после переключения
// режим на 2.5 с показывается в строке трека.

import { SPEED_BOX } from "./speed.js";
import { INDICATOR_ROW, indicatorsWidth } from "./status.js";
import {
  C, SS, blit, capHeight, fontLabel, fontNum, fontStencil, hazardSprite, makeCanvas, plateSprite, setting, sprite,
  spriteGeneration, textWidth,
} from "./theme.js";

/** Высота плашки: ровно две нижние строки индикаторов (GPS, REC) — строка «NO NET» над ними остаётся свободной. */
export const RADIO_H = 2 * INDICATOR_ROW.h + INDICATOR_ROW.gap + 6; // 84
const GAP = 12; // до спидометра и до индикаторов
const PAD = 18; // поля текста (заклёпки по углам)
const VISIBLE = new Set(["playing", "connecting", "waiting_output"]);

const STATION_SIZES = [27, 26, 25];
const TRACK_FONT = fontNum(26, 500);
const STATION_Y = 39; // центр строки станции от верха плашки
const TRACK_Y = 66; // центр строки трека
const DIAL_Y = 15; // линия шкалы

const WIPE_S = 0.6; // смена трека: «развёртка» слева направо
const TUNE_S = 0.9; // смена станции: стрелка переезжает
const VOLUME_S = 2.5; // громкость показывается после изменения
const MODE_S = 2.5; // режим «по порядку / случайно» — после переключения
const SHUFFLE_W = 30; // место под значок shuffle справа от станции
const APPEAR_S = 0.25;

// ── чистые помощники (тестируются в tests/display/radio.test.mjs) ─────────

/** Показывать ли плашку. */
export function radioVisible(state) {
  const r = state?.telemetry?.radio;
  return !!r && VISIBLE.has(r.state) && setting(state, "display.radio_panel", true) !== false;
}

/** Прямоугольник плашки (логические px): между спидометром и индикаторами, низ — по зоне безопасности. */
export function radioRect(safe, indicatorsW, liftPx = 0) {
  const x0 = Math.round(safe.x + SPEED_BOX.w + GAP);
  const x1 = Math.round(safe.x + safe.w - indicatorsW - GAP);
  const y1 = Math.round(safe.y + safe.h - liftPx);
  return { x: x0, y: y1 - RADIO_H, w: Math.max(160, x1 - x0), h: RADIO_H };
}

/** Положение стрелки на шкале 0.08…0.92 — своё у каждой станции (FNV-1a от имени), стабильное. */
export function dialPosition(name) {
  let h = 0x811c9dc5;
  for (const ch of String(name ?? "")) {
    h ^= ch.codePointAt(0);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return 0.08 + ((h % 1000) / 999) * 0.84;
}

/**
 * Сдвиг бегущей строки (px, ≥ 0) через t секунд после появления текста: пауза, затем ровный ход,
 * текст уходит целиком и за ним (через gap) снова приходит начало — цикл без рывка.
 */
export function marqueeOffset(t, textW, boxW, { speed = 34, pause = 1.6, gap = 56 } = {}) {
  if (!(textW > boxW) || !(t > pause)) return 0;
  const run = (textW + gap) / speed;
  const tc = (t - pause) % (pause + run);
  return tc < run ? tc * speed : 0;
}

/** Строка трека: трек, иначе жанр/«в эфире». */
export function trackLine(radio) {
  const t = (radio?.track ?? "").trim();
  if (t) return { text: t, isTrack: true };
  const g = (radio?.genre ?? "").trim();
  return { text: g ? `${g.toUpperCase()} · ON AIR` : "ON AIR", isTrack: false };
}

const ease = (p) => (p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2);
const clamp01 = (x) => Math.max(0, Math.min(1, x));

// ── кэш текстовых спрайтов (LRU; темовый кэш не вытесняет — для меняющихся треков не годится) ──

const textCache = new Map();
const TEXT_CACHE_MAX = 16;

/** Текст с тёмной обводкой в спрайт; y центра прописных — посередине высоты h. */
function textSprite(ctx, text, font, h, { fill = C.ink, lw = 4 } = {}) {
  const key = `${spriteGeneration()}|${font}|${fill}|${lw}|${h}|${text}`;
  let s = textCache.get(key);
  if (s) {
    textCache.delete(key);
    textCache.set(key, s);
    return s;
  }
  const w = Math.ceil(textWidth(ctx, font, text)) + lw + 4;
  const canvas = makeCanvas(Math.ceil(w * SS), Math.ceil(h * SS));
  if (!canvas) return null;
  const c = canvas.getContext("2d");
  c.scale(SS, SS);
  c.font = font;
  c.textAlign = "left";
  c.textBaseline = "alphabetic";
  const yb = h / 2 + capHeight(ctx, font) / 2;
  if (lw > 0) {
    c.lineJoin = "round";
    c.lineWidth = lw;
    c.strokeStyle = C.shadow;
    c.strokeText(text, lw / 2 + 2, yb);
  }
  c.fillStyle = fill;
  c.fillText(text, lw / 2 + 2, yb);
  s = { canvas, w, h };
  textCache.set(key, s);
  if (textCache.size > TEXT_CACHE_MAX) textCache.delete(textCache.keys().next().value);
  return s;
}

/** Станция: самый крупный кегль, при котором помещается; иначе 25 px с многоточием. */
function stationSprite(ctx, name, maxW) {
  const key = `fit|${spriteGeneration()}|${maxW}|${name}`;
  let fit = fitCache.get(key);
  if (!fit) {
    fit = null;
    for (const px of STATION_SIZES) {
      const f = fontLabel(px);
      if (textWidth(ctx, f, name) + 8 <= maxW) {
        fit = { text: name, font: f };
        break;
      }
    }
    if (!fit) {
      const f = fontLabel(STATION_SIZES[STATION_SIZES.length - 1]);
      let lo = 1, hi = name.length;
      while (lo < hi) {
        const mid = (lo + hi + 1) >> 1;
        if (textWidth(ctx, f, cut(name, mid)) + 8 <= maxW) lo = mid;
        else hi = mid - 1;
      }
      fit = { text: cut(name, lo), font: f };
    }
    fitCache.set(key, fit);
    if (fitCache.size > 32) fitCache.delete(fitCache.keys().next().value);
  }
  return textSprite(ctx, fit.text, fit.font, 34);
}
const fitCache = new Map();
// обрезать до n символов с многоточием, без «висящих» разделителей: «Radio Paradise -…» → «Radio Paradise…»
const cut = (name, n) => name.slice(0, n).replace(/[\s\-–—:|·,/(]+$/u, "") + "…";

// ширина самого текста в спрайте (без полей под обводку) — для бесшовного цикла бегущей строки
const textW = (s) => s.w - 6;

/**
 * Спрайт текста со сдвигом off от x0, видимый только в полосе clipX0..clipX1. loopGap — бегущая строка:
 * следом (через loopGap) идёт вторая копия, чтобы начало приходило без пустоты.
 */
function drawClipped(ctx, s, x0, y, off, boxW, clipX0, clipX1, loopGap) {
  if (!s || clipX1 <= clipX0) return;
  ctx.save();
  ctx.beginPath();
  ctx.rect(clipX0, y - 2, clipX1 - clipX0, s.h + 4);
  ctx.clip();
  ctx.drawImage(s.canvas, x0 - off, y, s.w, s.h);
  if (loopGap != null && textW(s) > boxW && off > 0) {
    ctx.drawImage(s.canvas, x0 - off + textW(s) + loopGap, y, s.w, s.h);
  }
  ctx.restore();
}

// ── статичные части ───────────────────────────────────────────────────────

/** Шкала тюнера: штрихи (мелкие и крупные) и линия; стрелка рисуется поверх каждый кадр. */
function dialSprite(w) {
  return sprite("radio-dial", w, 20, (c) => {
    c.strokeStyle = C.inkDim;
    c.lineWidth = 2;
    c.beginPath();
    c.moveTo(0, DIAL_Y - 4.5);
    c.lineTo(w, DIAL_Y - 4.5);
    c.stroke();
    const n = Math.max(8, Math.round(w / 9));
    for (let i = 0; i <= n; i++) {
      const x = 1 + (i / n) * (w - 2);
      const major = i % 5 === 0;
      c.fillStyle = major ? C.ink : C.inkDim;
      c.fillRect(Math.round(x - 1), major ? 1 : 5, 2, major ? DIAL_Y - 5.5 : DIAL_Y - 9.5);
    }
  });
}

/** Значок shuffle: две скрещённые стрелки; straight — две параллельные («по порядку»). 26×20. */
function modeSprite(straight) {
  return sprite(straight ? "radio-inorder" : "radio-shuffle", 26, 20, (c) => {
    const paths = straight
      ? [[[2, 6], [20, 6]], [[2, 14], [20, 14]]]
      : [[[2, 15], [8, 15], [14, 5], [20, 5]], [[2, 5], [8, 5], [14, 15], [20, 15]]];
    const heads = (stroke) => {
      for (const pts of paths) {
        const [x, y] = pts[pts.length - 1];
        c.beginPath();
        c.moveTo(x - 5, y - 4.5);
        c.lineTo(x + 1, y);
        c.lineTo(x - 5, y + 4.5);
        stroke();
      }
    };
    c.lineCap = "round";
    c.lineJoin = "round";
    for (const [lw, col] of [[6, C.shadow], [2.6, C.ink]]) {
      c.lineWidth = lw;
      c.strokeStyle = col;
      for (const pts of paths) {
        c.beginPath();
        pts.forEach(([x, y], i) => (i ? c.lineTo(x, y) : c.moveTo(x, y)));
        c.stroke();
      }
      heads(() => c.stroke());
    }
  });
}

/** Ноты перед названием трека (две восьмые с ребром), с тёмной обводкой — читаются и на пёстрой карте. */
function noteSprite() {
  return sprite("radio-note", 18, 24, (c) => {
    c.beginPath();
    c.rect(6.2, 4, 2.4, 14.5); // штили
    c.rect(14.2, 2, 2.4, 14.5);
    c.moveTo(6.2, 4); // ребро
    c.lineTo(16.6, 1.5);
    c.lineTo(16.6, 5.8);
    c.lineTo(6.2, 8.3);
    c.closePath();
    for (const [hx, hy] of [[4.6, 18.6], [12.6, 16.6]]) { // головки
      c.moveTo(hx + 4.2, hy);
      c.ellipse(hx, hy, 4.2, 3.2, -0.45, 0, 2 * Math.PI);
    }
    c.lineJoin = "round";
    c.lineWidth = 3;
    c.strokeStyle = C.shadow;
    c.stroke();
    c.fillStyle = C.ink;
    c.fill("nonzero");
  });
}

// ── состояние анимаций — своё для каждого холста (в галерее их несколько) ──

const panels = new WeakMap();

function panelFor(ctx) {
  const key = ctx.canvas ?? ctx;
  let p = panels.get(key);
  if (!p) {
    p = { station: null, track: null, trackT0: 0, prevTrack: null, wipeT0: -1, needleFrom: null, needle: 0.5,
          tuneT0: -1, prevStation: null, volume: null, volumeT0: -1, shuffle: null, modeT0: -1, appearT0: -1,
          visible: false };
    panels.set(key, p);
  }
  return p;
}

/** Учесть новые данные: смена станции/трека/громкости запускает анимации. */
function update(p, radio, now) {
  const station = radio.station ?? "";
  const tl = trackLine(radio);
  if (!p.visible) {
    // появилась (или снова после «stopped») — без анимаций смены, только плавное появление
    p.visible = true;
    p.appearT0 = now;
    p.station = station;
    p.track = tl;
    p.trackT0 = now;
    p.needle = dialPosition(station);
    p.needleFrom = null;
    p.prevTrack = null;
    p.prevStation = null;
    p.volume = radio.volume ?? null;
    p.shuffle = !!radio.shuffle;
    return;
  }
  if (station !== p.station) {
    p.needleFrom = currentNeedle(p, now);
    p.needle = dialPosition(station);
    p.tuneT0 = now;
    p.prevStation = p.station;
    p.prevTrack = p.track;
    p.prevTrackOff = p.trackOffNow ?? 0;
    p.wipeT0 = now;
    p.station = station;
    p.track = tl;
    p.trackT0 = now;
  } else if (tl.text !== p.track?.text) {
    p.prevTrack = p.track;
    p.prevTrackOff = p.trackOffNow ?? 0;
    p.wipeT0 = now;
    p.track = tl;
    p.trackT0 = now;
  }
  const v = radio.volume;
  if (typeof v === "number" && p.volume !== null && v !== p.volume) p.volumeT0 = now;
  if (typeof v === "number") p.volume = v;
  const sh = !!radio.shuffle;
  if (p.shuffle !== null && sh !== p.shuffle) p.modeT0 = now;
  p.shuffle = sh;
}

function currentNeedle(p, now) {
  if (p.needleFrom == null || p.tuneT0 < 0) return p.needle;
  const k = ease(clamp01((now - p.tuneT0) / TUNE_S));
  return p.needleFrom + (p.needle - p.needleFrom) * k;
}

// ── рисование ─────────────────────────────────────────────────────────────

function drawNeedle(ctx, x, y, pos, dim) {
  ctx.save();
  ctx.globalAlpha *= dim ? 0.5 : 1;
  ctx.fillStyle = C.shadow;
  ctx.fillRect(Math.round(x + pos) - 3.5, y + 1, 7, 21);
  ctx.fillStyle = C.accent;
  ctx.fillRect(Math.round(x + pos) - 2, y + 2, 4, 19);
  ctx.beginPath(); // «флажок» сверху
  ctx.moveTo(Math.round(x + pos) - 5, y + 1);
  ctx.lineTo(Math.round(x + pos) + 5, y + 1);
  ctx.lineTo(Math.round(x + pos), y + 7);
  ctx.closePath();
  ctx.fill();
  ctx.restore();
}

/** Затемнение края окна бегущей строки (градиент в цвет плашки). */
function fadeEdge(ctx, x, y, w, h, left) {
  const g = ctx.createLinearGradient(x, 0, x + w, 0);
  g.addColorStop(left ? 1 : 0, "rgba(10,12,8,0)");
  g.addColorStop(left ? 0 : 1, "rgba(10,12,8,0.92)");
  ctx.fillStyle = g;
  ctx.fillRect(x, y, w, h);
}

/** Яркая полоса развёртки (смена трека/станции) — со «свечением». */
function drawScanBar(ctx, x, y, h) {
  ctx.fillStyle = "rgba(236,247,216,0.16)";
  ctx.fillRect(x - 12, y, 24, h);
  ctx.fillStyle = "rgba(236,247,216,0.34)";
  ctx.fillRect(x - 6, y, 12, h);
  ctx.fillStyle = C.ink;
  ctx.fillRect(x - 2, y - 3, 4, h + 6);
}

function drawVolume(ctx, x, yc, w, vol) {
  const font = fontLabel(25);
  const lw = textWidth(ctx, font, "VOL");
  ctx.save();
  ctx.font = font;
  ctx.textAlign = "left";
  ctx.textBaseline = "alphabetic";
  ctx.lineWidth = 4;
  ctx.lineJoin = "round";
  ctx.strokeStyle = C.shadow;
  const yb = yc + capHeight(ctx, font) / 2;
  ctx.strokeText("VOL", x, yb);
  ctx.fillStyle = C.ink;
  ctx.fillText("VOL", x, yb);
  const num = String(Math.round(vol));
  const fN = fontNum(26, 600);
  const nw = textWidth(ctx, fN, "100");
  ctx.font = fN;
  ctx.textAlign = "right";
  const ybN = yc + capHeight(ctx, fN) / 2;
  ctx.strokeText(num, x + w, ybN);
  ctx.fillText(num, x + w, ybN);
  // сегменты как у спидометра: светлые — зажжены, тёмные с рамкой — нет (различимо и в ч/б)
  const sx = x + lw + 12;
  const sw = w - lw - 12 - nw - 12;
  const n = 10;
  const gap = 3;
  const segW = (sw - gap * (n - 1)) / n;
  const lit = Math.round((Math.max(0, Math.min(100, vol)) / 100) * n);
  for (let i = 0; i < n; i++) {
    const px = sx + i * (segW + gap);
    const hh = 8 + Math.round((i / (n - 1)) * 10);
    const py = yc + 9 - hh;
    if (i < lit) {
      ctx.fillStyle = C.ink;
      ctx.fillRect(px, py, segW, hh);
    } else {
      ctx.fillStyle = "rgba(236,247,216,0.12)";
      ctx.fillRect(px, py, segW, hh);
      ctx.strokeStyle = "rgba(236,247,216,0.35)";
      ctx.lineWidth = 1;
      ctx.strokeRect(px + 0.5, py + 0.5, segW - 1, hh - 1);
    }
  }
  ctx.restore();
}

/** «NEXT: RANDOM» / «NEXT: IN ORDER» — после переключения клавишей 6. */
function drawMode(ctx, x, yc, shuffle) {
  blit(ctx, modeSprite(!shuffle), x - 1, Math.round(yc - 10));
  const s = textSprite(ctx, shuffle ? "NEXT: RANDOM" : "NEXT: IN ORDER", fontLabel(25), 30);
  blit(ctx, s, x + 30, Math.round(yc - 15));
}

/** «NO SPEAKER» — светлая аварийная плашка (как NO GPS), не мигает: это не авария, а подсказка. */
function drawNoSpeaker(ctx, x, yc) {
  const text = "NO SPEAKER";
  const font = fontStencil(25);
  const w = Math.ceil(textWidth(ctx, font, text)) + 2 * 22 + 16;
  const h = 30;
  blit(ctx, hazardSprite(w, h), x, Math.round(yc - h / 2));
  ctx.save();
  ctx.font = font;
  ctx.textAlign = "center";
  ctx.textBaseline = "alphabetic";
  ctx.fillStyle = C.dark;
  ctx.fillText(text, x + w / 2, yc + capHeight(ctx, font) / 2);
  ctx.restore();
}

/** «TUNING» с бегущими точками (только пока ищем сигнал). */
function drawTuning(ctx, x, yc, now) {
  const dots = Math.floor(now * 2.5) % 4;
  const s = textSprite(ctx, "TUNING" + ".".repeat(dots), fontLabel(25), 30);
  blit(ctx, s, x - 2, Math.round(yc - 15));
}

/**
 * Плашка радио. safe — зона безопасности, liftPx — поднять над нижним краем (подпись Esri в демо/удалённо).
 * Возвращает занятый прямоугольник или null, если плашка не показана.
 */
export function drawRadio(ctx, state, safe, liftPx = 0) {
  const p = panelFor(ctx);
  if (!radioVisible(state)) {
    p.visible = false;
    return null;
  }
  const radio = state.telemetry.radio;
  const now = state?.now ?? 0;
  update(p, radio, now);

  const r = radioRect(safe, indicatorsWidth(ctx), liftPx);
  const { x, y, w, h } = r;
  const appear = clamp01((now - p.appearT0) / APPEAR_S);
  ctx.save();
  if (appear < 1) {
    ctx.globalAlpha *= appear;
    ctx.translate(0, Math.round((1 - appear) * 10));
  }

  blit(ctx, plateSprite(w, h, { chamfer: 9 }), x, y);

  // шкала и стрелка
  const dx = x + PAD + 2;
  const dw = w - 2 * (PAD + 2);
  blit(ctx, dialSprite(dw), dx, y + 4);
  let needle = currentNeedle(p, now);
  if (radio.state === "connecting") {
    // поиск сигнала: стрелка «гуляет» около станции
    needle += Math.sin(now * 2.3) * 0.06 + Math.sin(now * 5.7) * 0.015;
  } else if (p.wipeT0 >= 0 && now - p.wipeT0 < 0.4 && p.tuneT0 !== p.wipeT0) {
    needle += Math.sin((now - p.wipeT0) * 40) * 0.006 * (1 - (now - p.wipeT0) / 0.4); // «толчок» на новом треке
  }
  drawNeedle(ctx, dx, y + 2, clamp01(needle) * dw, radio.state === "waiting_output");

  // станция (при смене станции — та же развёртка, что у трека)
  const tx0 = x + PAD;
  const tx1 = x + w - PAD;
  const wipe = p.wipeT0 >= 0 ? clamp01((now - p.wipeT0) / WIPE_S) : 1;
  if (wipe >= 1) {
    p.prevStation = null;
    p.prevTrack = null;
  }
  const barX = tx0 - 10 + wipe * (tx1 - tx0 + 20);
  const sx1 = p.shuffle ? tx1 - SHUFFLE_W : tx1; // справа от станции — значок shuffle
  const st = stationSprite(ctx, p.station || "RADIO", sx1 - tx0);
  const sy = Math.round(y + STATION_Y - 17);
  if (wipe < 1 && p.prevStation != null) {
    drawClipped(ctx, st, tx0, sy, 0, sx1 - tx0, tx0, Math.min(barX, sx1));
    drawClipped(ctx, stationSprite(ctx, p.prevStation || "RADIO", sx1 - tx0), tx0, sy, 0, sx1 - tx0, barX, sx1);
  } else {
    drawClipped(ctx, st, tx0, sy, 0, sx1 - tx0, tx0, sx1);
  }
  if (p.shuffle) blit(ctx, modeSprite(false), tx1 - 24, Math.round(y + STATION_Y - 10));

  // строка трека: громкость или режим (после изменения, последнее из них) / NO SPEAKER / TUNING / трек
  const yc = y + TRACK_Y;
  const showVol = p.volumeT0 >= 0 && now - p.volumeT0 < VOLUME_S && typeof p.volume === "number";
  const showMode = p.modeT0 >= 0 && now - p.modeT0 < MODE_S;
  if (showMode && !(showVol && p.volumeT0 > p.modeT0)) {
    drawMode(ctx, tx0, yc, p.shuffle);
  } else if (showVol) {
    drawVolume(ctx, tx0, yc, tx1 - tx0, p.volume);
  } else if (radio.state === "waiting_output") {
    drawNoSpeaker(ctx, tx0 - 2, yc);
  } else if (radio.state === "connecting") {
    drawTuning(ctx, tx0, yc, now);
  } else {
    drawTrackLine(ctx, p, now, tx0, tx1, yc, wipe, barX);
  }
  if (wipe < 1) drawScanBar(ctx, barX, y + 24, h - 30);
  ctx.restore();
  return r;
}

function drawTrackLine(ctx, p, now, tx0, tx1, yc, wipe, barX) {
  const lineH = 32;
  const ly = Math.round(yc - lineH / 2);
  const draw = (tl, t0, clipA, clipB, frozenOff) => {
    if (!tl) return;
    let x0 = tx0;
    if (tl.isTrack) {
      blit(ctx, noteSprite(), tx0 - 1, Math.round(yc - 13));
      x0 = tx0 + 22;
    }
    const font = tl.isTrack ? TRACK_FONT : fontLabel(25);
    const s = textSprite(ctx, tl.text, font, lineH, { fill: C.ink, lw: 4 });
    if (!s) return;
    const boxW = tx1 - x0;
    const off = frozenOff ?? marqueeOffset(now - t0 - WIPE_S, textW(s), boxW);
    if (frozenOff == null) p.trackOffNow = off;
    drawClipped(ctx, s, x0, ly, off, boxW, Math.max(x0, clipA), clipB, 56);
    if (textW(s) > boxW) {
      // текст длиннее окна — края «окна шкалы» затемнены: строка уходит в щель, а не обрывается
      fadeEdge(ctx, tx1 - 18, ly, 18, lineH, false);
      if (off > 0) fadeEdge(ctx, x0, ly, 14, lineH, true);
    }
  };
  if (wipe < 1 && p.prevTrack) {
    // развёртка: слева от полосы — новый трек, справа — старый (замер на своём месте)
    ctx.save();
    ctx.beginPath();
    ctx.rect(tx0 - 4, ly - 4, barX - tx0 + 4, lineH + 8);
    ctx.clip();
    draw(p.track, p.trackT0, tx0, Math.min(barX, tx1), 0);
    ctx.restore();
    ctx.save();
    ctx.beginPath();
    ctx.rect(barX, ly - 4, tx1 - barX + 4, lineH + 8);
    ctx.clip();
    draw(p.prevTrack, 0, barX, tx1, p.prevTrackOff ?? 0);
    ctx.restore();
  } else {
    draw(p.track, p.trackT0, tx0, tx1, null);
  }
}

/** Для отладки/тестов: сбросить анимационное состояние холста. */
export function resetRadio(ctx) {
  panels.delete(ctx.canvas ?? ctx);
}
