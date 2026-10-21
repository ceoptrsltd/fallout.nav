import test from "node:test";
import assert from "node:assert/strict";

import { NavLogic, zoomForSpeed, hasFix } from "../../web/display/demo/navlogic.js";
import { DemoSim, LoopRoute } from "../../web/display/demo/sim.js";
import { LV_ROUTE } from "../../web/display/demo/route.js";
import { parseCommand, settingsPatch } from "../../web/display/demo/bridge.js";
import { esriTileUrl } from "../../web/display/demo/demo.js";
import { DEFAULT_SETTINGS, deepMerge } from "../../web/display/map/defaults.js";

const NAV = DEFAULT_SETTINGS.nav;
const DISP = DEFAULT_SETTINGS.display;
const gps = (speed, fix = "3d") => ({ fix, lat: 36.11, lon: -115.17, speed_mps: speed, age_s: 0.05 });

// ── порт navlogic ──

test("zoomForSpeed: ступени таблицы (как fnav/core/navlogic.py)", () => {
  assert.equal(zoomForSpeed(NAV, 0), 19);
  assert.equal(zoomForSpeed(NAV, 4.99), 19);
  assert.equal(zoomForSpeed(NAV, 5), 18);
  assert.equal(zoomForSpeed(NAV, 12), 17);
  assert.equal(zoomForSpeed(NAV, 20), 16);
  assert.equal(zoomForSpeed(NAV, 40), 15);
  assert.equal(zoomForSpeed({ zoom_table: [] }, 10), 17);
  // порядок строк таблицы не важен
  assert.equal(zoomForSpeed({ zoom_table: [{ min_mps: 10, zoom: 16 }, { min_mps: 0, zoom: 18 }] }, 3), 18);
});

test("hasFix: фикс, координаты, устаревание", () => {
  assert.equal(hasFix(gps(1)), true);
  assert.equal(hasFix(gps(1, "none")), false);
  assert.equal(hasFix({ ...gps(1), lat: null }), false);
  assert.equal(hasFix({ ...gps(1), age_s: 6 }), false);
});

test("NavLogic: гистерезис карта↔камера с выдержкой switch_delay_s", () => {
  const n = new NavLogic(NAV, DISP, { startTime: 0 });
  assert.equal(n.update(0, gps(10)).mode, "map"); // первый фикс — сразу по скорости
  // ниже 2.0 м/с — переключение только через 2 с
  assert.equal(n.update(1, gps(1.5)).mode, "map");
  assert.equal(n.update(2.9, gps(1.5)).mode, "map");
  assert.equal(n.update(3.0, gps(1.5)).mode, "camera");
  // между порогами (2.0…2.5) — остаётся камера
  assert.equal(n.update(10, gps(2.3)).mode, "camera");
  // выше 2.5 — обратно через 2 с; кратковременный выброс выдержку сбрасывает
  assert.equal(n.update(11, gps(3)).mode, "camera");
  assert.equal(n.update(12, gps(2.2)).mode, "camera");
  assert.equal(n.update(13, gps(3)).mode, "camera");
  assert.equal(n.update(14.9, gps(3)).mode, "camera");
  const d = n.update(15, gps(3));
  assert.equal(d.mode, "map");
  assert.equal(d.reason, "speed");
});

test("NavLogic: зум меняется после zoom_delay_s, первая ступень — сразу", () => {
  const n = new NavLogic(NAV, DISP, { startTime: 0 });
  assert.equal(n.update(0, gps(12)).target_zoom, 17);
  assert.equal(n.update(1, gps(20)).target_zoom, 17);
  assert.equal(n.update(3.9, gps(20)).target_zoom, 17);
  assert.equal(n.update(4.0, gps(20)).target_zoom, 16);
  // новая кандидатура сбрасывает выдержку
  n.update(5, gps(30));
  n.update(6, gps(12));
  assert.equal(n.update(8.5, gps(12)).target_zoom, 16);
  assert.equal(n.update(9.0, gps(12)).target_zoom, 17);
});

test("NavLogic: нет фикса, startup grace, forced", () => {
  const n = new NavLogic(NAV, DISP, { startTime: 0 });
  let d = n.update(1, gps(0, "none"));
  assert.deepEqual([d.mode, d.reason], ["camera", "startup"]);
  d = n.update(6, gps(0, "none"));
  assert.equal(d.reason, "no_fix");
  n.applySettings(NAV, { ...DISP, force_mode: "map" });
  d = n.update(7, gps(0, "none"));
  assert.deepEqual([d.mode, d.reason], ["map", "forced"]);
  n.applySettings(NAV, { ...DISP, force_mode: "auto" });
  assert.equal(n.update(8, gps(15)).mode, "map");
});

// ── симулятор ──

