// Alarm page and push notifications: pure helpers (web/admin/js/alarm-fmt.js), docs/contracts/alarm.md §4, §7.
import test from "node:test";
import assert from "node:assert/strict";

import {
  IMPACT_MIN, IMPACT_MAX, SLIDER_STEPS, METER_MIN, METER_MAX, logPos, logVal, roundG, impactToSlider, sliderToImpact,
  logTicks, fmtG, peakHold, alarmStateText, armLeft, relayText, kindInfo, eventsKey, eventLink, isIos, deviceName,
  pushMode, b64uToBytes, bytesToB64u, pushErrorText, toUnix, hazardStatus, silenceLabel, visionStatus, thresholdAt, stepThreshold, IMPACT_STEPS,
  markLabel, marksSummary,
} from "../../web/admin/js/alarm-fmt.js";

const UA = {
  iphone: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
  iphoneApp: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
  iphoneChrome: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/126.0.6478.54 Mobile/15E148 Safari/604.1",
  ipadOs: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
  android: "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
  samsung: "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/25.0 Chrome/121.0.0.0 Mobile Safari/537.36",
  edge: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
  firefoxLinux: "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
  macChrome: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
};

test("impact slider: log scale over 0.02…2 g, ends and middle", () => {
  assert.equal(impactToSlider(IMPACT_MIN), 0);
  assert.equal(impactToSlider(IMPACT_MAX), SLIDER_STEPS);
  assert.equal(impactToSlider(0.2), SLIDER_STEPS / 2); // geometric middle of 0.02…2
  assert.equal(sliderToImpact(0), 0.02);
  assert.equal(sliderToImpact(SLIDER_STEPS), 2);
  assert.equal(sliderToImpact(SLIDER_STEPS / 2), 0.2);
  // out of range is clamped
  assert.equal(impactToSlider(0.001), 0);
  assert.equal(impactToSlider(9), SLIDER_STEPS);
  assert.equal(sliderToImpact(-50), 0.02);
  assert.equal(sliderToImpact(5000), 2);
});

test("impact slider: two significant digits, round trip stays put", () => {
  assert.equal(roundG(0.0234), 0.023);
  assert.equal(roundG(0.153), 0.15);
  assert.equal(roundG(1.27), 1.3);
  for (const g of [0.02, 0.05, 0.1, 0.15, 0.3, 0.42, 1, 1.5, 2]) {
    assert.equal(sliderToImpact(impactToSlider(g)), g, String(g));
  }
  // every slider step gives a valid value with ≤ 2 significant digits, monotonic
  let prev = 0;
  for (let p = 0; p <= SLIDER_STEPS; p += 7) {
    const g = sliderToImpact(p);
    assert.ok(g >= IMPACT_MIN && g <= IMPACT_MAX);
    assert.ok(g >= prev);
    assert.equal(Number(g.toPrecision(2)), g);
    prev = g;
  }
});

test("logPos / logVal / ticks of the vibration meter", () => {
  assert.equal(logPos(null, METER_MIN, METER_MAX), 0);
  assert.equal(logPos(METER_MIN / 2, METER_MIN, METER_MAX), 0);
  assert.equal(logPos(5, METER_MIN, METER_MAX), 1);
  assert.ok(Math.abs(logPos(logVal(0.37, 0.01, 1), 0.01, 1) - 0.37) < 1e-12);
  assert.ok(Math.abs(logPos(0.1, 0.01, 1) - 0.5) < 1e-12);
  assert.deepEqual(logTicks([0.001, 0.01, 0.1, 1, 10], 0.01, 1).map((t) => [t.label, t.p]), [["0.01", 0], ["0.1", 0.5], ["1", 1]]);
});

test("fmtG", () => {
  assert.equal(fmtG(0.0043), "0.004 g");
  assert.equal(fmtG(0.42), "0.42 g");
  assert.equal(fmtG(1.5), "1.50 g");
  assert.equal(fmtG(null), "—");
  assert.equal(fmtG(NaN), "—");
});

