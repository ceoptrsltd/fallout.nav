// Отрисовка тайлов на Canvas с поворотом и дробным зумом.
//
// Точность: абсолютные world-px на z19 ~ 1.3e8 — больше точности float32 в матрицах Skia,
// поэтому смещения тайлов относительно машины считаются в JS (double), а в матрицу canvas
// попадают только небольшие числа.

import { TILE_SIZE, parentTile, tileKey, tileLevelFor, visibleTiles, worldSize } from "./mercator.js";

const DEG = Math.PI / 180;
export const BG_COLOR = "#141614";

/** Заглушка для отсутствующего тайла подложки: тёмная сетка (рисуется один раз). */
function makePlaceholder() {
  const c = typeof OffscreenCanvas === "function" ? new OffscreenCanvas(TILE_SIZE, TILE_SIZE) : document.createElement("canvas");
  c.width = TILE_SIZE;
  c.height = TILE_SIZE;
  const g = c.getContext("2d");
  g.fillStyle = "#1c1f1c";
  g.fillRect(0, 0, TILE_SIZE, TILE_SIZE);
  g.strokeStyle = "#2c302c";
  g.lineWidth = 2;
  for (let i = 0; i <= TILE_SIZE; i += 64) {
    g.beginPath();
    g.moveTo(i, 0);
    g.lineTo(i, TILE_SIZE);
    g.moveTo(0, i);
    g.lineTo(TILE_SIZE, i);
    g.stroke();
  }
  g.strokeStyle = "#383d38";
  g.strokeRect(1, 1, TILE_SIZE - 2, TILE_SIZE - 2);
  return c;
}

export class MapRenderer {
  constructor(store, { maxParentLevels = 3 } = {}) {
    this.store = store;
    this.maxParentLevels = maxParentLevels;
    this.placeholder = null;
    this.lastDrawn = { tiles: 0, fallbacks: 0, missing: 0 };
  }

  /**
   * Нарисовать слои. view — из makeView(); layers — activeLayers(); base — матрица холста
   * (например, pixelRatio), к которой добавляется трансформация карты.
   */
  draw(ctx, view, layers, maxZoom, pixelRatio = 1) {
    if (!this.placeholder) this.placeholder = makePlaceholder();
    const z = tileLevelFor(view.zoom, maxZoom);
    const k = Math.pow(2, view.zoom - z); // масштаб: px тайла уровня z → px экрана
    const ws = worldSize(z);
    const ox = view.cx * ws;
    const oy = view.cy * ws;
    const tiles = visibleTiles(view, z, 2);
    const stats = { tiles: 0, fallbacks: 0, missing: 0 };

    ctx.save();
    ctx.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    ctx.fillStyle = BG_COLOR;
    ctx.fillRect(0, 0, view.w, view.h);
    ctx.translate(view.ax, view.ay);
    ctx.rotate(-view.bearing * DEG);
    ctx.scale(k, k);
    ctx.imageSmoothingEnabled = true;

    for (const layer of layers) {
      // подложку рисуем с перекрытием ~1 px экрана, чтобы при повороте не было швов
      const ov = layer.base ? 1 / k : 0;
      const sz = TILE_SIZE + ov;
      for (const t of tiles) {
        const dx = t.x * TILE_SIZE - ox;
        const dy = t.y * TILE_SIZE - oy;
        const key = tileKey(layer.id, z, t.wx, t.y);
        const img = this.store.get(key);
        if (img) {
          ctx.drawImage(img, dx, dy, sz, sz);
          stats.tiles++;
          continue;
        }
        if (this._drawFallback(ctx, layer.id, z, t.wx, t.y, dx, dy, sz)) {
          stats.fallbacks++;
          continue;
        }
        stats.missing++;
        if (layer.base && this.store.isFailed(key)) ctx.drawImage(this.placeholder, dx, dy, sz, sz);
      }
    }
    ctx.restore();
    this.lastDrawn = stats;
    return stats;
  }

  /** Пока тайл грузится — масштабированный родитель (до 3 уровней вверх) или 4 потомка. */
  _drawFallback(ctx, layer, z, x, y, dx, dy, sz) {
    for (let kk = 1; kk <= this.maxParentLevels; kk++) {
      const p = parentTile(z, x, y, kk);
      if (!p) break;
      const img = this.store.get(tileKey(layer, p.z, p.x, p.y));
      if (img) {
        ctx.drawImage(img, p.sx, p.sy, p.size, p.size, dx, dy, sz, sz);
        return true;
      }
    }
    // при отдалении: потомки уровня z+1 уже могут быть в кэше
    const cz = z + 1;
    const half = TILE_SIZE / 2;
    let any = false;
    for (let i = 0; i < 4; i++) {
      const cx = x * 2 + (i & 1), cy = y * 2 + (i >> 1);
      const img = this.store.get(tileKey(layer, cz, cx, cy));
      if (img) {
        ctx.drawImage(img, dx + (i & 1) * half, dy + (i >> 1) * half, half + (sz - TILE_SIZE), half + (sz - TILE_SIZE));
        any = true;
      }
    }
    return any;
  }
}