test("LoopRoute: маршрут — замкнутая петля в Лас-Вегасе", () => {
  const r = new LoopRoute(LV_ROUTE);
  assert.ok(r.total > 5000 && r.total < 8000, `длина ${r.total}`);
  for (const [lat, lon] of LV_ROUTE) {
    assert.ok(lat > 36.09 && lat < 36.13 && lon > -115.18 && lon < -115.16);
  }
  const p = r.at(r.total + 10);
  const q = r.at(10);
  assert.ok(Math.abs(p.lat - q.lat) < 1e-9 && Math.abs(p.lon - q.lon) < 1e-9);
  // первый участок — Strip на север
  assert.ok(r.at(300).course < 10 || r.at(300).course > 350);
});

function drive(sim, seconds, dt = 0.1, cb = null) {
  for (let i = 0; i < seconds / dt; i++) {
    sim.step(dt);
    if (cb) cb(sim, i * dt);
  }
}

test("DemoSim: машина едет по маршруту, стоит на светофорах, проходит круг", () => {
  const sim = new DemoSim();
  const start = sim.dist;
  let stopped = 0, maxSpeed = 0, wasWaiting = false, stopsSeen = 0;
  const courses = new Set();
  drive(sim, 900, 0.1, (s) => {
    maxSpeed = Math.max(maxSpeed, s.speed);
    if (s.speed < 0.1) stopped += 0.1;
    if (s.waiting && !wasWaiting) stopsSeen++;
    wasWaiting = s.waiting;
    courses.add(Math.round(s.route.at(s.dist).course / 45) % 8);
  });
  assert.ok(sim.dist - start > sim.route.total, `за 15 мин не прошли круг: ${sim.dist - start}`);
  assert.ok(stopsSeen >= 6, `остановок ${stopsSeen}`);
  assert.ok(stopped > 60, `стояли ${stopped} с`);
  assert.ok(maxSpeed > 17 && maxSpeed < 22, `макс. скорость ${maxSpeed}`);
  assert.ok(courses.size >= 4, "курс по маршруту меняется (повороты)");
});

test("DemoSim: телеметрия + navlogic дают и карту, и камеру, и разные зумы", () => {
  const sim = new DemoSim();
  const nav = new NavLogic(NAV, DISP, { startTime: 0 });
  const modes = new Set(), zooms = new Set();
  let t = 0;
  drive(sim, 600, 0.1, (s) => {
    t += 0.1;
    const tel = s.telemetry(1.7e9 + t);
    const d = nav.update(t, tel.gps);
    modes.add(d.mode);
    zooms.add(d.target_zoom);
    assert.equal(tel.gps.fix, "3d");
    assert.ok(["gps", "mag", "blend"].includes(tel.heading.source));
  });
  assert.deepEqual([...modes].sort(), ["camera", "map"]);
  assert.ok(zooms.size >= 3, `зумы ${[...zooms]}`);
});

test("DemoSim: speedOverride держит скорость и проезжает светофоры; пауза — скорость 0", () => {
  const sim = new DemoSim();
  sim.speedOverride = 25;
  let waits = 0;
  drive(sim, 120, 0.1, (s) => {
    if (s.waiting) waits++;
  });
  assert.equal(waits, 0);
  assert.ok(Math.abs(sim.speed - 25) < 1e-6);
  sim.speedOverride = 0;
  drive(sim, 20);
  assert.equal(sim.speed, 0);
  sim.speedOverride = null;
  drive(sim, 10);
  assert.ok(sim.speed > 0);
  assert.equal(sim.telemetry(0, { paused: true }).gps.speed_mps, 0);
});

// ── мост postMessage ──

test("bridge: разбор команд строго по контракту", () => {
  assert.equal(parseCommand({ type: "other" }), null);
  assert.deepEqual(parseCommand({ type: "fnav-demo", play: false, speedOverride: 12.5, orientation: "north_up" }), {
    play: false,
    speedOverride: 12.5,
    orientation: "north_up",
  });
  assert.deepEqual(parseCommand({ type: "fnav-demo", speedOverride: null, layer: "street", forceMode: "camera", compassStyle: "v3" }), {
    speedOverride: null,
    layer: "street",
    forceMode: "camera",
    compassStyle: "v3",
  });
  // мусор отбрасывается
  assert.deepEqual(parseCommand({ type: "fnav-demo", play: "yes", orientation: "south_up", layer: "x", compassStyle: "<b>", speedOverride: "9" }), {});
  const p = settingsPatch({ orientation: "north_up", compassStyle: "v2", forceMode: "map", layer: "street" });
  assert.deepEqual(p, { display: { orientation: "north_up", compass_style: "v2", force_mode: "map", layers: { street_map_instead: true } } });
  const merged = deepMerge(DEFAULT_SETTINGS, p);
  assert.equal(merged.display.layers.transport, true);
  assert.equal(settingsPatch({ play: true }), null);
});

test("esriTileUrl: прямой URL Esri по шаблону, порядок z/y/x", () => {
  const u = esriTileUrl(DEFAULT_SETTINGS, "imagery", 16, 12000, 25000);
  assert.equal(u, "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/16/25000/12000");
  const k = esriTileUrl(deepMerge(DEFAULT_SETTINGS, { tiles: { esri_api_key: "abc" } }), "places", 1, 0, 1);
  assert.match(k, /\/tile\/1\/1\/0\?token=abc$/);
});
