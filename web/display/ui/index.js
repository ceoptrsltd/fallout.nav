// HUD fallout.nav — единая точка входа для рендера экрана (агент B).
// Контракт: docs/contracts/display.md («API компонентов C»).

import { drawAlarmBanner, updateAlarm } from "./alarm.js";
import { drawClock } from "./clock.js";
import { compassStyles, drawCompass } from "./compass.js";
import { drawLevel, levelVisible } from "./level.js";
import { drawAlarmFlash, drawRadio } from "./radio.js";
import { drawRearSignal, drawRearTag, rearView } from "./rear.js";
import { SPEED_BOX, drawSpeed } from "./speed.js";
import { drawStandby } from "./standby.js";
import { drawPower } from "./power.js";
import { REC_BOX, drawIndicators, drawRecTag, drawWarnings } from "./status.js";
import { clearSprites, safeRect, setting } from "./theme.js";
import { drawTrail } from "./trail.js";
import { drawVehicle } from "./vehicle.js";
import { drawWeather, drawWind } from "./weather.js";

const PANEL_GAP = 8; // между широкой нижней плашкой и уровнем над ней (стоянка)

export { compassStyles, drawRadio, drawStandby, drawTrail, drawVehicle, drawWind };
export { LEVEL_BOX, drawLevelPanel } from "./level.js";

const overlaps = (a, b) => !!b && a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

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
 * Весь HUD поверх карты/видео: часы, запись, компас, скорость, станции, индикаторы, предупреждения, радио.
 * Раскладка (768×576, heading_up — машина в нижней трети по центру):
 *   часы — слева вверху (display.show_clock), справа от них — плашка записи (камера, номера камер 1 и 2);
 *   компас — вверху (v1/v3 справа, v2 по центру), под ним — плашка спутников;
 *   предупреждения — колонкой слева под часами;
 *   скорость — слева внизу (машина стоит — на её месте уровень, ui/level.js); электростанции — справа внизу
 *   (ui/power.js), над ними — строки «нет сети» и охраны; радио (когда играет, иначе погода; переключили охрану —
 *   2.5 с вспышка ALARM ON/OFF) — внизу между ними; ветер (state.weather) — струйки вокруг машины, под плашками.
 *   Центральная полоса над машиной (дорога впереди) свободна; сработала охрана — 10 с крупная плашка
 *   IMPACT / TILTED / MOVING над машиной, поверх всего (машина стоит, дорога не нужна).
 * Задний вид (view.camera = "rear", ui/rear.js): светлая метка REAR вверху между часами и компасом; нет картинки
 * (view.rearSignal) — по центру плашка REAR CAMERA / NO SIGNAL или CONNECTING.
 * opts.wind = false — без ветра: app.js рисует его на своём холсте под HUD (тот же вид, но HUD не перерисовывается
 * ради анимации струек).
 */
export function drawHud(ctx, state, { wind = true } = {}) {
  const safe = safeRect(state);
  ctx.save();
  ctx.setLineDash([]);
  ctx.globalAlpha = 1;
  ctx.globalCompositeOperation = "source-over";

  updateAlarm(ctx, state); // раз за кадр: вспышки и плашка охраны (alarm.js) читают это состояние
  if (wind) drawWind(ctx, state); // под всеми плашками
  const style = setting(state, "display.compass_style", "v1");
  const cr = drawCompass(ctx, state, safe, style);
  const clock = setting(state, "display.show_clock", true) ? drawClock(ctx, state, safe, cr) : null;
  // запись — справа от часов (их нет — на их месте); упёрлась в компас — под часами
  let recAt = clock ? { x: clock.x + clock.w + 8, y: clock.y } : { x: Math.round(safe.x + 3), y: Math.round(safe.y + 3) };
  if (clock && overlaps({ ...recAt, ...REC_BOX }, cr)) recAt = { x: clock.x, y: clock.y + clock.h + 8 };
  const rec = drawRecTag(ctx, state, recAt.x, recAt.y);
  // задний вид (ui/rear.js): метка REAR вверху между часами (с записью) и компасом
  const rear = rearView(state);
  const tag = rear ? drawRearTag(ctx, state, safe, [cr, clock, rec]) : null;

  // предупреждения слева; если часы или компас занимают левый верх — ниже них
  const colRight = safe.x + 230;
  let yTop = safe.y + 3;
  for (const r of [cr, clock, rec, tag]) {
    if (r && r.x < colRight && r.y < safe.y + 200) yTop = Math.max(yTop, r.y + r.h + 10);
  }
  drawWarnings(ctx, state, safe, yTop);

  // стоянка (показан уровень): нижняя плашка (радио / вспышка охраны / погода) — от левого края до плашки станций,
  // уровень — над ней: прогнозу хватает места на значки и больше дней
  const parked = levelVisible(ctx, state);
  const panel = drawRadio(ctx, state, safe, 0, parked) || drawAlarmFlash(ctx, state, safe, 0, parked)
    || drawWeather(ctx, state, safe, 0, parked);
  if (!drawLevel(ctx, state, safe, parked && panel ? panel.h + PANEL_GAP : 0)) {
    drawSpeed(ctx, state, Math.round(safe.x), Math.round(safe.y + safe.h - SPEED_BOX.h));
  }
  drawPower(ctx, state, safe);
  drawIndicators(ctx, state, safe);
  if (rear) drawRearSignal(ctx, state, safe); // нет картинки задней камеры — вместо стоп-кадра
  drawAlarmBanner(ctx, state, safe, [cr, clock, rec, tag]);
  ctx.restore();
}
