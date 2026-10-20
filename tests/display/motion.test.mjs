import test from "node:test";
import assert from "node:assert/strict";

import { norm360, wrap180, angleDiff, lerpAngle, approachAngle, springStep } from "../../web/display/map/angles.js";
import { MotionModel, ZoomAnimator, OrientationBlend, orientationPose } from "../../web/display/map/motion.js";
import { lonLatToMerc, mercDistanceM, moveMerc, mercToLonLat } from "../../web/display/map/mercator.js";

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

test("OrientationBlend: плавное переключение за 0.8 с", () => {
  const o = new OrientationBlend(0.8);
  o.set("heading_up");
  assert.equal(o.update(0), 1);
  o.set("north_up");
  const mid = (() => {
    let f;
    for (let i = 0; i < 24; i++) f = o.update(1 / 60);
    return f;
  })();
  assert.ok(mid > 0.2 && mid < 0.8, `середина ${mid}`);
  for (let i = 0; i < 30; i++) o.update(1 / 60);
  assert.equal(o.update(1 / 60), 0);
});

test("orientationPose: heading_up / north_up по контракту", () => {
  const hu = orientationPose(1, 247.3, 768, 576, 0.67);
  close(hu.bearing, 247.3, 1e-9);
  close(hu.vehicleRot, 0, 1e-9);
  close(hu.ax, 384, 1e-9);
  close(hu.ay, 576 * 0.67, 1e-9);
  const nu = orientationPose(0, 247.3, 768, 576, 0.67);
  assert.equal(nu.bearing, 0);
  close(nu.vehicleRot, 247.3, 1e-9);
  close(nu.ay, 288, 1e-9);
  // на полпути карта поворачивается по кратчайшей дуге (247 → 0 через 303.6, а не через 123)
  const half = orientationPose(0.5, 247.3, 768, 576, 0.67);
  close(half.bearing, 303.65, 1e-9);
  close(norm360(half.bearing + half.vehicleRot), 247.3, 1e-9);
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
