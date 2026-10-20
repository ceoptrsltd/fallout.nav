// node --test tests/display/
import test from "node:test";
import assert from "node:assert/strict";

import {
  lonLatToMerc,
  mercToLonLat,
  makeView,
  mercToScreen,
  screenToMerc,
  visibleTiles,
  parentTile,
  tileLevelFor,
  wrapTileX,
  moveMerc,
  mercDistanceM,
  metersPerPixel,
} from "../../web/display/map/mercator.js";

const close = (a, b, eps, msg) => assert.ok(Math.abs(a - b) <= eps, `${msg ?? ""} ${a} != ${b} (±${eps})`);

// Ленинский проспект (начало мок-маршрута)
const LAT = 55.7069, LON = 37.5853;

test("lonLat ↔ merc: туда-обратно и известные точки", () => {
  const m = lonLatToMerc(LON, LAT);
  const ll = mercToLonLat(m.x, m.y);
  close(ll.lat, LAT, 1e-9);
  close(ll.lon, LON, 1e-9);
  const o = lonLatToMerc(0, 0);
  close(o.x, 0.5, 1e-12);
  close(o.y, 0.5, 1e-12);
  // тайл z19, в котором стоит машина (сверено с формулой OSM slippy map)
  const n = 2 ** 19;
  const tx = Math.floor(m.x * n), ty = Math.floor(m.y * n);
  const latR = (LAT * Math.PI) / 180;
  const ty2 = Math.floor(((1 - Math.log(Math.tan(latR) + 1 / Math.cos(latR)) / Math.PI) / 2) * n);
  assert.equal(tx, Math.floor(((LON + 180) / 360) * n));
  assert.equal(ty, ty2);
});

test("масштаб: метров на пиксель на z19 в Москве ≈ 0.168", () => {
  close(metersPerPixel(LAT, 19), 0.1682, 0.001);
});

test("вид без поворота: центр в якоре, север вверху", () => {
  const c = lonLatToMerc(LON, LAT);
  const v = makeView({ cx: c.x, cy: c.y, zoom: 17, bearing: 0, ax: 384, ay: 288, w: 768, h: 576 });
  const s = mercToScreen(v, c.x, c.y);
  close(s.x, 384, 1e-6);
  close(s.y, 288, 1e-6);
  const north = moveMerc(c.x, c.y, 0, 100);
  const sn = mercToScreen(v, north.x, north.y);
  close(sn.x, 384, 1e-6);
  assert.ok(sn.y < 288);
});

test("heading_up: точка впереди по курсу оказывается прямо над машиной", () => {
  const c = lonLatToMerc(LON, LAT);
  for (const hdg of [0, 45, 90, 213.7, 300]) {
    const v = makeView({ cx: c.x, cy: c.y, zoom: 18.3, bearing: hdg, ax: 384, ay: 386, w: 768, h: 576 });
    const ahead = moveMerc(c.x, c.y, hdg, 50);
    const s = mercToScreen(v, ahead.x, ahead.y);
    close(s.x, 384, 1e-6, `hdg ${hdg}`);
    const mpp = metersPerPixel(LAT, 18.3);
    close(386 - s.y, 50 / mpp, 0.05, `hdg ${hdg}`);
    // точка справа по курсу — справа на экране
    const right = moveMerc(c.x, c.y, hdg + 90, 30);
    assert.ok(mercToScreen(v, right.x, right.y).x > 384);
  }
});

test("screenToMerc обратно mercToScreen", () => {
  const c = lonLatToMerc(LON, LAT);
  const v = makeView({ cx: c.x, cy: c.y, zoom: 16.7, bearing: 137, ax: 384, ay: 386, w: 768, h: 576 });
  for (const [x, y] of [[0, 0], [768, 0], [100, 500], [384, 386]]) {
    const m = screenToMerc(v, x, y);
    const s = mercToScreen(v, m.x, m.y);
    close(s.x, x, 1e-6);
    close(s.y, y, 1e-6);
  }
});

function coverageOk(v, z, tiles) {
  // каждая точка экрана сетки должна попасть в какой-то из выбранных тайлов
  const n = 2 ** z;
  const set = new Set(tiles.map((t) => t.x + "," + t.y));
  for (let x = 0; x <= v.w; x += 16)
    for (let y = 0; y <= v.h; y += 16) {
      const m = screenToMerc(v, x, y);
      if (!set.has(Math.floor(m.x * n) + "," + Math.floor(m.y * n))) return false;
    }
  return true;
}

test("visibleTiles покрывает экран без поворота и с поворотом, сортировка от машины", () => {
  const c = lonLatToMerc(LON, LAT);
  for (const bearing of [0, 30, 45, 247.3]) {
    for (const zoom of [15.5, 17, 18.49, 19]) {
      const z = tileLevelFor(zoom, 19);
      const v = makeView({ cx: c.x, cy: c.y, zoom, bearing, ax: 384, ay: 386, w: 768, h: 576 });
      const tiles = visibleTiles(v, z, 0);
      assert.ok(coverageOk(v, z, tiles), `покрытие b=${bearing} z=${zoom}`);
      assert.ok(tiles.length <= 40, `слишком много тайлов: ${tiles.length}`);
      for (let i = 1; i < tiles.length; i++) assert.ok(tiles[i].d >= tiles[i - 1].d);
    }
  }
});

test("visibleTiles с поворотом отсекает углы bbox (SAT)", () => {
  const c = lonLatToMerc(LON, LAT);
  const v = makeView({ cx: c.x, cy: c.y, zoom: 17.5, bearing: 45, ax: 384, ay: 288, w: 768, h: 576 });
  const z = 18;
  const tiles = visibleTiles(v, z, 0);
  const xs = tiles.map((t) => t.x), ys = tiles.map((t) => t.y);
  const bbox = (Math.max(...xs) - Math.min(...xs) + 1) * (Math.max(...ys) - Math.min(...ys) + 1);
  assert.ok(tiles.length < bbox, `${tiles.length} < ${bbox}`);
});

test("tileLevelFor: ближайший целый, ограничение max_zoom", () => {
  assert.equal(tileLevelFor(17.4, 19), 17);
  assert.equal(tileLevelFor(17.6, 19), 18);
  assert.equal(tileLevelFor(19.8, 19), 19);
  assert.equal(tileLevelFor(-1, 19), 0);
});

test("parentTile: под-прямоугольник", () => {
  const p = parentTile(19, 316459, 163934, 1);
  assert.deepEqual(p, { z: 18, x: 158229, y: 81967, sx: 128, sy: 0, size: 128 });
  const p3 = parentTile(19, 316459, 163934, 3);
  assert.equal(p3.size, 32);
  assert.equal(p3.x, 316459 >> 3);
  assert.equal(p3.sx, (316459 % 8) * 32);
  assert.equal(parentTile(1, 0, 0, 2), null);
});

test("wrapTileX", () => {
  assert.equal(wrapTileX(-1, 3), 7);
  assert.equal(wrapTileX(8, 3), 0);
  assert.equal(wrapTileX(5, 3), 5);
});

test("moveMerc / mercDistanceM согласованы", () => {
  const c = lonLatToMerc(LON, LAT);
  const p = moveMerc(c.x, c.y, 213, 250);
  close(mercDistanceM(c.x, c.y, p.x, p.y), 250, 0.5);
});
