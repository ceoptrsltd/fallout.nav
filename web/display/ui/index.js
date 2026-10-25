// HUD fallout.nav — единая точка входа для рендера экрана (агент B).
// Контракт: docs/contracts/display.md («API компонентов C»).

import { drawClock } from "./clock.js";
import { compassStyles, drawCompass } from "./compass.js";
import { drawRadio } from "./radio.js";
import { SPEED_BOX, drawSpeed } from "./speed.js";
import { drawIndicators, drawWarnings } from "./status.js";
import { clearSprites, safeRect, setting } from "./theme.js";
import { drawTrail } from "./trail.js";
import { drawVehicle } from "./vehicle.js";
import { drawWeather, drawWind } from "./weather.js";

export { compassStyles, drawRadio, drawTrail, drawVehicle };

const FONTS = [
  { family: "FnavRusso", file: "RussoOne-Regular.ttf", descriptors: { weight: "400" } },
  { family: "FnavOswald", file: "Oswald-wght.ttf", descriptors: { weight: "200 700" } },
  { family: "FnavStencil", file: "BlackOpsOne-Regular.ttf", descriptors: { weight: "400" } },
];

let loadPromise = null;

/**
 * Загрузить шрифты (локально, из ./fonts/). Ошибки не пробрасываются: при сбое HUD рисуется
 * запасными системными шрифтами. Возвращает { fonts: { FnavRusso: "ok"|"error"|"unsupported", … } }.
 */
export function loadUi() {
  if (loadPromise) return loadPromise;
  loadPromise = (async () => {
    const result = { fonts: {} };
    const fontSet = typeof document !== "undefined" ? document.fonts : globalThis.self?.fonts;
    if (typeof FontFace === "undefined" || !fontSet) {
      for (const f of FONTS) result.fonts[f.family] = "unsupported";
      return result;
    }
    await Promise.all(
      FONTS.map(async (f) => {
        try {
          const url = new URL(`./fonts/${f.file}`, import.meta.url).href;
          const face = new FontFace(f.family, `url("${url}")`, f.descriptors);
          const timeout = new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), 5000));
          await Promise.race([face.load(), timeout]);
          fontSet.add(face);
          result.fonts[f.family] = "ok";
        } catch (e) {
          console.warn(`[fnav/ui] font ${f.file} failed to load, using fallback`, e);
          result.fonts[f.family] = "error";
        }
      }),
    );
    clearSprites(); // спрайты с текстом перерисуются уже новыми шрифтами
    return result;
  })();
  return loadPromise;
}

/**
 * Весь HUD поверх карты/видео: часы, компас, скорость, индикаторы, предупреждения, радио.
 * Раскладка (768×576, heading_up — машина в нижней трети по центру):
 *   часы — слева вверху (display.show_clock); компас — вверху (v1/v3 справа, v2 по центру);
 *   предупреждения — колонкой слева под часами;
 *   скорость — слева внизу; индикаторы — справа внизу; радио (когда играет, иначе погода) — внизу между ними;
 *   ветер (state.weather) — струйки вокруг машины, под всеми плашками.
 *   Центральная полоса над машиной (дорога впереди) свободна.
 * view.attribution — внизу по центру подпись источников карты (демо/удалённо): радио поднимается над ней.
 */
export function drawHud(ctx, state) {
  const safe = safeRect(state);
  ctx.save();
  ctx.setLineDash([]);
  ctx.globalAlpha = 1;
  ctx.globalCompositeOperation = "source-over";

  drawWind(ctx, state); // под всеми плашками
  const style = setting(state, "display.compass_style", "v1");
  const cr = drawCompass(ctx, state, safe, style);
  const clock = setting(state, "display.show_clock", true) ? drawClock(ctx, state, safe, cr) : null;

  // предупреждения слева; если часы или компас занимают левый верх — ниже них
  const colRight = safe.x + 230;
  let yTop = safe.y + 3;
  for (const r of [cr, clock]) {
    if (r && r.x < colRight && r.y < safe.y + 200) yTop = Math.max(yTop, r.y + r.h + 10);
  }
  drawWarnings(ctx, state, safe, yTop);

  drawSpeed(ctx, state, Math.round(safe.x), Math.round(safe.y + safe.h - SPEED_BOX.h));
  drawIndicators(ctx, state, safe);
  const lift = state?.view?.attribution ? 17 : 0;
  if (!drawRadio(ctx, state, safe, lift)) drawWeather(ctx, state, safe, lift); // радио выключено — погода
  ctx.restore();
}
