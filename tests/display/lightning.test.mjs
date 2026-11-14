// Молния вокруг плашки (ui/lightning.js): детерминирована от now, между вспышками ничего не рисует.
import test from "node:test";
import assert from "node:assert/strict";

const { LIGHTNING_VARIANTS, drawPlateLightning, lightningPhase } = await import("../../web/display/ui/lightning.js");

function recCtx() {
  const calls = [];
  const st = {};
  return new Proxy({}, {
    get(_, k) {
      if (k === "calls") return calls;
      if (k in st) return st[k];
      return (...a) => calls.push([k, ...a]);
    },
    set(_, k, v) {
      st[k] = v;
      calls.push(["=" + k, v]);
      return true;
    },
  });
}

const RECT = { x: 507, y: 464, w: 222, h: 84 };
const SAFE = { x: 38.4, y: 28.8, w: 691.2, h: 518.4 };

test("lightning: the same time gives the same drawing; between bursts nothing is drawn", () => {
  assert.equal(LIGHTNING_VARIANTS.length, 4);
  for (const { id } of LIGHTNING_VARIANTS) {
    const ph = lightningPhase(0.3, id);
    assert.ok(ph, `${id}: burst at the start of a cycle`);
    const a = recCtx(), b = recCtx();
    const box = drawPlateLightning(a, RECT, 0.3, id, { bounds: SAFE });
    drawPlateLightning(b, RECT, 0.3, id, { bounds: SAFE });
    assert.ok(box && a.calls.length > 20, `${id}: draws`);
    assert.deepEqual(a.calls, b.calls, `${id}: deterministic`);
    assert.ok(box.x + box.w <= SAFE.x + SAFE.w + 5 && box.y + box.h <= SAFE.y + SAFE.h + 5, `${id}: inside safe (+5 px)`);
    const strokes = a.calls.filter((c) => c[0] === "=lineWidth").map((c) => c[1]);
    assert.ok(strokes.every((w) => w >= 2), `${id}: lines ≥ 2 px`);

    const idle = recCtx();
    const tIdle = 5.5; // после вспышки (≤ 1.5 с), до следующего цикла (≥ 6 с)
    assert.equal(lightningPhase(tIdle, id), null);
    assert.equal(drawPlateLightning(idle, RECT, tIdle, id), null);
    assert.equal(idle.calls.length, 0, `${id}: idle — no calls`);
  }
});

test("lightning: different cycles differ", () => {
  const a = recCtx(), b = recCtx();
  drawPlateLightning(a, RECT, 0.4, "c");
  drawPlateLightning(b, RECT, 6.4, "c"); // тот же момент следующей вспышки
  assert.notDeepEqual(a.calls, b.calls);
});
