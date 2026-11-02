import test from "node:test";
import assert from "node:assert/strict";

import { norm360, wrap180, angleDiff, lerpAngle, approachAngle, springStep } from "../../web/display/map/angles.js";
import { CompassFilter, MotionModel, ZoomAnimator, OrientationBlend, RotationRig, hudSpeed, vehicleAnchor, vehicleCenter } from "../../web/display/map/motion.js";
import { lonLatToMerc, makeView, mercDistanceM, mercToScreen, metersPerPixel, moveMerc, mercToLonLat } from "../../web/display/map/mercator.js";

const close = (a, b, eps, msg) => assert.ok(Math.abs(a - b) <= eps, `${msg ?? ""} ${a} != ${b} (±${eps})`);

test("углы: нормализация и кратчайшая разность", () => {
  assert.equal(norm360(-10), 350);
  assert.equal(norm360(720), 0);
  assert.equal(wrap180(190), -170);
  assert.equal(wrap180(180), 180);
  assert.equal(angleDiff(350, 10), 20);
  assert.equal(angleDiff(10, 350), -20);
  close(lerpAngle(350, 10, 0.5), 0, 1e-9);
  close(lerpAngle(10, 350, 0.25), 5, 1e-9);
});

test("approachAngle идёт через 0, а не через 180", () => {
  let a = 355;
  for (let i = 0; i < 100; i++) {
    a = approachAngle(a, 5, 1 / 60, 0.2);
    assert.ok(a >= 355 || a <= 5, `ушли не туда: ${a}`);
  }
  close(a, 5, 0.01);
});

test("пружина зума: без перелёта, установление ~1.5 с", () => {
  let x = 19, v = 0, max = -Infinity, min = Infinity;
  let settledAt = null;
  for (let i = 0; i < 180; i++) {
    [x, v] = springStep(x, v, 17, 4, 1 / 60);
    max = Math.max(max, x);
    min = Math.min(min, x);
    if (settledAt === null && Math.abs(x - 17) < 0.02) settledAt = (i + 1) / 60;
  }
  assert.ok(min >= 17 - 1e-9, "перелёт");
  assert.ok(max <= 19);
  assert.ok(settledAt > 0.8 && settledAt < 2.2, `установление ${settledAt}`);
});

test("ZoomAnimator: первая цель — сразу, затем плавно", () => {
  const z = new ZoomAnimator(4);
  z.setTarget(18);
  assert.equal(z.update(1 / 60), 18);
  z.setTarget(16);
  const a = z.update(1 / 60);
  assert.ok(a < 18 && a > 17.9);
  for (let i = 0; i < 300; i++) z.update(1 / 60);
  assert.equal(z.z, 16);
  assert.ok(z.settled);
});

test("OrientationBlend + vehicleAnchor: машина плавно переезжает в нижнюю треть", () => {
  const o = new OrientationBlend(0.8);
  o.set("heading_up");
  assert.equal(o.update(0), 1);
  o.set("north_up");
  let f;
  for (let i = 0; i < 24; i++) f = o.update(1 / 60);
  assert.ok(f > 0.2 && f < 0.8, `середина ${f}`);
  for (let i = 0; i < 30; i++) o.update(1 / 60);
  assert.equal(o.update(1 / 60), 0);
  close(vehicleAnchor(1, 768, 576, 0.67).ay, 576 * 0.67, 1e-9);
  close(vehicleAnchor(0, 768, 576, 0.67).ay, 288, 1e-9);
  assert.equal(vehicleAnchor(0.3, 768, 576, 0.67).ax, 384);
});

// ── эффект поворота ──
const FPS = 60;
function run(rig, seconds, heading, speed = 12, cb = null) {
  const out = [];
  const n = Math.round(seconds * FPS);
  for (let i = 0; i < n; i++) {
    const h = typeof heading === "function" ? heading(i / FPS) : heading;
    const r = rig.update(1 / FPS, h, speed);
    out.push({ t: (i + 1) / FPS, ...r });
    if (cb) cb(r);
  }
  return out;
}
const unwrapFrom = (ref, a) => ref + angleDiff(ref, a);

