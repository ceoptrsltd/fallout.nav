import test from "node:test";
import assert from "node:assert/strict";

import { TileStore } from "../../web/display/map/tiles.js";
import { activeLayers, planRequests } from "../../web/display/map/layers.js";
import { Trail } from "../../web/display/map/trail.js";
import { lonLatToMerc, makeView, moveMerc } from "../../web/display/map/mercator.js";
import { DEFAULT_SETTINGS, deepMerge } from "../../web/display/map/defaults.js";

const tick = () => new Promise((r) => setImmediate(r));

function fakeLoader() {
  const calls = [];
  const loader = (url, signal) =>
    new Promise((resolve, reject) => {
      const c = { url, signal, resolve, reject, closed: false };
      calls.push(c);
      signal.addEventListener("abort", () => reject(Object.assign(new Error("abort"), { name: "AbortError" })));
    });
  return { calls, loader };
}

const img = (name) => ({ name, closed: false, close() { this.closed = true; } });
const req = (k, p = 0) => ({ key: k, url: "/tiles/" + k, priority: p });

test("TileStore: не больше 6 параллельных загрузок, по приоритету", async () => {
  const { calls, loader } = fakeLoader();
  let now = 0;
  const s = new TileStore({ loader, now: () => now, maxConcurrent: 6 });
  s.want(Array.from({ length: 20 }, (_, i) => req("t" + i, 20 - i)));
  assert.equal(calls.length, 6);
  assert.equal(calls[0].url, "/tiles/t19"); // наименьший priority — первым
  calls[0].resolve(img("a"));
  await tick();
  assert.equal(calls.length, 7);
  assert.ok(s.get("t19"));
  assert.equal(s.inflight.size, 6);
});

test("TileStore: LRU-вытеснение закрывает ImageBitmap", async () => {
  const { calls, loader } = fakeLoader();
  const s = new TileStore({ loader, now: () => 0, capacity: 3, maxConcurrent: 10 });
  s.want(["a", "b", "c", "d"].map((k) => req(k)));
  const imgs = calls.map((c) => img(c.url));
  calls.forEach((c, i) => c.resolve(imgs[i]));
  await tick();
  assert.equal(s.cache.size, 3);
  assert.equal(imgs[0].closed, true);
  assert.equal(s.get("a"), null);
  // использование продлевает жизнь
  s.get("b");
  s._put("e", img("e"));
  assert.ok(s.has("b"));
  assert.ok(!s.has("c"));
});

test("TileStore: ненужные загрузки отменяются после grace", async () => {
  const { calls, loader } = fakeLoader();
  let now = 0;
  const s = new TileStore({ loader, now: () => now, graceS: 1 });
  s.want([req("x"), req("y")]);
  assert.equal(calls.length, 2);
  now = 0.5;
  s.want([req("y")]);
  assert.equal(calls[0].signal.aborted, false); // ещё в пределах grace
  now = 1.6;
  s.want([req("y"), req("z")]);
  assert.equal(calls[0].signal.aborted, true);
  assert.equal(calls[1].signal.aborted, false);
  assert.equal(calls.length, 3);
  await tick();
  assert.equal(s.isFailed("x"), false); // отмена — не ошибка
});

test("TileStore: 404 → backoff, без повторных запросов", async () => {
  const { calls, loader } = fakeLoader();
  let now = 0;
  const s = new TileStore({ loader, now: () => now });
  s.want([req("q")]);
  calls[0].reject(Object.assign(new Error("HTTP 404"), { status: 404 }));
  await tick();
  assert.ok(s.isFailed("q"));
  for (let i = 0; i < 10; i++) {
    now += 1;
    s.want([req("q")]);
  }
  assert.equal(calls.length, 1, "повторный запрос до истечения backoff");
  now = 16;
  s.want([req("q")]);
  assert.equal(calls.length, 2);
  calls[1].reject(Object.assign(new Error("HTTP 404"), { status: 404 }));
  await tick();
  now = 40; // второй backoff = 30 с
  s.want([req("q")]);
  assert.equal(calls.length, 2);
  now = 47;
  s.want([req("q")]);
  assert.equal(calls.length, 3);
  calls[2].resolve(img("ok"));
  await tick();
  assert.equal(s.isFailed("q"), false);
  assert.ok(s.get("q"));
});

