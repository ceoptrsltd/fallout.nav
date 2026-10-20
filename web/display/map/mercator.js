// Web Mercator (EPSG:3857) — чистые функции, без DOM. Тестируются в tests/display/.
//
// Координаты:
//   * merc — нормализованные [0..1): x = 0 на -180°, y = 0 на +85.05°;
//   * world px на уровне z — merc * 256 * 2^z;
//   * screen — пиксели логического холста 768×576.
//
// Вид (view): { cx, cy, zoom, bearing, ax, ay, w, h }
//   cx, cy — точка карты (merc), которая стоит в экранной точке (ax, ay) (= позиция машины);
//   bearing — на сколько градусов повёрнута карта (курс, смотрящий вверх); 0 = север вверху.
//   screen = A + R(-bearing) · (m - c) · worldSize(zoom)

export const TILE_SIZE = 256;
export const EARTH_CIRCUMFERENCE_M = 40075016.686;
export const MAX_LAT = 85.0511287798066;

const DEG = Math.PI / 180;

export function clamp(v, lo, hi) {
  return v < lo ? lo : v > hi ? hi : v;
}

export function lonLatToMerc(lon, lat) {
  const la = clamp(lat, -MAX_LAT, MAX_LAT) * DEG;
  const x = (lon + 180) / 360;
  const y = (1 - Math.log(Math.tan(la) + 1 / Math.cos(la)) / Math.PI) / 2;
  return { x, y };
}

export function mercToLonLat(x, y) {
  const lon = x * 360 - 180;
  const n = Math.PI - 2 * Math.PI * y;
  const lat = Math.atan(Math.sinh(n)) / DEG;
  return { lon, lat };
}

/** Сколько метров на местности в одной единице merc на широте lat. */
export function metersPerMercUnit(lat) {
  return EARTH_CIRCUMFERENCE_M * Math.cos(clamp(lat, -MAX_LAT, MAX_LAT) * DEG);
}

/** Широта по merc-y (быстро, для пересчёта масштаба). */
export function latOfMercY(y) {
  return Math.atan(Math.sinh(Math.PI - 2 * Math.PI * y)) / DEG;
}

export function worldSize(z) {
  return TILE_SIZE * Math.pow(2, z);
}

/** Метров в одном пикселе экрана на зуме zoom и широте lat. */
export function metersPerPixel(lat, zoom) {
  return metersPerMercUnit(lat) / worldSize(zoom);
}

/** Сместить merc-точку на meters по азимуту bearingDeg (локально-плоско, для малых расстояний). */
export function moveMerc(mx, my, bearingDeg, meters) {
  const k = meters / metersPerMercUnit(latOfMercY(my));
  const b = bearingDeg * DEG;
  return { x: mx + Math.sin(b) * k, y: my - Math.cos(b) * k };
}

/** Расстояние между merc-точками в метрах (локально-плоско). */
export function mercDistanceM(ax, ay, bx, by) {
  const m = metersPerMercUnit(latOfMercY((ay + by) / 2));
  return Math.hypot(bx - ax, by - ay) * m;
}

/** Целый уровень тайлов для дробного зума: ближайший, в пределах [minZ, maxZ]. */
export function tileLevelFor(zoom, maxZ, minZ = 0) {
  return clamp(Math.round(zoom), minZ, maxZ);
}

export function wrapTileX(x, z) {
  const n = 1 << z;
  return ((x % n) + n) % n;
}

export function tileKey(layer, z, x, y) {
  return layer + "/" + z + "/" + x + "/" + y;
}

// ───────────── преобразования вида ─────────────

export function makeView(v) {
  const scale = worldSize(v.zoom);
  const b = (v.bearing || 0) * DEG;
  return { ...v, scale, cos: Math.cos(b), sin: Math.sin(b) };
}

/** merc → экран. */
export function mercToScreen(view, mx, my) {
  const dx = (mx - view.cx) * view.scale;
  const dy = (my - view.cy) * view.scale;
  // поворот на -bearing: (x cos + y sin, -x sin + y cos)
  return {
    x: view.ax + dx * view.cos + dy * view.sin,
    y: view.ay - dx * view.sin + dy * view.cos,
  };
}

/** экран → merc. */
export function screenToMerc(view, sx, sy) {
  const px = sx - view.ax;
  const py = sy - view.ay;
  // поворот на +bearing: (x cos - y sin, x sin + y cos)
  const dx = px * view.cos - py * view.sin;
  const dy = px * view.sin + py * view.cos;
  return { x: view.cx + dx / view.scale, y: view.cy + dy / view.scale };
}

/**
 * Тайлы уровня z, пересекающие экран (с полем margin px), с учётом поворота (точная проверка SAT
 * двух прямоугольников). Возвращает [{z, x (развёрнутый, для позиции), wx (по модулю), y, d}],
 * отсортированные по расстоянию d (в тайлах) от точки (ax, ay).
 */
export function visibleTiles(view, z, margin = 0) {
  const n = 1 << z;
  const x0s = -margin, y0s = -margin, x1s = view.w + margin, y1s = view.h + margin;
  const corners = [
    screenToMerc(view, x0s, y0s),
    screenToMerc(view, x1s, y0s),
    screenToMerc(view, x1s, y1s),
    screenToMerc(view, x0s, y1s),
  ];
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const c of corners) {
    const tx = c.x * n, ty = c.y * n;
    if (tx < minX) minX = tx;
    if (tx > maxX) maxX = tx;
    if (ty < minY) minY = ty;
    if (ty > maxY) maxY = ty;
  }
  const tx0 = Math.floor(minX), tx1 = Math.floor(maxX);
  const ty0 = Math.max(0, Math.floor(minY)), ty1 = Math.min(n - 1, Math.floor(maxY));
  const out = [];
  if (!(tx1 - tx0 < 4096 && ty1 - ty0 < 4096)) return out; // защита от вырожденного вида
  const anchor = screenToMerc(view, view.ax, view.ay);
  const rotated = Math.abs(view.sin) > 1e-6;
  for (let ty = ty0; ty <= ty1; ty++) {
    for (let tx = tx0; tx <= tx1; tx++) {
      if (rotated) {
        // проекция тайла на оси экрана: AABB его экранных углов против прямоугольника экрана
        let sx0 = Infinity, sy0 = Infinity, sx1 = -Infinity, sy1 = -Infinity;
        for (let i = 0; i < 4; i++) {
          const p = mercToScreen(view, (tx + (i & 1)) / n, (ty + (i >> 1)) / n);
          if (p.x < sx0) sx0 = p.x;
          if (p.x > sx1) sx1 = p.x;
          if (p.y < sy0) sy0 = p.y;
          if (p.y > sy1) sy1 = p.y;
        }
        if (sx1 < x0s || sx0 > x1s || sy1 < y0s || sy0 > y1s) continue;
      }
      const d = Math.hypot(tx + 0.5 - anchor.x * n, ty + 0.5 - anchor.y * n);
      out.push({ z, x: tx, wx: wrapTileX(tx, z), y: ty, d });
    }
  }
  out.sort((a, b) => a.d - b.d);
  return out;
}

/**
 * Родительский тайл на k уровней выше и под-прямоугольник в нём (в px исходного 256×256),
 * соответствующий тайлу (z, x, y).
 */
export function parentTile(z, x, y, k) {
  const pz = z - k;
  if (pz < 0) return null;
  const px = x >> k, py = y >> k;
  const size = TILE_SIZE / (1 << k);
  return { z: pz, x: px, y: py, sx: (x - (px << k)) * size, sy: (y - (py << k)) * size, size };
}
