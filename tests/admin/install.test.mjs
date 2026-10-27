// "Install app": which mode and which steps for which browser (web/admin/js/install-fmt.js).
import test from "node:test";
import assert from "node:assert/strict";

import { platformOf, installMode, installSteps, stepText } from "../../web/admin/js/install-fmt.js";

const UA = {
  iphone: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
  iphoneChrome: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/126.0.6478.54 Mobile/15E148 Safari/604.1",
  ipadOs: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
  android: "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
  samsung: "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/25.0 Chrome/121.0.0.0 Mobile Safari/537.36",
  androidFirefox: "Mozilla/5.0 (Android 14; Mobile; rv:128.0) Gecko/128.0 Firefox/128.0",
  edge: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
  winChrome: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
  macSafari: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
  firefoxLinux: "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
};

const modeFor = (ua, extra = {}) => {
  const p = platformOf({ ua, platform: extra.platform || "", touch: extra.touch || 0 });
  return installMode({ ...p, ...extra });
};

test("platform: os, browser, iOS / Android", () => {
  assert.deepEqual(platformOf({ ua: UA.iphone }), { os: "iPhone", browser: "Safari", ios: true, android: false, mobile: true });
  assert.equal(platformOf({ ua: UA.iphoneChrome }).browser, "Chrome");
  assert.equal(platformOf({ ua: UA.ipadOs, platform: "MacIntel", touch: 5 }).ios, true); // iPadOS reports a Mac
  assert.equal(platformOf({ ua: UA.macSafari, platform: "MacIntel", touch: 0 }).ios, false);
  assert.deepEqual(platformOf({ ua: UA.samsung }), { os: "Android", browser: "Samsung Internet", ios: false, android: true, mobile: true });
  assert.equal(platformOf({ ua: UA.edge }).mobile, false);
});

test("mode: installed hides everything; a kept prompt wins; otherwise by platform", () => {
  assert.equal(modeFor(UA.android, { standalone: true, hasPrompt: true }), "installed");
  assert.equal(modeFor(UA.iphone, { standalone: true }), "installed");
  assert.equal(modeFor(UA.android, { hasPrompt: true }), "prompt");
  assert.equal(modeFor(UA.winChrome, { hasPrompt: true }), "prompt");
  assert.equal(modeFor(UA.android), "android"); // dismissed earlier or not offered yet: the menu
  assert.equal(modeFor(UA.samsung), "android");
  assert.equal(modeFor(UA.iphone), "ios");
  assert.equal(modeFor(UA.iphoneChrome), "ios");
  assert.equal(modeFor(UA.ipadOs, { platform: "MacIntel", touch: 5 }), "ios");
  assert.equal(modeFor(UA.winChrome), "desktop");
  assert.equal(modeFor(UA.macSafari, { platform: "MacIntel" }), "desktop");
  assert.equal(installMode(), "desktop");
});

const texts = (mode, browser, opts) => {
  const r = installSteps(mode, browser, opts);
  return { ...r, all: r.steps.map(stepText).join(" | ") };
};

test("steps: iPhone Safari — Share → Add to Home Screen, and why (notifications)", () => {
  const r = texts("ios", "Safari");
  assert.equal(r.title, "Add to Home Screen");
  assert.match(r.all, /\[Share\]/);
  assert.match(r.all, /Add to Home Screen/);
  assert.match(r.note, /notifications/);
  const c = texts("ios", "Chrome");
  assert.match(c.all, /Add to Home Screen/);
  assert.match(c.all, /Safari/); // fallback: open in Safari
});

test("steps: Android by browser", () => {
  assert.match(texts("android", "Chrome").all, /⋮.*Install app.*Add to Home screen/);
  assert.match(texts("android", "Samsung Internet").all, /Add page to.*Home screen/);
  assert.match(texts("android", "Firefox").all, /Add app to Home screen/);
  assert.match(texts("android", "Edge").all, /Add to phone/);
});

test("steps: desktop by browser", () => {
  assert.match(texts("desktop", "Chrome").all, /address bar.*Install page as app/);
  assert.match(texts("desktop", "Edge").all, /Install this site as an app/);
  assert.match(texts("desktop", "Safari").all, /Add to Dock/);
  assert.match(texts("desktop", "Firefox").all, /Chrome.*Edge/);
});

test("steps: the device's plain-http address points to the secure remote address", () => {
  assert.equal(texts("android", "Chrome", { secure: true }).note, "");
  assert.match(texts("android", "Chrome", { secure: false }).note, /https:\/\/<your server>\/fnadmin\//);
  const ios = texts("ios", "Safari", { secure: false });
  assert.match(ios.note, /notifications/);
  assert.match(ios.note, /fnadmin/);
  assert.match(texts("prompt", "Chrome").all, /Install/);
});

test("stepText renders parts", () => {
  assert.equal(stepText(["Tap ", { icon: "share" }, " ", { b: "Share" }, "."]), "Tap [Share] Share.");
  assert.equal(stepText(["Menu ", { icon: "dots" }]), "Menu ⋮");
  assert.equal(stepText(null), "");
});
