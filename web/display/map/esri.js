// Тайлы Esri напрямую с arcgisonline (без прокси /tiles/ устройства): демо-режим и удалённый доступ
// (docs/contracts/demo.md, docs/contracts/remote.md). Условия Esri требуют подпись источников на карте.

export const ESRI_ATTRIBUTION = {
  imagery: "Esri, Maxar, Earthstar Geographics",
  street: "Esri, HERE, Garmin, © OpenStreetMap contributors",
};

/** Прямой адрес тайла по шаблонам TileSettings (imagery_url, …) + ключ Esri, если задан. */
export function esriTileUrl(settings, layer, z, x, y) {
  const t = settings.tiles || {};
  const tpl = t[`${layer}_url`];
  if (!tpl) return null;
  let url = tpl.replace("{z}", z).replace("{x}", x).replace("{y}", y);
  if (t.esri_api_key) url += (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(t.esri_api_key);
  return url;
}