test("peakHold: higher replaces at once, lower waits for the hold time, no value keeps it", () => {
  let p = peakHold(null, 0.01, 0);
  assert.deepEqual(p, { g: 0.01, at: 0 });
  p = peakHold(p, 0.4, 1);
  assert.deepEqual(p, { g: 0.4, at: 1 });
  assert.equal(peakHold(p, 0.02, 20, 30), p);
  assert.equal(peakHold(p, null, 50, 30), p);
  assert.deepEqual(peakHold(p, 0.02, 31.5, 30), { g: 0.02, at: 31.5 });
  assert.deepEqual(peakHold(p, 0.4, 5, 30), { g: 0.4, at: 5 }); // the same again refreshes the hold
});

test("state wording, countdown, server link", () => {
  assert.deepEqual(alarmStateText("armed"), { text: "Armed", kind: "ok" });
  assert.equal(alarmStateText("triggered").kind, "bad");
  assert.equal(alarmStateText("arming").kind, "warn");
  assert.equal(alarmStateText("off").kind, "");
  assert.equal(alarmStateText("weird").text, "weird");
  assert.equal(armLeft(45, 1000, 1000), 45);
  assert.equal(armLeft(45, 1000, 3500), 43); // 2.5 s later: ceil(42.5)
  assert.equal(armLeft(2, 0, 9000), 0);
  assert.equal(armLeft(null, 0, 0), null);
  assert.equal(relayText({ configured: false, pending: 0 }).kind, "warn");
  assert.equal(relayText(null).kind, "warn");
  assert.deepEqual(relayText({ configured: true, pending: 0 }), { text: "up to date", kind: "ok" });
  assert.match(relayText({ configured: true, pending: 3 }).text, /^3 events waiting/);
  assert.match(relayText({ configured: true, pending: 1 }).text, /^1 event waiting/);
});

test("event log helpers", () => {
  assert.equal(kindInfo("impact").kind, "bad");
  assert.equal(kindInfo("move").icon, "pin");
  assert.equal(kindInfo("other").label, "other");
  assert.equal(eventsKey([]), "0:::");
  assert.notEqual(eventsKey([{ id: "a" }, { id: "b" }]), eventsKey([{ id: "c" }, { id: "a" }]));
  assert.equal(eventsKey(null), "0:::");
  // the snapshot / kept recording arrive a moment after the event — the log must redraw for them
  assert.notEqual(eventsKey([{ id: "a", data: {} }]), eventsKey([{ id: "a", data: { snapshot: true, segment: "x.ts" } }]));
  // … and the rear camera's, which may come later still
  const both = eventsKey([{ id: "a", data: { snapshot: true, segment: "x.ts" } }]);
  assert.notEqual(both, eventsKey([{ id: "a", data: { snapshot: true, segment: "x.ts", snapshot_rear: true } }]));
  assert.notEqual(both, eventsKey([{ id: "a", data: { snapshot: true, segment: "x.ts", segment_rear: "rear/x.ts" } }]));
  assert.notEqual(eventsKey([{ id: "a", data: { snapshot: true } }]), eventsKey([{ id: "a", data: { snapshot_rear: true } }]));
  assert.deepEqual(eventLink({ url: "#/tracking" }), { href: "#/tracking", text: "Show on the map" });
  assert.equal(eventLink({ url: "#/alarm" }), null);
  assert.equal(eventLink({ url: "https://evil.example/" }), null);
  assert.equal(eventLink({ url: "javascript:alert(1)" }), null);
  assert.equal(eventLink({}), null);
  assert.equal(eventLink({ url: "#/recordings" }).text, "Open recordings");
});

test("isIos and deviceName", () => {
  assert.equal(isIos({ ua: UA.iphone }), true);
  assert.equal(isIos({ ua: UA.ipadOs, platform: "MacIntel", touch: 5 }), true);
  assert.equal(isIos({ ua: UA.ipadOs, platform: "MacIntel", touch: 0 }), false); // a real Mac
  assert.equal(isIos({ ua: UA.android }), false);
  assert.equal(deviceName({ ua: UA.iphone }), "iPhone · Safari");
  assert.equal(deviceName({ ua: UA.iphoneApp }), "iPhone · Safari"); // Home Screen app: no "Safari/" token
  assert.equal(deviceName({ ua: UA.iphoneChrome }), "iPhone · Chrome");
  assert.equal(deviceName({ ua: UA.ipadOs, platform: "MacIntel", touch: 5 }), "iPad · Safari");
  assert.equal(deviceName({ ua: UA.ipadOs, platform: "MacIntel", touch: 0 }), "Mac · Safari");
  assert.equal(deviceName({ ua: UA.android }), "Android · Chrome");
  assert.equal(deviceName({ ua: UA.samsung }), "Android · Samsung Internet");
  assert.equal(deviceName({ ua: UA.edge }), "Windows · Edge");
  assert.equal(deviceName({ ua: UA.firefoxLinux }), "Linux · Firefox");
  assert.equal(deviceName({ ua: UA.macChrome }), "Mac · Chrome");
  assert.equal(deviceName({}), "Device · Browser");
});

