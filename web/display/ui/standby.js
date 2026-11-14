// Экран «на охране» (settings.display.armed_standby): пока машина на охране (когда — решает map/standby.js),
// вместо карты и HUD — «визитка» для прохожих у фургона: чёрный фон, крупно логотип хозяина и его телефон
// (сайт ptrs.ltd), внизу бегущая строка «по всем вопросам — к хозяину», сбоку от логотипа ALARM ON с замком и часы.
// Без карты, тайлов, камеры, ветра и опроса геймпада. Бегущая строка движется — drawStandby возвращает
// animated: true, и app.js рисует его по requestAnimationFrame (с ?fps=), а не раз в секунду.
// «Ставят на охрану» (arming) и «сработала» (triggered) — обычный экран (сработала — с крупной плашкой).
//
// Вся картинка раз в минуту сдвигается на несколько px: статичный логотип часами не выжигает люминофор кинескопа.
//
// Логотип — web/display/ui/img/ptrs-logo.png: ptrs.ltd/ptrslogofull.png (чёрный рисунок на прозрачном), для кинескопа
// переведён в «светлое лицо на чёрном»: внутри силуэта головы — светлая заливка и тёмные штрихи, штрихи вне головы
// (плечи) — светлые. Грузится асинхронно: первые кадры — без него (место под ним пустое).

import { drawLock } from "./alarm.js";
import { CLOCK_H, clockWidth, drawClock } from "./clock.js";
import { C, blit, fontLabel, fontNum, fontStencil, outlinedText, plateSprite, sprite, textWidth } from "./theme.js";

/** Хозяин: телефон — как на ptrs.ltd (раздел Contact), строка — латиница (шрифты экрана без кириллицы). */
export const OWNER = {
  phone: "+48 792 556 939",
  ticker: "FOR ANY QUESTIONS PLEASE CONTACT THE OWNER",
  logoUrl: new URL("./img/ptrs-logo.png", import.meta.url).href,
  logoAspect: 547 / 600, // ширина / высота файла
};

const TEXT = "ALARM ON";
const LOCK = 40; // высота замка, px
const GAP = 14; // замок — надпись
const LOGO_H = 290; // высота логотипа, px
const COL_GAP = 40; // логотип — столбик ALARM ON / часы
const TOP = 52; // верх логотипа (без сдвига)
const PHONE_PX = 80;
const PHONE_H = 96; // высота плашки телефона
const PHONE_Y = TOP + LOGO_H + 10; // верх плашки
const TICK_PX = 40;
const TICK_Y = PHONE_Y + PHONE_H + 46; // центр бегущей строки
const TICK_GAP = 96; // промежуток между повторами (в его середине — ромб)
const TICK_SPEED = 60; // px/с: 2 px за кадр при 30 к/с — ровный шаг на кинескопе
const DRIFT_X = [0, 24, -18, 10, -26, 16, -8, 22, -14, 4]; // сдвиги по минутам, px
const DRIFT_Y = [0, -14, 10, -6, 16, -18, 6, 12, -10, -2];
const MAX_DX = Math.max(...DRIFT_X.map(Math.abs));
const PLATE_PAD = 48; // поля плашки телефона (обе стороны вместе)
const TICK_H = TICK_PX + 16; // высота спрайта бегущей строки

/** Сдвиг картинки в минуту ms (время экрана): шаг раз в минуту, повтор через 10 минут. */
export function standbyDrift(ms) {
  const m = Math.floor((Number.isFinite(ms) ? ms : 0) / 60000);
  const i = ((m % 10) + 10) % 10;
  return { dx: DRIFT_X[i], dy: DRIFT_Y[i] };
}

/** Сдвиг бегущей строки (целые px, 0 … period−1) в момент t с: едет влево без пауз, повтор каждые period px. */
export function tickerOffset(t, period, speed = TICK_SPEED) {
  if (!(period > 0) || !Number.isFinite(t)) return 0;
  return Math.floor((((t * speed) % period) + period) % period);
}

// ── логотип: грузится один раз, при ошибке — повтор через 30 с ──
const logo = { img: null, loading: false, retryAt: 0 };

function ownerLogo() {
  if (logo.img || logo.loading || typeof Image === "undefined" || Date.now() < logo.retryAt) return logo.img;
  logo.loading = true;
  const img = new Image();
  img.decoding = "async";
  img.onload = () => {
    logo.loading = false;
    logo.img = img;
  };
  img.onerror = () => {
    logo.loading = false;
    logo.retryAt = Date.now() + 30000;
  };
  img.src = OWNER.logoUrl;
  return null;
}

/** Логотип спрайтом (картинка `<img>` напрямую сделала бы каждый кадр «всегда рисовать», map/retained.js). */
function logoSprite(w, h) {
  const img = ownerLogo();
  if (!img) return null;
  return sprite(`standby-logo|${OWNER.logoUrl}`, w, h, (c) => {
    c.imageSmoothingEnabled = true;
    c.imageSmoothingQuality = "high";
    c.drawImage(img, 0, 0, w, h);
  });
}

