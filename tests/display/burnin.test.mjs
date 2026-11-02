// Защита кинескопа от выжигания: расписание сдвига HUD и машины (web/display/map/burnin.js).
import test from "node:test";
import assert from "node:assert/strict";

import { BURNIN_AMP, BURNIN_STEP_MS, burnInOffset, insetRect } from "../../web/display/map/burnin.js";

const MIN = 60_000;

test("burn-in: one step every 5–10 minutes, constant in between", () => {
  assert.ok(BURNIN_STEP_MS >= 5 * MIN && BURNIN_STEP_MS <= 10 * MIN);
  const t0 = Date.UTC(2026, 10, 2, 9, 0, 0) - (Date.UTC(2026, 10, 2, 9, 0, 0) % BURNIN_STEP_MS);
  const a = burnInOffset(t0);
  assert.deepEqual(burnInOffset(t0 + BURNIN_STEP_MS - 1), a);
  assert.notDeepEqual(burnInOffset(t0 + BURNIN_STEP_MS), a);
});

test("burn-in: each step moves 2–3 px, never more than BURNIN_AMP from home; the cycle wanders over the field", () => {
  const seen = new Set();
  let sx = 0, sy = 0;
  const n = 200;
  for (let i = 0; i < n; i++) {
    const a = burnInOffset(i * BURNIN_STEP_MS);
    const b = burnInOffset((i + 1) * BURNIN_STEP_MS);
    const step = Math.max(Math.abs(b.dx - a.dx), Math.abs(b.dy - a.dy));
    assert.ok(step >= 2 && step <= 3, `step ${i}: ${JSON.stringify(a)} → ${JSON.stringify(b)}`);
    assert.ok(Math.abs(a.dx) <= BURNIN_AMP && Math.abs(a.dy) <= BURNIN_AMP);
    assert.ok(Number.isInteger(a.dx) && Number.isInteger(a.dy), "whole pixels: text stays crisp");
    seen.add(`${a.dx},${a.dy}`);
    sx += a.dx;
    sy += a.dy;
  }
  assert.equal(BURNIN_AMP, 3);
  assert.ok(seen.size >= 8, `only ${seen.size} positions`);
  assert.ok(Math.abs(sx / n) < 0.5 && Math.abs(sy / n) < 0.5, "centered on the home position");
  // время до 1970 или мусор — без исключений
  assert.ok(Math.abs(burnInOffset(-123456789).dx) <= BURNIN_AMP);
  assert.deepEqual(burnInOffset(NaN), burnInOffset(0));
});

test("burn-in: the HUD safe area shrinks by the amplitude, so shifted plates stay inside the real one", () => {
  const safe = { x: 38.4, y: 28.8, w: 691.2, h: 518.4 };
  const inner = insetRect(safe, BURNIN_AMP);
  for (let i = 0; i < 11; i++) {
    const { dx, dy } = burnInOffset(i * BURNIN_STEP_MS);
    assert.ok(inner.x + dx >= safe.x && inner.y + dy >= safe.y);
    assert.ok(inner.x + inner.w + dx <= safe.x + safe.w + 1e-9 && inner.y + inner.h + dy <= safe.y + safe.h + 1e-9);
  }
});
