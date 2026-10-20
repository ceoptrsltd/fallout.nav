// Какие слои рисовать и какие тайлы запрашивать — чистые функции.

import { latOfMercY, makeView, metersPerPixel, moveMerc, tileKey, tileLevelFor, visibleTiles } from "./mercator.js";

/**
 * Слои снизу вверх по settings.display.layers: imagery|street → transport → places.
 * base = непрозрачная подложка (для неё — заглушки и перекрытие швов).
 */
export function activeLayers(settings) {
  const l = (settings && settings.display && settings.display.layers) || {};
  const out = [];
  if (l.street_map_instead) out.push({ id: "street", base: true });
  else if (l.imagery !== false) out.push({ id: "imagery", base: true });
  if (l.transport !== false) out.push({ id: "transport", base: false });
  if (l.places !== false) out.push({ id: "places", base: false });
  return out;
}

export function tileUrl(layer, z, x, y) {
  return `/tiles/${layer}/${z}/${x}/${y}`;
}

/**
 * Список запросов тайлов с приоритетами (меньше = раньше):
 *   1) видимые тайлы текущего уровня (подложка раньше надписей, ближние к машине раньше);
 *   2) уровень целевого зума (если отличается) — заранее, к плавной смене масштаба;
 *   3) область впереди по курсу (lookahead), чтобы тайлы были готовы до въезда в кадр.
 */
export function planRequests({ view, layers, maxZoom, targetZoom = null, headingDeg = null, speedMps = 0, margin = 48, maxRequests = 160 }) {
  const reqs = [];
  const z = tileLevelFor(view.zoom, maxZoom);
  const push = (tiles, layerIdx, layer, basePri) => {
    for (const t of tiles) {
      reqs.push({
        key: tileKey(layer.id, t.z, t.wx, t.y),
        url: tileUrl(layer.id, t.z, t.wx, t.y),
        priority: basePri + layerIdx * 0.75 + t.d * 0.25,
      });
    }
  };
  const vis = visibleTiles(view, z, margin);
  layers.forEach((layer, i) => push(vis, i, layer, 0));

  if (targetZoom !== null) {
    const tz = tileLevelFor(targetZoom, maxZoom);
    if (tz !== z) {
      const tv = visibleTiles(makeView({ ...view, zoom: targetZoom }), tz, 0);
      layers.forEach((layer, i) => push(tv, i, layer, 4));
    }
  }

  if (headingDeg !== null && speedMps > 2) {
    // смотрим вперёд на ~8 с хода, но не меньше половины экрана
    const mpp = metersPerPixel(latOfMercY(view.cy), view.zoom);
    const ahead = Math.max(speedMps * 8, mpp * view.h * 0.5);
    const p = moveMerc(view.cx, view.cy, headingDeg, ahead);
    const av = makeView({ ...view, cx: p.x, cy: p.y });
    const ahv = visibleTiles(av, z, 0);
    // впереди — только подложка и дороги: надписи догрузятся при въезде
    layers.slice(0, 2).forEach((layer, i) => push(ahv, i, layer, 8));
  }

  reqs.sort((a, b) => a.priority - b.priority);
  return reqs.length > maxRequests ? reqs.slice(0, maxRequests) : reqs;
}