/** Повтор бегущей строки: текст + ромб посередине промежутка. */
function tickerSprite(ctx) {
  const font = fontLabel(TICK_PX);
  const tw = Math.ceil(textWidth(ctx, font, OWNER.ticker));
  const unit = tw + TICK_GAP;
  const h = TICK_H;
  const s = sprite(`standby-ticker|${font}|${OWNER.ticker}`, unit, h, (c) => {
    outlinedText(c, OWNER.ticker, 0, h / 2, font, { align: "left", lw: 0 });
    const x = tw + TICK_GAP / 2;
    c.fillStyle = C.inkDim;
    c.beginPath();
    c.moveTo(x, h / 2 - 8);
    c.lineTo(x + 8, h / 2);
    c.lineTo(x, h / 2 + 8);
    c.lineTo(x - 8, h / 2);
    c.closePath();
    c.fill();
  });
  return { s, unit, h };
}

/**
 * Раскладка экрана «на охране» со сдвигом { dx, dy } (логические px холста cw×ch).
 * Всё, кроме бегущей строки (она во всю ширину и уходит за края), при любом сдвиге — в зоне безопасности 5 %.
 */
export function standbyLayout(ctx, cw, ch, { dx = 0, dy = 0 } = {}) {
  // верх: логотип слева, справа от него столбик «замок + ALARM ON» над часами; вся группа — по центру
  const logoW = Math.round(LOGO_H * OWNER.logoAspect);
  const font = fontStencil(44);
  const tw = textWidth(ctx, font, TEXT);
  const lockW = LOCK * 0.8;
  const rowW = lockW + GAP + tw;
  const cWidth = clockWidth(ctx);
  const colW = Math.max(rowW, cWidth);
  const groupW = logoW + COL_GAP + colW;
  const gx = Math.round((cw - groupW) / 2 + dx);
  const top = TOP + dy;
  // столбик — по середине высоты логотипа
  const colCx = gx + logoW + COL_GAP + colW / 2;
  const colH = 44 + 26 + CLOCK_H;
  const colTop = Math.round(top + (LOGO_H - colH) / 2);
  const rx = colCx - rowW / 2;

  // телефон: шрифт мельче, если плашка с полями при самом большом сдвиге не влезает в зону безопасности
  const cx = Math.round(cw / 2 + dx);
  const maxW = cw * 0.9 - 2 * MAX_DX - PLATE_PAD - 4;
  let px = PHONE_PX;
  let pw = textWidth(ctx, fontNum(px, 700), OWNER.phone);
  if (pw > maxW) {
    px = Math.max(40, Math.floor((px * maxW) / pw));
    pw = textWidth(ctx, fontNum(px, 700), OWNER.phone);
  }
  const plateW = Math.ceil(pw) + PLATE_PAD;
  const tickY = TICK_Y + dy;
  return {
    logo: { x: gx, y: top, w: logoW, h: LOGO_H },
    alarm: { lockX: rx + lockW / 2, textX: rx + lockW + GAP, y: colTop + 22, w: rowW, font },
    clock: { x: Math.round(colCx - cWidth / 2), y: colTop + 44 + 26, w: cWidth, h: CLOCK_H },
    phone: { x: Math.round(cx - plateW / 2), y: PHONE_Y + dy, w: plateW, h: PHONE_H, cx, font: fontNum(px, 700) },
    // черты бегущей строки — сверху и снизу, по 2 px
    ticker: { y: Math.round(tickY - TICK_H / 2), h: TICK_H, rule1: Math.round(tickY - TICK_H / 2 - 4), rule2: Math.round(tickY + TICK_H / 2 + 2) },
  };
}

/** Весь экран «на охране» (чёрный фон на весь холст). Возвращает { x, y, w, h, animated: true }. */
export function drawStandby(ctx, state) {
  const cw = state?.canvas?.w ?? 768;
  const ch = state?.canvas?.h ?? 576;
  ctx.save();
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, cw, ch);
  const L = standbyLayout(ctx, cw, ch, standbyDrift(state?.view?.wallMs ?? Date.now()));

  const ls = logoSprite(L.logo.w, L.logo.h);
  if (ls) {
    ctx.globalAlpha = 0.92;
    blit(ctx, ls, L.logo.x, L.logo.y);
    ctx.globalAlpha = 1;
  }

  // замок и ALARM ON тусклые, часы — как в HUD (drawClock ставит плашку в safe.x + 3, safe.y + 3)
  const a = L.alarm;
  drawLock(ctx, a.lockX, a.y - 2, LOCK, false, C.inkDim, C.dark);
  outlinedText(ctx, TEXT, a.textX, a.y, a.font, { align: "left", lw: 0, fill: C.inkDim });
  ctx.globalAlpha = 0.8;
  drawClock(ctx, state, { x: L.clock.x - 3, y: L.clock.y - 3, w: L.clock.w, h: L.clock.h });
  ctx.globalAlpha = 1;

  // телефон — крупно, на плашке
  const p = L.phone;
  blit(ctx, plateSprite(p.w, p.h, { chamfer: 12 }), p.x, p.y);
  outlinedText(ctx, OWNER.phone, p.cx, p.y + p.h / 2, p.font, { lw: 6 });

  // бегущая строка во всю ширину холста: за края кинескопа она уходит сама
  const t = L.ticker;
  ctx.fillStyle = C.inkFaint;
  ctx.fillRect(0, t.rule1, cw, 2);
  ctx.fillRect(0, t.rule2, cw, 2);
  const ts = tickerSprite(ctx);
  if (ts.s) {
    const off = tickerOffset(state?.now ?? 0, ts.unit);
    for (let x = -off; x < cw; x += ts.unit) blit(ctx, ts.s, x, t.y);
  }
  ctx.restore();
  return { x: 0, y: L.logo.y, w: cw, h: t.rule2 + 2 - L.logo.y, animated: true };
}
