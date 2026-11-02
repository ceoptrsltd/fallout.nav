// Request timeouts (web/admin/js/api.js): a half-dead tunnel must not leave Arm spinning until nginx gives up (60 s).
import test from "node:test";
import assert from "node:assert/strict";

import { TIMEOUT_MS, requestTimeout, timeoutText, noConnectionText, request, ApiError } from "../../web/admin/js/api.js";

test("requestTimeout: a sane default, longer where the device or the server really works", () => {
  assert.ok(TIMEOUT_MS >= 10_000 && TIMEOUT_MS < 60_000, "shorter than nginx's 60 s");
  assert.equal(requestTimeout("POST", "/api/alarm/arm"), TIMEOUT_MS);
  assert.equal(requestTimeout("POST", "/api/camera/view"), TIMEOUT_MS); // "Show on screen"
  assert.equal(requestTimeout("GET", "/api/alarm?events=5"), TIMEOUT_MS);
  assert.equal(requestTimeout("GET", "/api/session"), TIMEOUT_MS);
  assert.equal(requestTimeout("POST", "/api/bluetooth/devices/AA%3ABB/connect"), 120_000);
  assert.equal(requestTimeout("POST", "/api/bluetooth/scan"), 120_000);
  assert.equal(requestTimeout("GET", "/api/bluetooth/state"), TIMEOUT_MS);
  assert.equal(requestTimeout("POST", "/api/push/test"), 60_000);
  assert.equal(requestTimeout("POST", "/api/push/subscribe"), 30_000);
  assert.equal(requestTimeout("POST", "/api/gps/configure"), 60_000);
  assert.equal(requestTimeout("post", "/api/system/wifi"), 60_000);
  assert.equal(requestTimeout("DELETE", "/api/system/wifi/Home"), TIMEOUT_MS);
  assert.equal(requestTimeout("DELETE", "/api/tiles"), 60_000);
  assert.equal(requestTimeout("GET", "/api/tracking/places?from=1"), 30_000);
  assert.equal(requestTimeout("GET", "/api/recordings"), 30_000);
  assert.equal(requestTimeout("GET", "/api/recordings/item/2026-10-20/x.ts"), TIMEOUT_MS);
  assert.equal(requestTimeout("GET", "/api/system/logs?unit=fnav-core&lines=200"), 30_000);
  assert.equal(requestTimeout("POST", "/api/display/output"), 25_000);
  assert.equal(requestTimeout("PATCH", "/api/settings"), 25_000);
  assert.equal(requestTimeout("GET", "https://example.com/x"), TIMEOUT_MS);
  assert.equal(requestTimeout(undefined, undefined), TIMEOUT_MS);
});

test("error texts: who did not answer, and an offline phone", () => {
  assert.equal(timeoutText("/api/alarm/arm", 15000), "No answer from the device in 15 s — the connection is slow or down");
  assert.match(timeoutText("/api/tracking/places?from=1", 30000), /^No answer from the server in 30 s/);
  assert.match(timeoutText("/api/push/test", 60000), /the server in 60 s/);
  assert.equal(noConnectionText(false), "No connection: this phone is offline");
  assert.equal(noConnectionText(true), "No connection to the device");
  assert.equal(noConnectionText(undefined), "No connection to the device");
});

/** fetch that never answers on its own, like a dead tunnel, but honours the abort signal. */
function hangingFetch() {
  return (_url, init) => new Promise((_res, rej) => {
    init.signal.addEventListener("abort", () => rej(Object.assign(new Error("aborted"), { name: "AbortError" })));
  });
}

test("request: gives up after its timeout with a clear ApiError", async (t) => {
  t.mock.method(globalThis, "fetch", hangingFetch());
  const t0 = Date.now();
  await assert.rejects(request("POST", "/api/alarm/arm", {}, { timeout: 60 }), (e) => {
    assert.ok(e instanceof ApiError);
    assert.equal(e.status, 0);
    assert.equal(e.timeout, true);
    assert.equal(e.message, "No answer from the device in 0 s — the connection is slow or down");
    return true;
  });
  assert.ok(Date.now() - t0 < 2000);
});

test("request: the caller's own abort is passed on as is, not as a timeout", async (t) => {
  t.mock.method(globalThis, "fetch", hangingFetch());
  const ctl = new AbortController();
  const p = request("GET", "/api/alarm", undefined, { init: { signal: ctl.signal }, timeout: 5000 });
  ctl.abort();
  await assert.rejects(p, (e) => e.name === "AbortError" && !(e instanceof ApiError));
});

test("request: a quick answer is not affected; a raw response is limited only until its headers", async (t) => {
  t.mock.method(globalThis, "fetch", async (_url, init) => {
    assert.ok(init.signal, "every request carries a signal");
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  });
  assert.deepEqual(await request("GET", "/api/alarm", undefined, { timeout: 50 }), { ok: true });
  const raw = await request("GET", "/api/camera/live.ts", undefined, { raw: true, timeout: 20 });
  await new Promise((r) => setTimeout(r, 60)); // past the timeout: the stream must still be readable
  assert.deepEqual(await raw.json(), { ok: true });
});

test("request: a refused connection is 'No connection to the device'", async (t) => {
  t.mock.method(globalThis, "fetch", async () => { throw new TypeError("Failed to fetch"); });
  await assert.rejects(request("GET", "/api/alarm"), (e) => e instanceof ApiError && e.status === 0 && !e.timeout
    && /^No connection/.test(e.message));
});