test("RotationRig: иконка поворачивается первой, карта догоняет без перелёта", () => {
  const rig = new RotationRig({ vehicleLagS: 0.25, mapLagS: 1.5, deadbandDeg: 2 });
  run(rig, 2, 0);
  const tr = run(rig, 6, 90);
  const at = (t) => tr[Math.round(t * FPS) - 1];
  // 0.3 с: иконка уже повернулась сильно, карта едва тронулась (мягкий старт)
  assert.ok(at(0.3).vehicle > 55, `иконка ${at(0.3).vehicle}`);
  assert.ok(at(0.3).bearing < 10, `карта ${at(0.3).bearing}`);
  assert.ok(at(0.3).vehicleRot > 45, "иконка отклонена от вертикали");
  // за mapLagS ≈ 80 %, к 3 с почти совпали
  assert.ok(at(1.5).bearing > 55 && at(1.5).bearing < 85, `1.5 с: ${at(1.5).bearing}`);
  assert.ok(Math.abs(angleDiff(at(4).bearing, 90)) < 1.5, `4 с: ${at(4).bearing}`);
  // монотонно и без перелёта
  let prev = -1;
  for (const r of tr) {
    assert.ok(r.bearing >= prev - 1e-9, "карта пошла назад");
    assert.ok(r.bearing <= 90 + 1e-6, `перелёт ${r.bearing}`);
    prev = r.bearing;
  }
  // скорость поворота карты растёт плавно с нуля (нет рывка в начале)
  assert.ok(tr[0].bearing < 0.05, `рывок на старте ${tr[0].bearing}`);
  close(tr[tr.length - 1].vehicleRot > 180 ? tr[tr.length - 1].vehicleRot - 360 : tr[tr.length - 1].vehicleRot, 0, 0.3);
});

test("RotationRig: через 0/360 — кратчайшим путём", () => {
  const rig = new RotationRig({ vehicleLagS: 0.25, mapLagS: 1.5 });
  run(rig, 2, 350);
  const tr = run(rig, 6, 20);
  for (const r of tr) {
    const b = unwrapFrom(350, r.bearing);
    const v = unwrapFrom(350, r.vehicle);
    assert.ok(b >= 350 - 1e-6 && b <= 380 + 1e-6, `карта ушла длинным путём: ${r.bearing}`);
    assert.ok(v >= 350 - 1e-6 && v <= 380 + 1e-6, `иконка ушла длинным путём: ${r.vehicle}`);
    assert.ok(r.bearing >= 0 && r.bearing < 360);
  }
  close(angleDiff(tr[tr.length - 1].bearing, 20), 0, 0.5);
  // разворот на 180° (конец маршрута) — тоже без перелёта и без «рывка» через 360
  const back = run(rig, 8, 200);
  let prevU = 20;
  for (const r of back) {
    const u = unwrapFrom(prevU, r.bearing);
    assert.ok(Math.abs(u - prevU) < 3, "скачок угла за кадр");
    prevU = u;
  }
  close(angleDiff(back[back.length - 1].bearing, 200), 0, 0.5);
});

test("RotationRig: deadband — мелкие колебания курса карту не крутят", () => {
  const rig = new RotationRig({ vehicleLagS: 0.25, mapLagS: 1.5, deadbandDeg: 2 });
  run(rig, 3, 100);
  const b0 = rig.update(0, 100, 12).bearing;
  // руление ±1.2° с периодом 0.7 с
  const tr = run(rig, 5, (t) => 100 + 1.2 * Math.sin((2 * Math.PI * t) / 0.7));
  const maxDev = Math.max(...tr.map((r) => Math.abs(angleDiff(b0, r.bearing))));
  assert.ok(maxDev < 0.3, `карта дрожит на ${maxDev}°`);
  // иконка при этом следует (видно руление)
  const vDev = Math.max(...tr.map((r) => Math.abs(angleDiff(100, r.vehicle))));
  assert.ok(vDev > 0.5);
});

