// Плашка станций (ui/power.js), плашка записи и строки справа внизу (ui/status.js).
import test from "node:test";
import assert from "node:assert/strict";

globalThis.OffscreenCanvas = undefined; // спрайты не нужны (null — blit пропускает)
const { drawPower, powerWidth, socText, voltParts, POWER_H } = await import("../../web/display/ui/power.js");
const { camState, drawIndicators, drawRecTag, rightColumnWidth, indicatorsWidth } = await import("../../web/display/ui/status.js");

function recCtx() {
  const calls = [];
  const st = { font: "10px sans-serif", globalAlpha: 1 };
  return new Proxy({}, {
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
}
const SAFE = { x: 41.4, y: 31.8, w: 685.2, h: 512.4 };
const texts = (ctx) => ctx.calls.filter((c) => c[0] === "fillText").map((c) => c[1]);
const fills = (ctx, text) => {
  // цвет заливки, действовавший при fillText(text)
  let fill = null;
  for (const c of ctx.calls) {
    if (c[0] === "=fillStyle") fill = c[1];
    if (c[0] === "fillText" && c[1] === text) return fill;
  }
  return undefined;
};

test("power: values and dashes", () => {
  assert.equal(socText({ soc_pct: 91.3 }), "91");
  assert.equal(socText({ soc_pct: 100 }), "100");
  assert.equal(socText(null), "--");
  assert.deepEqual(voltParts({ voltage_v: 20.84 }), ["20", ".8"]);
  assert.deepEqual(voltParts({ voltage_v: null }), ["--", ".-"]);

  const ctx = recCtx();
  const power = { main: { soc_pct: 91.3, charging: false, low: false }, second: { voltage_v: 20.8, low: false }, third: {} };
  const r = drawPower(ctx, { now: 0.1, telemetry: { power } }, SAFE);
  assert.ok(r && r.h === POWER_H && r.w === powerWidth(ctx));
  assert.equal(Math.round(r.x + r.w), Math.round(SAFE.x + SAFE.w));
  const t = texts(ctx);
  for (const s of ["91", "%", "20", ".8", "--", ".-"]) assert.ok(t.includes(s), `${s} in ${t}`);
  assert.equal(drawPower(recCtx(), { telemetry: { power: null } }, SAFE), null, "switched off in the settings");
});

test("power: low charge blinks", () => {
  const power = { main: { soc_pct: 15, low: true }, second: {}, third: {} };
  const seen = new Set([0.1, 0.7].map((now) => {
    const ctx = recCtx();
    drawPower(ctx, { now, telemetry: { power } }, SAFE);
    return fills(ctx, "15");
  }));
  assert.equal(seen.size, 2, "bright and dim phases");
});

test("indicators sit above the power plate; the radio leaves room for it", () => {
  const al = { state: "armed", arm_in_s: null };
  const st = (power) => ({ now: 0, telemetry: { alarm: al, power }, view: { internet: false } });
  const ctx = recCtx();
  const withPower = drawIndicators(ctx, st({ main: {}, second: {}, third: {} }), SAFE);
  const without = drawIndicators(recCtx(), st(null), SAFE);
  assert.ok(texts(ctx).includes("NO NET") && texts(ctx).includes("ARMED"));
  assert.equal(Math.round(withPower.y + withPower.h), Math.round(SAFE.y + SAFE.h - POWER_H - 6));
  assert.equal(Math.round(without.y + without.h), Math.round(SAFE.y + SAFE.h));
  assert.equal(rightColumnWidth(ctx, st({})), powerWidth(ctx));
  assert.equal(rightColumnWidth(ctx, st(null)), indicatorsWidth(ctx));
});

test("recording tag: camera numbers 1 and 2, missing one crossed out", () => {
  assert.equal(camState({ state: "recording", recording: true }), "rec");
  assert.equal(camState({ state: "live", recording: false }), "idle");
  assert.equal(camState({ state: "error", recording: false }), "absent");
  assert.equal(camState(null), "absent", "rear camera off in the settings");

  const strokes = (cams) => {
    const ctx = recCtx();
    drawRecTag(ctx, { now: 0.1, telemetry: { camera: cams[0], camera_rear: cams[1] } }, 300, 30);
    assert.deepEqual(texts(ctx), ["1", "2"]);
    return ctx.calls.filter((c) => c[0] === "stroke").length;
  };
  const both = strokes([{ recording: true }, { recording: true }]);
  const noRear = strokes([{ recording: true }, null]);
  assert.ok(noRear > both, "the missing camera's number is crossed out");
});
