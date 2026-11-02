// След: шаг по экрану (~2.5 px) и так, чтобы точек хватило на display.trail_seconds (web/display/map/trail.js).
import test from "node:test";
import assert from "node:assert/strict";

import { Trail } from "../../web/display/map/trail.js";
import { lonLatToMerc, makeView, mercToScreen, metersPerPixel, moveMerc } from "../../web/display/map/mercator.js";
import { zoomForSpeed } from "../../web/display/demo/navlogic.js";
import { DEFAULT_SETTINGS } from "../../web/display/map/defaults.js";

const LAT = 52.2;

/** Едем на восток со скоростью speed (м/с) seconds секунд, кадры 30 Гц; зум — по таблице nav.zoom_table. */
function drive(tr, speed, seconds, keepS, zoom = zoomForSpeed(DEFAULT_SETTINGS.nav, speed)) {
  let p = lonLatToMerc(21.0, LAT);
  const mpp = metersPerPixel(LAT, zoom);
  for (let i = 0; i <= seconds * 30; i++) {
    const t = i / 30;
    tr.add(p.x, p.y, t, { mpp, speed, keepS });
    tr.prune(t, keepS);
    p = moveMerc(p.x, p.y, 90, speed / 30);
  }
  return { p, zoom, t: seconds };
}

test("Trail: trail_seconds = 600 s is covered at any speed (the 4000-point cap used to cut it to 320 s at 90 km/h)", () => {
  for (const kmh of [10, 30, 50, 60, 90, 120, 140]) {
    const tr = new Trail();
    const { t } = drive(tr, kmh / 3.6, 650, 600);
    const span = t - tr.pts[0].t;
    assert.ok(span > 595, `${kmh} km/h: trail covers only ${span.toFixed(0)} s`);
    assert.ok(tr.pts.length <= tr.maxPoints, `${kmh} km/h: ${tr.pts.length} points`);
  }
});

test("Trail: steps of ~2.5 screen px at the current zoom, fewer points to project each frame", () => {
  for (const kmh of [20, 50, 90]) {
    const tr = new Trail();
    const { p, zoom } = drive(tr, kmh / 3.6, 60, 60);
    const view = makeView({ cx: p.x, cy: p.y, zoom, bearing: 90, ax: 384, ay: 386, w: 768, h: 576 });
    const pts = tr.project(view, 60, null);
    const gaps = [];
    for (let i = 1; i < pts.length; i++) gaps.push(Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y));
    const mean = gaps.reduce((a, b) => a + b) / gaps.length;
    assert.ok(mean >= 2.4 && mean <= 3.5, `${kmh} km/h z${zoom}: ${mean.toFixed(2)} px between points`);
  }
  // 60 s at 90 km/h (zoom 15) with the old fixed 2 m step: 750 points; now ~200
  const tr = new Trail();
  drive(tr, 25, 60, 60);
  assert.ok(tr.pts.length < 250, `${tr.pts.length} points`);
});

test("Trail: stepM — the screen step, the cap step for long trails, the noise floor when parked", () => {
  const tr = new Trail();
  const mpp19 = metersPerPixel(LAT, 19), mpp15 = metersPerPixel(LAT, 15);
  assert.equal(tr.stepM({ mpp: mpp19, speed: 0, keepS: 60 }), 0.5, "≥ 0.5 m: GPS noise on a parked van draws no scribble");
  assert.ok(Math.abs(tr.stepM({ mpp: mpp15, speed: 25, keepS: 60 }) - 2.5 * mpp15) < 1e-9);
  // 600 s at 25 m/s on zoom 16: 2.5 px ≈ 3.7 m would need 4100 points — the step grows to fit the cap
  const s = tr.stepM({ mpp: metersPerPixel(LAT, 16), speed: 25, keepS: 600 });
  assert.ok((25 * 600) / s <= tr.maxPoints * 0.9 + 1e-6, String(s));
  assert.equal(tr.stepM(), 0.5, "no options: the floor");
});