test("RotationRig: постоянный перекос внутри deadband досинхронизируется", () => {
  const rig = new RotationRig({ vehicleLagS: 0.25, mapLagS: 1.5, deadbandDeg: 2 });
  run(rig, 3, 100);
  const tr = run(rig, 0.9, 101.5);
  assert.ok(Math.abs(angleDiff(tr[tr.length - 1].bearing, 100)) < 0.2, "сразу не крутим");
  const tr2 = run(rig, 5, 101.5);
  close(angleDiff(tr2[tr2.length - 1].bearing, 101.5), 0, 0.3);
  for (const r of tr2) assert.ok(r.bearing <= 101.5 + 1e-6, "перелёт при досинхронизации");
});

test("RotationRig: north_up ↔ heading_up через ту же пружину", () => {
  const rig = new RotationRig({ mapLagS: 1.5 });
  rig.setOrientation("heading_up");
  run(rig, 3, 247.3);
  close(rig.update(0, 247.3, 12).bearing, 247.3, 0.3);
  rig.setOrientation("north_up");
  const tr = run(rig, 6, 247.3);
  // 247 → 0 кратчайшим путём через 300…360
  for (const r of tr) assert.ok(r.bearing >= 247 || r.bearing < 1e-6, `не тот путь: ${r.bearing}`);
  const last = tr[tr.length - 1];
  assert.ok(last.bearing < 0.5 || last.bearing > 359.5);
  close(last.vehicleRot, 247.3, 0.5); // north_up: иконка = курс
  rig.setOrientation("heading_up");
  const tr2 = run(rig, 6, 247.3);
  close(angleDiff(tr2[tr2.length - 1].bearing, 247.3), 0, 0.5);
});

test("RotationRig: на стоянке шум компаса не дёргает иконку и карту", () => {
  const rig = new RotationRig({ vehicleLagS: 0.25, mapLagS: 1.5, deadbandDeg: 2 });
  let seed = 7;
  const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647 - 0.5) * 2;
  run(rig, 3, 100, 0);
  // вход уже отфильтрован MotionModel; оставшийся шум ±1° с частотой пакетов 10 Гц
  let h = 100;
  const tr = [];
  for (let i = 0; i < 600; i++) {
    if (i % 6 === 0) h = 100 + rnd();
    tr.push(rig.update(1 / 60, h, 0));
  }
  let maxStep = 0;
  for (let i = 1; i < tr.length; i++) maxStep = Math.max(maxStep, Math.abs(angleDiff(tr[i - 1].vehicle, tr[i].vehicle)));
  assert.ok(maxStep < 0.15, `иконка дёргается на ${maxStep}° за кадр`);
  const bDev = Math.max(...tr.map((r) => Math.abs(angleDiff(100, r.bearing))));
  assert.ok(bDev < 1.2, `карта гуляет на ${bDev}°`);
});

test("RotationRig: нулевые постоянные — мгновенно; неизвестный курс держится", () => {
  const rig = new RotationRig({ vehicleLagS: 0, mapLagS: 0, deadbandDeg: 0 });
  rig.update(1 / 60, 10, 12);
  const r = rig.update(1 / 60, 80, 12);
  close(r.vehicle, 80, 1e-9);
  close(r.bearing, 80, 1e-9);
  const r2 = rig.update(1 / 60, null, 12);
  close(r2.vehicle, 80, 1e-9);
});

function tel(lat, lon, speed, course, headingDeg = course, source = "gps") {
  return {
    type: "telemetry",
    gps: { fix: "3d", lat, lon, speed_mps: speed, course_deg: course },
    heading: { deg: headingDeg, source },
    display: { mode: "map", target_zoom: 17 },
  };
}