test("pushMode: plain http, iPhone outside the Home Screen app, unsupported, ok", () => {
  const full = { secure: true, sw: true, push: true, notification: true, ios: false, standalone: false };
  assert.equal(pushMode({ ...full, secure: false }), "insecure");
  assert.equal(pushMode({ ...full, secure: false, ios: true }), "insecure");
  assert.equal(pushMode({ ...full, ios: true, standalone: false, push: false }), "ios-install");
  assert.equal(pushMode({ ...full, ios: true, standalone: true }), "ok");
  assert.equal(pushMode({ ...full, ios: true, standalone: true, push: false }), "unsupported"); // iOS < 16.4
  assert.equal(pushMode({ ...full, push: false }), "unsupported");
  assert.equal(pushMode({ ...full, sw: false }), "unsupported");
  assert.equal(pushMode(full), "ok");
  assert.equal(pushMode({ ...full, standalone: true }), "ok");
});

test("base64url VAPID key ↔ bytes", () => {
  const key = "BKv6h1WxfR0q3cBTKnOKOLQC7Pz5_JHornNVyAQFRL-1ehYb1YkzD--Ryn4QOrP6oIU_Q7SZioFOjZZgqvzwkp8";
  const b = b64uToBytes(key);
  assert.equal(b.length, 65);
  assert.equal(b[0], 4); // uncompressed P-256 point
  assert.equal(bytesToB64u(b), key);
  assert.equal(bytesToB64u(b.buffer), key);
  assert.equal(bytesToB64u(null), "");
  assert.deepEqual([...b64uToBytes("-_8")], [251, 255]);
});

test("pushErrorText: server answers and browser errors read well", () => {
  const api = (status, message) => Object.assign(new Error(message), { status });
  assert.match(pushErrorText(api(503, "Push needs python3-cryptography on the server")), /not available on the server: Push needs/);
  assert.match(pushErrorText(api(503, "Error 503")), /not available on the server\.$/);
  assert.match(pushErrorText(api(404, "Not found")), /does not handle notifications yet/);
  assert.match(pushErrorText(api(404, "Subscription not found")), /no longer subscribed/);
  assert.equal(pushErrorText(api(0, "No connection to the device")), "No connection to the server");
  assert.match(pushErrorText(Object.assign(new Error("denied"), { name: "NotAllowedError" })), /blocked for this site/);
  assert.match(pushErrorText(Object.assign(new Error("push service error"), { name: "AbortError" })), /push service: push service error/);
  assert.equal(pushErrorText(new Error("boom")), "boom");
  assert.equal(pushErrorText(null), "Unknown error");
});

test("hazard lights: status line", () => {
  assert.equal(hazardStatus(null), null);
  assert.equal(hazardStatus(undefined), null);
  assert.deepEqual(hazardStatus({ enabled: true, active: true, error: null }), { text: "Flashing now", kind: "warn" });
  assert.deepEqual(hazardStatus({ enabled: false, active: true, error: null }), { text: "Flashing now", kind: "warn" }); // the test
  assert.equal(hazardStatus({ enabled: true, active: false, error: null }).kind, "ok");
  assert.equal(hazardStatus({ enabled: false, active: false, error: null }).kind, "");
  assert.deepEqual(hazardStatus({ enabled: true, active: false, error: "GPIO19 is busy" }), { text: "GPIO19 is busy", kind: "bad" });
});

test("silence button wording: siren, lights or both", () => {
  assert.equal(silenceLabel({ siren: true, hazard: true, hazardEnabled: true }), "Stop siren & lights");
  assert.equal(silenceLabel({ siren: true, hazard: false, hazardEnabled: true }), "Stop siren & lights");
  assert.equal(silenceLabel({ siren: false, hazard: true, hazardEnabled: true }), "Stop the lights");
  assert.equal(silenceLabel({ siren: true, hazard: false, hazardEnabled: false }), "Stop the siren");
  for (const t of ["Stop siren & lights", "Stop the lights", "Stop the siren"]) assert.ok(t.length <= 20, "fits a phone button");
});

