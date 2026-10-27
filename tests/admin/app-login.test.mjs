// The installed app asks for the password on its first start on a device (web/admin/js/app-login.js).
import test from "node:test";
import assert from "node:assert/strict";

import { APP_LOGIN_KEY, isStandalone, needsAppLogin, markAppLogin, clearAppLogin } from "../../web/admin/js/app-login.js";

function fakeWindow({ mode = "browser", iosHome = false, storage = "ok" } = {}) {
  const data = new Map();
  const ls = {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => { if (storage === "readonly") throw new Error("quota"); data.set(k, String(v)); },
    removeItem: (k) => data.delete(k),
  };
  const w = {
    navigator: iosHome ? { standalone: true } : {},
    matchMedia: (q) => ({ matches: q === `(display-mode: ${mode})` }),
    data,
  };
  if (storage === "throws") Object.defineProperty(w, "localStorage", { get() { throw new Error("denied"); } });
  else w.localStorage = ls;
  return w;
}

test("isStandalone: installed app (Android/desktop display-mode, iOS Home Screen), not a browser tab", () => {
  assert.equal(isStandalone(fakeWindow()), false);
  assert.equal(isStandalone(fakeWindow({ mode: "standalone" })), true);
  assert.equal(isStandalone(fakeWindow({ mode: "fullscreen" })), true);
  assert.equal(isStandalone(fakeWindow({ iosHome: true })), true);
  assert.equal(isStandalone({}), false);
});

test("needsAppLogin: only in the app, until a sign-in made inside it; Log out asks again", () => {
  const browser = fakeWindow();
  assert.equal(needsAppLogin(browser), false); // a browser tab: the usual session
  markAppLogin(browser);
  assert.equal(browser.data.has(APP_LOGIN_KEY), false); // signing in in the browser does not unlock the app

  const app = fakeWindow({ mode: "standalone" });
  assert.equal(needsAppLogin(app), true); // first start: the password even with the browser's cookie
  markAppLogin(app);
  assert.equal(needsAppLogin(app), false);
  clearAppLogin(app);
  assert.equal(needsAppLogin(app), true);
});

test("needsAppLogin: no usable storage - no endless sign-in loop", () => {
  assert.equal(needsAppLogin(fakeWindow({ mode: "standalone", storage: "throws" })), false);
  const ro = fakeWindow({ mode: "standalone", storage: "readonly" }); // e.g. a private mode refusing writes
  assert.doesNotThrow(() => markAppLogin(ro));
  assert.equal(needsAppLogin(ro), false);
  assert.equal(ro.data.size, 0);
});