test("MotionModel: экстраполяция между пакетами 10 Гц — равномерно, без рывков", () => {
  const m = new MotionModel();
  const start = lonLatToMerc(37.5853, 55.7069);
  const course = 225, speed = 15; // 15 м/с на юго-запад
  let pos = { ...start };
  let t = 0;
  const frames = [];
  for (let k = 0; k < 30; k++) {
    const ll = mercToLonLat(pos.x, pos.y);
    m.ingest(tel(ll.lat, ll.lon, speed, course), t);
    for (let f = 0; f < 6; f++) {
      const s = m.update(t + f / 60);
      frames.push({ t: t + f / 60, x: s.mx, y: s.my });
    }
    t += 0.1;
    pos = moveMerc(pos.x, pos.y, course, speed * 0.1);
  }
  // после разгона фильтра шаг между кадрами ≈ 15/60 = 0.25 м, разброс мал
  const steps = [];
  for (let i = 60; i < frames.length; i++) {
    steps.push(mercDistanceM(frames[i - 1].x, frames[i - 1].y, frames[i].x, frames[i].y));
  }
  const mean = steps.reduce((a, b) => a + b) / steps.length;
  close(mean, 0.25, 0.01);
  for (const s of steps) close(s, 0.25, 0.03);
});

test("MotionModel: на стоянке шум GPS и компаса не дёргает картинку", () => {
  const m = new MotionModel();
  let seed = 1;
  const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647 - 0.5) * 2;
  const lat = 55.7069, lon = 37.5853;
  let t = 0;
  const hs = [], ps = [];
  for (let k = 0; k < 100; k++) {
    m.ingest(tel(lat + rnd() * 3e-6, lon + rnd() * 3e-6, Math.abs(rnd()) * 0.1, null, 100 + rnd() * 5, "mag"), t);
    for (let f = 0; f < 6; f++) {
      const s = m.update(t + f / 60);
      if (k > 20) {
        hs.push(s.heading);
        ps.push({ x: s.mx, y: s.my });
      }
    }
    t += 0.1;
  }
  const hmin = Math.min(...hs), hmax = Math.max(...hs);
  assert.ok(hmax - hmin < 3, `курс гуляет на ${hmax - hmin}°`);
  let maxStep = 0;
  for (let i = 1; i < ps.length; i++) maxStep = Math.max(maxStep, mercDistanceM(ps[i - 1].x, ps[i - 1].y, ps[i].x, ps[i].y));
  assert.ok(maxStep < 0.05, `скачок позиции ${maxStep} м за кадр`);
});

test("MotionModel: курс через 0° и потеря курса", () => {
  const m = new MotionModel();
  m.ingest(tel(55.7, 37.5, 10, 350), 0);
  m.update(0);
  m.ingest(tel(55.7, 37.5, 10, 10), 0.1);
  let s;
  for (let i = 1; i <= 60; i++) s = m.update(0.1 + i / 60);
  close(s.heading, 10, 0.5);
  m.ingest({ ...tel(55.7, 37.5, 0, null), heading: { deg: null, source: "none" } }, 1.2);
  s = m.update(1.25);
  assert.equal(s.headingKnown, false);
  assert.ok(Number.isFinite(s.heading)); // последний курс сохраняется для поворота карты
});

test("MotionModel: большой скачок — телепорт без анимации", () => {
  const m = new MotionModel();
  m.ingest(tel(55.7, 37.5, 0, null, 0), 0);
  m.update(0);
  m.ingest(tel(55.8, 37.5, 0, null, 0), 0.1);
  const s = m.update(0.1);
  close(s.lat, 55.8, 1e-6);
});

test("MotionModel: без пакетов дольше 1 с не уезжает", () => {
  const m = new MotionModel();
  m.ingest(tel(55.7, 37.5, 20, 90), 0);
  m.update(0);
  let s;
  for (let i = 1; i <= 300; i++) s = m.update(i / 60); // 5 с тишины
  const start = lonLatToMerc(37.5, 55.7);
  const d = mercDistanceM(start.x, start.y, s.mx, s.my);
  assert.ok(d < 25, `уехали на ${d} м`); // ≤ 20 м/с × 1 с + коррекция
});

test("MotionModel: фикс пропал — скорости нет, даже если в пакете осталась последняя", () => {
  const m = new MotionModel();
  m.ingest(tel(55.7, 37.5, 25, 45), 0);
  close(m.update(0).speed, 25, 1e-9);
  // старое ядро / gpsd замолчал: fix "none", а speed_mps — последние 90 км/ч
  m.ingest({ ...tel(55.7, 37.5, 25, 45), gps: { fix: "none", lat: 55.7, lon: 37.5, speed_mps: 25, course_deg: 45 } }, 0.1);
  assert.equal(m.update(0.1).speed, null);
  m.ingest({ ...tel(null, null, null, null), gps: { fix: "none", lat: null, lon: null, speed_mps: null, course_deg: null } }, 0.2);
  assert.equal(m.update(0.2).speed, null);
  m.ingest(tel(55.7, 37.5, 3, 45), 0.3); // фикс вернулся
  close(m.update(0.3).speed, 3, 1e-9);
});