test("toUnix: seconds, milliseconds, ISO, nothing", () => {
  assert.equal(toUnix(1761140000.5), 1761140000.5);
  assert.equal(toUnix(1761140000500), 1761140000.5);
  assert.equal(toUnix("2025-10-22T13:33:20Z"), 1761140000);
  assert.equal(toUnix(null), null);
  assert.equal(toUnix("nope"), null);
});

test("camera watch: status line and event kinds", () => {
  assert.equal(visionStatus(null), null);
  assert.equal(visionStatus({ enabled: false }).kind, "");
  assert.equal(visionStatus({ enabled: true, available: false, error: "Vision service (fnav-watch) is not responding" }).kind, "bad");
  assert.equal(visionStatus({ enabled: true, available: false }).text, "The vision service is not running");
  assert.equal(visionStatus({ enabled: true, available: true, watching: false }).kind, "ok");
  assert.deepEqual(visionStatus({ enabled: true, available: true, watching: true, people: 0 }), { text: "Watching", kind: "ok" });
  assert.equal(visionStatus({ enabled: true, available: true, watching: true, people: 1 }).text, "Watching — a person in view");
  assert.equal(visionStatus({ enabled: true, available: true, watching: true, people: 3 }).kind, "warn");
  assert.equal(kindInfo("approach").label, "Person approaching");
  assert.equal(kindInfo("face").kind, "bad");
});

test("vibration meter sets the impact threshold: position and steps", () => {
  assert.equal(thresholdAt(0), IMPACT_MIN); // below the lowest threshold — the lowest
  assert.equal(thresholdAt(1), IMPACT_MAX);
  assert.equal(thresholdAt(logPos(0.05, METER_MIN, METER_MAX)), 0.05);
  assert.equal(thresholdAt(logPos(0.123, METER_MIN, METER_MAX)), 0.12); // two significant digits
  assert.equal(stepThreshold(0.025, 1), 0.03);
  assert.equal(stepThreshold(0.025, -1), 0.02);
  assert.equal(stepThreshold(0.045, 1), 0.05); // off the ladder → the next step
  assert.equal(stepThreshold(0.045, -1), 0.04);
  assert.equal(stepThreshold(2, 1), IMPACT_MAX);
  assert.equal(stepThreshold(0.02, -1), IMPACT_MIN);
  assert.ok(IMPACT_STEPS.every((x, i) => i === 0 || x > IMPACT_STEPS[i - 1]));
});

test("rear camera: event labels and the rear camera's problem in the watch status", () => {
  assert.equal(kindInfo("approach", "rear").label, "Approaching behind the van");
  assert.equal(kindInfo("approach").label, "Person approaching");
  assert.equal(kindInfo("impact", "rear").label, "Impact");
  const v = { enabled: true, available: true, watching: true, people: 0, error: null };
  assert.deepEqual(visionStatus({ ...v, rear: { watching: false, people: 0, error: "No frames from the rear camera" } }),
    { text: "Watching · Rear camera: No frames from the rear camera", kind: "warn" });
  assert.deepEqual(visionStatus({ ...v, rear: { watching: true, people: 0, error: null } }), { text: "Watching", kind: "ok" });
  assert.equal(visionStatus({ ...v, enabled: false, rear: { error: "x" } }).text, "Off — the camera does not guard the van");
});

test("marks without an alarm: labels and the summary line", () => {
  assert.equal(markLabel("vehicle"), "Vehicle");
  assert.equal(markLabel("person"), "Person passing by");
  assert.equal(markLabel("weird"), "Movement");
  assert.equal(marksSummary({ motion: 8, vehicle: 3, person: 1 }), "3 vehicles · 1 person · 8 movements");
  assert.equal(marksSummary({ motion: 1, vehicle: 0, person: 2 }), "2 people · 1 movement");
  assert.equal(marksSummary([{ kind: "vehicle" }, { kind: "vehicle" }, { kind: "motion" }]), "2 vehicles · 1 movement");
  assert.equal(marksSummary({ motion: 0, vehicle: 0, person: 0 }), "");
  assert.equal(marksSummary(null), "");
});
