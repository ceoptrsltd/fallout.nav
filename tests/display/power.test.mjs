// Плашка станций (ui/power.js), плашка записи и строки справа внизу (ui/status.js).
import test from "node:test";
import assert from "node:assert/strict";

globalThis.OffscreenCanvas = undefined; // спрайты не нужны (null — blit пропускает)
const { drawPower, powerWidth, socText, voltText, deltaText, wattText, chargeLit, CHARGE_SEGS, POWER_H } = await import("../../web/display/ui/power.js");
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
  assert.equal(socText({ soc_pct: 83.5 }), "83", "the whole part, as the station shows it");
  assert.equal(socText({ soc_pct: 100 }), "100");
  assert.equal(socText(null), "--");
  assert.equal(voltText({ voltage_v: 20.84 }), "20.8V");
  assert.equal(voltText({ voltage_v: null }), "--.-V");

  const ctx = recCtx();
  const power = { main: { soc_pct: 91.3, charging: false, low: false }, second: { soc_pct: 9.4, voltage_v: 20.8, low: false }, third: {} };
  const r = drawPower(ctx, { now: 0.1, telemetry: { power } }, SAFE);
  assert.ok(r && r.h === POWER_H && r.w === powerWidth(ctx));
  assert.equal(Math.round(r.x + r.w), Math.round(SAFE.x + SAFE.w));
  const t = texts(ctx);
  for (const s of ["91", "%", "9%", "20.8V", "--%", "--.-V"]) assert.ok(t.includes(s), `${s} in ${t}`);
  assert.equal(drawPower(recCtx(), { telemetry: { power: null } }, SAFE), null, "no data");
  const off = { telemetry: { power }, settings: { power: { show_on_screen: false } } };
  assert.equal(drawPower(recCtx(), off, SAFE), null, "switched off in the settings");
});

test("power: the main station charging — a smaller percentage, the charging power under it", () => {
  assert.equal(wattText({ input_w: 412.4 }), "412W");
  assert.equal(wattText({}), "--W");
  const draw = (main) => {
    const ctx = recCtx();
    drawPower(ctx, { now: 0.1, telemetry: { power: { main, second: {}, third: {} } } }, SAFE);
    return ctx;
  };
  const idle = draw({ soc_pct: 83, input_w: 0, charging: false });
  assert.ok(!texts(idle).some((t) => t.endsWith("W")), "not charging — no watts");
  const chg = draw({ soc_pct: 83, input_w: 412, charging: true });
  assert.ok(texts(chg).includes("412W") && texts(chg).includes("83"));
  const fontOf = (ctx, text) => {
    let font = null;
    for (const c of ctx.calls) {
      if (c[0] === "=font") font = c[1];
      if (c[0] === "fillText" && c[1] === text) return font;
    }
    return null;
  };
  const px = (f) => Number(/(\d+)px/.exec(f)[1]);
  assert.ok(px(fontOf(chg, "83")) < px(fontOf(idle, "83")), "smaller while charging");
});

test("power: the lead battery charging from the sun — a bolt and the watts, like the main station", () => {
  const draw = (third) => {
    const ctx = recCtx();
    drawPower(ctx, { now: 0.1, telemetry: { power: { main: {}, second: {}, third } } }, SAFE);
    return texts(ctx);
  };
  assert.ok(draw({ soc_pct: 78, voltage_v: 13.4, charging: true, input_w: 47 }).includes("47W"));
  assert.ok(!draw({ soc_pct: 78, voltage_v: 12.4, charging: false, input_w: 0 }).some((t) => t.endsWith("W")));
});

test("power: a charging station runs its charge segments", () => {
  const lit = [0, 0.5, 1, 1.5, 2, 2.3].map(chargeLit);
  assert.deepEqual(lit, [0, 1, 2, 3, 4, 0], "fill one by one, then again");
  assert.equal(CHARGE_SEGS, 4);
  const rects = (charging) => {
    const ctx = recCtx();
    const power = { main: {}, second: {}, third: { soc_pct: 90, voltage_v: 13.6, charging } };
    drawPower(ctx, { now: 1, telemetry: { power } }, SAFE);
    return ctx.calls.filter((c) => c[0] === "fillRect").length;
  };
  assert.ok(rects(true) >= rects(false) + 2 * CHARGE_SEGS);
});

test("power: the powerbank's cell spread, small; a dangerous one blinks", () => {
  assert.equal(deltaText({ cell_delta_mv: 60 }), "Δ60mV");
  assert.equal(deltaText({}), "");
  const shown = (bad, now) => {
    const ctx = recCtx();
    const power = { main: {}, second: { soc_pct: 50, voltage_v: 20.9, cell_delta_mv: bad ? 140 : 60, cell_delta_bad: bad }, third: {} };
    drawPower(ctx, { now, telemetry: { power } }, SAFE);
    return texts(ctx).some((t) => t.startsWith("Δ"));
  };
  assert.ok(shown(false, 0.1) && shown(false, 0.4), "steady");
  assert.notEqual(shown(true, 0.1), shown(true, 0.4), "blinks");
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