test("hudSpeed: нет связи с fnav-core — скорость неизвестна (спидометр \"--\", знак не мигает)", () => {
  assert.equal(hudSpeed(25, true), 25);
  assert.equal(hudSpeed(0, true), 0);
  assert.equal(hudSpeed(25, false), null, "NO LINK: not the last speed before the link dropped");
  assert.equal(hudSpeed(null, true), null);
  assert.equal(hudSpeed(NaN, true), null);
});

// ── компас: минимальная задержка ──
function feedCompass(f, from, to, headingAt) {
  // пакеты 10 Гц, кадры 60 Гц; возвращает [{t, v}]
  const out = [];
  for (let i = Math.round(from * 60); i < Math.round(to * 60); i++) {
    const t = i / 60;
    if (i % 6 === 0) f.ingest(headingAt(t), t);
    out.push({ t, v: f.update(t) });
  }
  return out;
}

test("CompassFilter: скачок курса на 90° — ≥ 90 % за 0.25 с (smoothing 0.1 с), без перелёта", () => {
  const f = new CompassFilter({ smoothingS: 0.1 });
  feedCompass(f, 0, 1, () => 10);
  const tr = feedCompass(f, 1, 2, () => 100);
  const at = (t) => tr.find((r) => r.t >= t - 1e-9).v;
  assert.ok(angleDiff(10, at(1.25)) >= 81, `за 0.25 с: ${at(1.25)}`);
  for (const r of tr) assert.ok(angleDiff(10, r.v) <= 90 + 1e-6, `перелёт ${r.v}`);
  close(tr[tr.length - 1].v, 100, 0.31);
});

test("CompassFilter: через 0/360 — кратчайшим путём", () => {
  const f = new CompassFilter({ smoothingS: 0.1 });
  feedCompass(f, 0, 1, () => 350);
  const tr = feedCompass(f, 1, 2, () => 20);
  for (const r of tr) {
    const u = 350 + angleDiff(350, r.v);
    assert.ok(u >= 350 - 1e-6 && u <= 380 + 1e-6, `длинный путь: ${r.v}`);
  }
  close(angleDiff(tr[tr.length - 1].v, 20), 0, 0.31);
});

test("CompassFilter: плавный поворот — почти без отставания, плавно между пакетами", () => {
  const f = new CompassFilter({ smoothingS: 0.1 });
  const h = (t) => 30 * t; // 30 °/с
  const tr = feedCompass(f, 0, 3, h);
  const late = tr.filter((r) => r.t > 1);
  const maxLag = Math.max(...late.map((r) => Math.abs(angleDiff(r.v, h(r.t)))));
  assert.ok(maxLag < 4, `отставание ${maxLag}°`);
  let maxStep = 0;
  for (let i = 1; i < late.length; i++) maxStep = Math.max(maxStep, Math.abs(angleDiff(late[i - 1].v, late[i].v)));
  assert.ok(maxStep < 1.5, `рывок ${maxStep}° за кадр`); // 30°/с ÷ 60 = 0.5°
});

test("CompassFilter: smoothing 0 — сразу; дрожание < 0.3° гасится; неизвестный курс", () => {
  const f = new CompassFilter({ smoothingS: 0 });
  f.ingest(10, 0);
  f.update(0);
  f.ingest(200, 0.1);
  assert.equal(f.update(0.1), 200);
  const g = new CompassFilter({ smoothingS: 0.1 });
  const tr = feedCompass(g, 0, 3, (t) => 50 + 0.2 * Math.sin(t * 40));
  const vals = new Set(tr.filter((r) => r.t > 0.5).map((r) => r.v));
  assert.ok(vals.size <= 3, `дрожит: ${vals.size} разных значений`);
  g.ingest(null, 3.1);
  assert.equal(g.known, false);
});