test("TileStore: результат отменённой загрузки закрывается и не попадает в кэш", async () => {
  let resolveIt;
  const loader = () => new Promise((r) => (resolveIt = r));
  let now = 0;
  const s = new TileStore({ loader, now: () => now, graceS: 0.1 });
  s.want([req("a")]);
  now = 1;
  s.want([]);
  const im = img("late");
  resolveIt(im);
  await tick();
  assert.equal(s.has("a"), false);
  assert.equal(im.closed, true);
});

test("activeLayers: порядок и street_map_instead", () => {
  const d = DEFAULT_SETTINGS;
  assert.deepEqual(activeLayers(d).map((l) => l.id), ["imagery", "transport", "places"]);
  const st = deepMerge(d, { display: { layers: { street_map_instead: true, places: false } } });
  assert.deepEqual(activeLayers(st).map((l) => l.id), ["street", "transport"]);
  assert.equal(activeLayers(st)[0].base, true);
  const none = deepMerge(d, { display: { layers: { imagery: false } } });
  assert.deepEqual(activeLayers(none).map((l) => l.id), ["transport", "places"]);
});

test("planRequests: видимые раньше предзагрузки, подложка раньше надписей", () => {
  const c = lonLatToMerc(37.5853, 55.7069);
  const view = makeView({ cx: c.x, cy: c.y, zoom: 17.2, bearing: 225, ax: 384, ay: 386, w: 768, h: 576 });
  const reqs = planRequests({ view, layers: activeLayers(DEFAULT_SETTINGS), maxZoom: 19, targetZoom: 18, headingDeg: 225, speedMps: 15 });
  assert.ok(reqs.length > 0 && reqs.length <= 160);
  assert.match(reqs[0].key, /^imagery\/17\//);
  assert.match(reqs[0].url, /^\/tiles\/imagery\/17\/\d+\/\d+$/);
  const firstPrefetch = reqs.findIndex((r) => r.key.includes("/18/"));
  const lastVisible = reqs.map((r) => r.key.startsWith("imagery/17/")).lastIndexOf(true);
  assert.ok(firstPrefetch > 0);
  assert.ok(reqs[firstPrefetch].priority >= 4);
  assert.ok(lastVisible >= 0);
});

test("deepMerge не меняет аргументы, массивы заменяет", () => {
  const a = { x: { y: 1, z: [1, 2] }, k: 1 };
  const b = deepMerge(a, { x: { z: [3] } });
  assert.deepEqual(b, { x: { y: 1, z: [3] }, k: 1 });
  assert.deepEqual(a.x.z, [1, 2]);
});

test("Trail: прореживание, возраст, обрезка", () => {
  const tr = new Trail();
  const c = lonLatToMerc(37.5853, 55.7069);
  let p = { ...c };
  for (let i = 0; i < 100; i++) {
    tr.add(p.x, p.y, i * 0.1);
    p = moveMerc(p.x, p.y, 90, 1); // 10 м/с
  }
  assert.ok(tr.pts.length >= 30 && tr.pts.length <= 51, String(tr.pts.length));
  tr.prune(10, 5);
  assert.ok(tr.pts.every((q) => 10 - q.t <= 5));
  const view = makeView({ cx: p.x, cy: p.y, zoom: 18, bearing: 90, ax: 384, ay: 386, w: 768, h: 576 });
  const pts = tr.project(view, 10, { mx: p.x, my: p.y });
  const last = pts[pts.length - 1];
  assert.equal(last.age_s, 0);
  assert.ok(Math.abs(last.x - 384) < 1e-6 && Math.abs(last.y - 386) < 1e-6);
  // heading_up на восток: след позади — ниже машины на экране
  assert.ok(pts[0].y > 386);
});

test("TileStore: сервер недоступен — общая пауза загрузок, без спама", async () => {
  const { calls, loader } = fakeLoader();
  let now = 0;
  const s = new TileStore({ loader, now: () => now, maxConcurrent: 6 });
  const reqs = Array.from({ length: 30 }, (_, i) => req("n" + i, i));
  s.want(reqs);
  assert.equal(calls.length, 6);
  calls.slice(0, 6).forEach((c) => c.reject(new TypeError("Failed to fetch")));
  await tick();
  assert.equal(calls.length, 6, "во время паузы новых запросов нет");
  assert.equal(s.failures.size, 0, "сетевая ошибка не помечает тайлы");
  assert.equal(s.netBackoff, 1, "одна пауза на волну ошибок");
  now = 0.5;
  s.want(reqs);
  assert.equal(calls.length, 6);
  now = 1.1;
  s.want(reqs);
  assert.equal(calls.length, 12);
  calls[6].resolve(img("ok"));
  await tick();
  assert.equal(s.netBackoff, 0);
});
