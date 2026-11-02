// HUD в тоннеле (счисление пути): DR в строке GPS вместо мигающего NO GPS, иконка тусклая, но не мигает.
import test from "node:test";
import assert from "node:assert/strict";

globalThis.OffscreenCanvas = undefined; // спрайты не нужны (null — blit пропускает)
const { drawIndicators, drawWarnings } = await import("../../web/display/ui/status.js");
const { vehicleAlpha } = await import("../../web/display/ui/vehicle.js");

function recCtx() {
  const calls = [];
  const st = { font: "10px sans-serif", globalAlpha: 1 };
  const ctx = new Proxy({}, {
    get(_, k) {
      if (k === "measureText") return (t) => ({ width: String(t).length * 14, actualBoundingBoxAscent: 20 });
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
  return ctx;
}

const SAFE = { x: 41.4, y: 31.8, w: 685.2, h: 512.4 };
const texts = (ctx) => ctx.calls.filter((c) => c[0] === "fillText").map((c) => c[1]);
const state = (view, gps = { fix: "none", sats_visible: 9, sats_used: 0 }) => ({
  now: 12.3, telemetry: { gps }, view: { connected: true, ...view }, settings: {},
});

test("status: no fix — NO GPS warning and satellites; in a tunnel — DR in the GPS row, no NO GPS", () => {
  let ctx = recCtx();
  drawWarnings(ctx, state({ hasFix: false }), SAFE, 40);
  drawIndicators(ctx, state({ hasFix: false }), SAFE);
  assert.ok(texts(ctx).includes("NO GPS"));
  assert.ok(texts(ctx).includes("9/0"));

  ctx = recCtx();
  drawWarnings(ctx, state({ hasFix: false, deadReckoning: true }), SAFE, 40);
  drawIndicators(ctx, state({ hasFix: false, deadReckoning: true }), SAFE);
  assert.ok(!texts(ctx).includes("NO GPS"), "the tunnel does not flash a warning");
  assert.ok(texts(ctx).includes("DR"));
  assert.ok(!texts(ctx).includes("9/0"));

  ctx = recCtx(); // NO LINK — всегда
  drawWarnings(ctx, state({ hasFix: false, deadReckoning: true, connected: false }), SAFE, 40);
  assert.deepEqual(texts(ctx).filter((t) => t.startsWith("NO")), ["NO LINK"]);

  ctx = recCtx(); // фикс есть — флаг ни на что не влияет
  drawIndicators(ctx, state({ hasFix: true, deadReckoning: true }, { fix: "3d", sats_visible: 14, sats_used: 11 }), SAFE);
  assert.ok(texts(ctx).includes("14/11"));
});

test("vehicle icon: blinks without a fix, steady dimmed in a tunnel, full with a fix", () => {
  assert.equal(vehicleAlpha({ now: 3, view: { hasFix: true } }), 1);
  assert.equal(vehicleAlpha({ now: 3, view: {} }), 1, "gallery states without the flag");
  const blink = [0, 0.25, 0.5, 0.75].map((now) => vehicleAlpha({ now, view: { hasFix: false } }));
  assert.ok(Math.max(...blink) - Math.min(...blink) > 0.3, `blinks: ${blink}`);
  assert.ok(blink.every((a) => a >= 0.22 && a <= 0.72));
  const dr = [0, 0.25, 0.5, 0.75].map((now) => vehicleAlpha({ now, view: { hasFix: false, deadReckoning: true } }));
  assert.ok(dr.every((a) => a === dr[0]) && dr[0] > 0.3 && dr[0] < 1, `steady dimmed: ${dr}`);
});