// ── счисление пути в тоннеле ──
const noFix = (headingDeg = 90) => ({
  type: "telemetry",
  gps: { fix: "none", lat: null, lon: null, speed_mps: null, course_deg: null },
  heading: { deg: headingDeg, source: headingDeg === null ? "none" : "mag" },
  display: { mode: "map", target_zoom: 16 },
});

/** Пакеты 10 Гц, кадры 30 Гц; packet(t) — пакет или null (пакетов нет). Возвращает последний результат update. */
function drive(m, from, to, packet) {
  let s = null;
  for (let i = Math.round(from * 30); i < Math.round(to * 30); i++) {
    const t = i / 30;
    if (i % 3 === 0) {
      const p = packet(t);
      if (p) m.ingest(p, t);
    }
    s = m.update(t);
  }
  return s;
}

test("MotionModel: тоннель — машина едет по последней скорости (медленно затухает) и курсу из телеметрии", () => {
  const m = new MotionModel();
  m.configure({ drMinMps: 2.5, drWindowS: 90 });
  let pos = lonLatToMerc(21.0, 52.2);
  let s = drive(m, 0, 3, () => {
    const ll = mercToLonLat(pos.x, pos.y);
    pos = moveMerc(pos.x, pos.y, 90, 25 * 0.1);
    return tel(ll.lat, ll.lon, 25, 90);
  });
  assert.equal(s.dr, false);
  const a = { x: s.mx, y: s.my };
  // 10 с без фикса (ядро шлёт speed null): ∫ 25·e^(−t/120) dt ≈ 240 м на восток
  s = drive(m, 3, 13, () => noFix(90));
  assert.equal(s.dr, true);
  assert.equal(s.speed, null, "the speedometer shows \"--\"");
  close(s.drSpeed, 25 * Math.exp(-10 / 120), 0.2);
  const d = mercDistanceM(a.x, a.y, s.mx, s.my);
  close(d, 3000 * (1 - Math.exp(-10 / 120)), 6);
  close(s.my, a.y, 1e-7); // строго на восток
  // курс сменился (гироскоп ведёт его и в тоннеле) — машина поворачивает за ним
  const b = { x: s.mx, y: s.my };
  s = drive(m, 13, 18, () => noFix(180));
  assert.ok(s.my > b.y && mercDistanceM(b.x, s.my, b.x, b.y) > 80, "went south");
  // окно 90 с от последнего фикса прошло — стоп, как раньше
  s = drive(m, 18, 93.5, () => noFix(180));
  assert.equal(s.dr, false);
  const c = { x: s.mx, y: s.my };
  s = drive(m, 93.5, 98, () => noFix(180));
  assert.ok(mercDistanceM(c.x, c.y, s.mx, s.my) < 0.5, "frozen after the window");
  assert.equal(s.drSpeed, null);
});

test("MotionModel: счисление — только на ходу, только пока идут пакеты, выключается настройкой", () => {
  const start = (speed, opts = { drMinMps: 2.5, drWindowS: 90 }) => {
    const m = new MotionModel();
    m.configure(opts);
    drive(m, 0, 2, () => tel(52.2, 21.0, speed, 90));
    return m;
  };
  // медленно (ниже map_above_mps) — фикс пропал, машина стоит
  let m = start(2.0);
  let s = drive(m, 2, 10, () => noFix(90));
  assert.equal(s.dr, false);
  // выключено (nav.dead_reckoning_s = 0)
  m = start(20, { drMinMps: 2.5, drWindowS: 0 });
  assert.equal(drive(m, 2, 5, () => noFix(90)).dr, false);
  // курса нет — ехать некуда
  m = start(20);
  assert.equal(drive(m, 2, 5, () => noFix(null)).dr, false);
  // связь пропала посреди тоннеля — не дальше maxExtrapolateS, дальше замирает
  m = start(20);
  s = drive(m, 2, 5, () => noFix(90));
  assert.equal(s.dr, true);
  const p = { x: s.mx, y: s.my };
  s = drive(m, 5, 10, () => null);
  assert.equal(s.dr, false);
  assert.ok(mercDistanceM(p.x, p.y, s.mx, s.my) < 21, "≤ 1 s of motion");
  // фикс вернулся (встали в пробке у начала тоннеля) — машина возвращается к нему
  m = start(20);
  drive(m, 2, 12, () => noFix(90));
  s = drive(m, 12, 18, () => tel(52.2, 21.0, 0, 90));
  const fixAt = lonLatToMerc(21.0, 52.2);
  assert.equal(s.dr, false);
  assert.ok(mercDistanceM(fixAt.x, fixAt.y, s.mx, s.my) < 1.5);
});

