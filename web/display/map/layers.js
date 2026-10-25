// Какие слои рисовать и какие тайлы запрашивать — чистые функции.

import { latOfMercY, makeView, metersPerPixel, moveMerc, parentTile, tileKey, tileLevelFor, visibleTiles } from "./mercator.js";
import { ROOT } from "./root.js";

/** Дороги, подписи и карта улиц Esri нарисованы только до z19 (дальше — пустые тайлы / заглушка): глубже — растягиваем. */
export const VECTOR_MAX_Z = 19;

/**
 * Слои снизу вверх по settings.display.layers: imagery|street → transport → places.
 * base = непрозрачная подложка (для неё — заглушки и перекрытие швов); maxZ — самый подробный уровень тайлов слоя
 * (снимки — settings.tiles.max_zoom, у Esri в городах есть z20–z21; остальное — VECTOR_MAX_Z).
 */
export function activeLayers(settings) {
  const l = (settings && settings.display && settings.display.layers) || {};
  const maxZ = settings?.tiles?.max_zoom ?? 21;
  const vec = Math.min(maxZ, VECTOR_MAX_Z);
  const out = [];
  if (l.street_map_instead) out.push({ id: "street", base: true, maxZ: vec });
  else if (l.imagery !== false) out.push({ id: "imagery", base: true, maxZ });
  if (l.transport !== false) out.push({ id: "transport", base: false, maxZ: vec });
  if (l.places !== false) out.push({ id: "places", base: false, maxZ: vec });
  return out;
}

/**
 * Уровень тайлов слоя: такой, чтобы тайл ложился на экран примерно 1:1 в настоящих пикселях.
 * detail = log2(пикселей экрана на логический px холста): ноутбук с чётким экраном берёт уровень подробнее.
 */
export function layerLevel(layer, zoom, maxZoom, detail = 0) {
  return tileLevelFor(zoom + detail, Math.min(maxZoom, layer.maxZ ?? maxZoom));
}

/** Тайл через кэш fnav-core: <root>tiles/… (docs/contracts/remote.md). */
export function tileUrl(layer, z, x, y, root = ROOT) {
  return `${root}tiles/${layer}/${z}/${x}/${y}`;
}

/**
 * Список запросов тайлов с приоритетами (меньше = раньше):
 *   1) видимые тайлы текущего уровня (подложка раньше надписей, ближние к машине раньше); тайла нет (failed —
 *      Esri «нет снимка», нет сети и нет в кэше) — его родитель, до 3 уровней вверх: рисуется растянутым;
 *   2) уровень целевого зума (если отличается) — заранее, к плавной смене масштаба;
 *   3) область впереди по курсу (lookahead), чтобы тайлы были готовы до въезда в кадр.
 * detail — см. layerLevel().
 */
export function planRequests({ view, layers, maxZoom, targetZoom = null, headingDeg = null, speedMps = 0, margin = 48,
                               maxRequests = 160, urlFor = tileUrl, detail = 0, failed = null }) {
  const reqs = [];
  const tilesAt = new Map(); // вид → уровень → видимые тайлы (у слоёв может быть разный уровень)
  const visible = (v, z, m) => {
    if (!tilesAt.has(v)) tilesAt.set(v, new Map());
    const byZ = tilesAt.get(v);
    if (!byZ.has(z)) byZ.set(z, visibleTiles(v, z, m));
    return byZ.get(z);
  };
  const push = (tiles, layerIdx, layer, basePri, parents = false) => {
    for (const t of tiles) {
      const key = tileKey(layer.id, t.z, t.wx, t.y);
      const priority = basePri + layerIdx * 0.75 + t.d * 0.25;
      reqs.push({ key, url: urlFor(layer.id, t.z, t.wx, t.y), priority });
      if (!parents || !failed || !failed(key)) continue;
      for (let up = 1; up <= 3; up++) {
        const p = parentTile(t.z, t.wx, t.y, up);
        if (!p) break;
        const pk = tileKey(layer.id, p.z, p.x, p.y);
        reqs.push({ key: pk, url: urlFor(layer.id, p.z, p.x, p.y), priority: priority + 0.1 * up });
        if (!failed(pk)) break;
      }
    }
  };
  layers.forEach((layer, i) => push(visible(view, layerLevel(layer, view.zoom, maxZoom, detail), margin), i, layer, 0, true));

  if (targetZoom !== null) {
    const tv = makeView({ ...view, zoom: targetZoom });
    layers.forEach((layer, i) => {
      const tz = layerLevel(layer, targetZoom, maxZoom, detail);
      if (tz !== layerLevel(layer, view.zoom, maxZoom, detail)) push(visible(tv, tz, 0), i, layer, 4);
    });
  }

  if (headingDeg !== null && speedMps > 2) {
    // смотрим вперёд на ~8 с хода, но не меньше половины экрана
    const mpp = metersPerPixel(latOfMercY(view.cy), view.zoom);
    const ahead = Math.max(speedMps * 8, mpp * view.h * 0.5);
    const p = moveMerc(view.cx, view.cy, headingDeg, ahead);
    const av = makeView({ ...view, cx: p.x, cy: p.y });
    // впереди — только подложка и дороги: надписи догрузятся при въезде
    layers.slice(0, 2).forEach((layer, i) => push(visible(av, layerLevel(layer, view.zoom, maxZoom, detail), 0), i, layer, 8));
  }

  reqs.sort((a, b) => a.priority - b.priority);
  return reqs.length > maxRequests ? reqs.slice(0, maxRequests) : reqs;
}