// ── антенна GPS ≠ центр машины ──
const VEH = { length_m: 6.0, width_m: 2.05, gps_from_front_m: 0.95, gps_right_m: 0.25 };

test("vehicleCenter: центр 2.05 м позади антенны и 0.25 м левее (по курсу иконки)", () => {
  const ant = lonLatToMerc(21.0, 52.2);
  const north = vehicleCenter(ant.x, ant.y, 0, VEH);
  close(mercDistanceM(ant.x, ant.y, ant.x, north.y), 2.05, 1e-3);
  assert.ok(north.y > ant.y, "behind = south");
  close(mercDistanceM(ant.x, ant.y, north.x, ant.y), 0.25, 1e-3);
  assert.ok(north.x < ant.x, "the antenna is right of the center line → the center is to the west");
  const east = vehicleCenter(ant.x, ant.y, 90, VEH);
  assert.ok(east.x < ant.x && east.y < ant.y, "heading east: center to the west and north");
  close(mercDistanceM(ant.x, ant.y, east.x, east.y), Math.hypot(2.05, 0.25), 1e-3);
  // курс ещё неизвестен или нет настроек — центр = антенна
  assert.deepEqual(vehicleCenter(ant.x, ant.y, null, VEH), { x: ant.x, y: ant.y });
  assert.deepEqual(vehicleCenter(ant.x, ant.y, 45, undefined), { x: ant.x, y: ant.y });
  // антенна ровно в центре — без сдвига
  assert.deepEqual(vehicleCenter(ant.x, ant.y, 45, { length_m: 4, gps_from_front_m: 2, gps_right_m: 0 }), { x: ant.x, y: ant.y });
});

test("vehicleCenter: на экране антенна впереди и правее иконки — на метры в пикселях текущего масштаба", () => {
  const ant = lonLatToMerc(21.0, 52.2);
  for (const heading of [0, 37, 247.3]) {
    for (const [zoom, bearing] of [[19, heading], [17, heading], [19, 0]]) {
      const c = vehicleCenter(ant.x, ant.y, heading, VEH);
      const view = makeView({ cx: c.x, cy: c.y, zoom, bearing, ax: 384, ay: 386, w: 768, h: 576 });
      const s = mercToScreen(view, ant.x, ant.y);
      const mpp = metersPerPixel(52.2, zoom);
      // в осях иконки на экране (повёрнута на heading − bearing): вперёд = (sin r, −cos r), вправо = (cos r, sin r)
      const r = ((heading - bearing) * Math.PI) / 180;
      const dx = s.x - 384, dy = s.y - 386;
      const fwd = dx * Math.sin(r) - dy * Math.cos(r);
      const right = dx * Math.cos(r) + dy * Math.sin(r);
      close(fwd * mpp, 2.05, 0.01, `fwd h${heading} z${zoom}`);
      close(right * mpp, 0.25, 0.01, `right h${heading} z${zoom}`);
    }
  }
  // heading-up на z19 (52° с.ш.): ~11 px вверх, ~1.4 px вправо
  const c = vehicleCenter(ant.x, ant.y, 90, VEH);
  const s = mercToScreen(makeView({ cx: c.x, cy: c.y, zoom: 19, bearing: 90, ax: 384, ay: 386, w: 768, h: 576 }), ant.x, ant.y);
  assert.ok(386 - s.y > 10 && 386 - s.y < 12.5, String(s.y));
  assert.ok(s.x - 384 > 1 && s.x - 384 < 2, String(s.x));
});
