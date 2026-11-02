// The admin's service worker (web/admin/sw.js, docs/contracts/alarm.md §7): notifications from a push body — the
// "Silence" button and the event's snapshot — and the button itself: POST api/alarm/silence on the device, a result
// notification. sw.js is a classic worker script: it runs here in a vm context with a fake `self`.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const CODE = readFileSync(new URL("../../web/admin/sw.js", import.meta.url), "utf8");

/** sw.js loaded under scope; fetchImpl answers its fetch() calls. → {handlers, shown, fetches, opened} */
function load(scope, fetchImpl = async () => { throw new Error("no fetch"); }) {
  const handlers = {};
  const shown = [];
  const fetches = [];
  const opened = [];
  const self = {
    registration: { scope, showNotification: async (title, opts) => { shown.push({ title, opts }); } },
    clients: { matchAll: async () => [], openWindow: async (u) => { opened.push(u); }, claim: async () => {} },
    navigator: { userAgent: "Mozilla/5.0 (Linux; Android 14) Chrome/126.0" },
    addEventListener: (type, fn) => { handlers[type] = fn; },
    skipWaiting: () => {},
  };
  const fetch = async (url, init) => {
    fetches.push({ url, init });
    return fetchImpl(url, init);
  };
  vm.runInNewContext(CODE, { self, fetch, URL, atob, console });
  return { handlers, shown, fetches, opened };
}

/** Fire an event at a handler; resolves when its waitUntil() promise does. */
async function fire(handler, ev) {
  let p = Promise.resolve();
  handler({ ...ev, waitUntil: (x) => { p = x; } });
  await p;
}

const push = (body) => ({ data: { json: () => body, text: () => JSON.stringify(body) } });
const note = (data = { url: "#/alarm" }) => ({ data, close() { this.closed = true; } });
const json = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });

const REMOTE = "https://van.example.org/fnadmin/admin/";
const plain = (x) => JSON.parse(JSON.stringify(x)); // objects from the vm context have its own Object prototype

test("alarm push: Silence button and the snapshot as a big picture", async () => {
  const sw = load(REMOTE);
  await fire(sw.handlers.push, push({ title: "Person approaching", body: "Someone came close to the van at 14:03", tag: "approach",
    url: "#/alarm", ts: 1792000000.5, image: "../api/push/snap/AbCdEfGhIjKlMnOpQrStUvWxYz012345.jpg" }));
  const { title, opts } = sw.shown[0];
  assert.equal(title, "Person approaching");
  assert.equal(opts.image, "https://van.example.org/fnadmin/api/push/snap/AbCdEfGhIjKlMnOpQrStUvWxYz012345.jpg");
  assert.deepEqual(plain(opts.actions), [{ action: "silence", title: "Silence" }]);
  assert.equal(opts.tag, "approach");
  assert.equal(opts.timestamp, 1792000000500);
  assert.equal(opts.icon, "https://van.example.org/fnadmin/admin/icons/icon-192.png");
  assert.deepEqual(plain(opts.data), { url: "#/alarm" });
});

test("notes get no Silence button; no image without a snapshot", async () => {
  const sw = load(REMOTE);
  for (const tag of ["test", "armed", "disarmed", "video_back", "offline", "siren"]) {
    await fire(sw.handlers.push, push({ title: "x", body: "y", tag, url: "#/alarm", ts: 1 }));
  }
  await fire(sw.handlers.push, push({ title: "Impact detected", body: "Hit 0.42 g", tag: "impact", url: "#/alarm", ts: 1 }));
  assert.deepEqual(sw.shown.map((s) => !!s.opts.actions), [false, false, false, false, false, false, true]);
  assert.ok(sw.shown.every((s) => !("image" in s.opts)));
});

test("Silence: POST api/alarm/silence with the session cookie, a quiet result notification", async () => {
  const sw = load(REMOTE, async () => json(200, { state: "triggered", siren: false }));
  const n = note();
  await fire(sw.handlers.notificationclick, { action: "silence", notification: n });
  assert.ok(n.closed);
  assert.equal(sw.fetches.length, 1);
  assert.equal(sw.fetches[0].url, "https://van.example.org/fnadmin/api/alarm/silence");
  assert.deepEqual(plain(sw.fetches[0].init), { method: "POST", credentials: "same-origin" });
  const { title, opts } = sw.shown[0];
  assert.equal(title, "Siren and hazard lights stopped");
  assert.equal(opts.body, "The alarm stays armed.");
  assert.equal(opts.tag, "silence");
  assert.equal(opts.silent, true);
  assert.ok(!opts.actions);
});

test("Silence on the device itself: the API at the root", async () => {
  const sw = load("http://fnav.local/admin/", async () => json(200, { state: "off" }));
  await fire(sw.handlers.notificationclick, { action: "silence", notification: note() });
  assert.equal(sw.fetches[0].url, "http://fnav.local/api/alarm/silence");
  assert.equal(sw.shown[0].opts.body, "The alarm is off.");
});

test("Silence failed: a notification that says why and opens the Alarm page", async () => {
  const cases = [
    [async () => json(503, { detail: "Device is offline" }), /The van is offline/],
    [async () => json(401, { detail: "Not authenticated" }), /Signed out/],
    [async () => json(500, { detail: "Internal server error" }), /^Internal server error$/],
    [async () => ({ ok: false, status: 502, json: async () => { throw new SyntaxError("not JSON"); } }), /error 502/],
    [async () => { throw new TypeError("Failed to fetch"); }, /No connection to the van \(Failed to fetch\)/],
  ];
  for (const [answer, body] of cases) {
    const sw = load(REMOTE, answer);
    await fire(sw.handlers.notificationclick, { action: "silence", notification: note() });
    const { title, opts } = sw.shown[0];
    assert.equal(title, "Could not silence the alarm");
    assert.match(opts.body, body);
    assert.equal(opts.silent, false);
    assert.deepEqual(plain(opts.data), { url: "#/alarm" });
  }
});

test("a tap on the notification (no button) opens the event's page, not the API", async () => {
  const sw = load(REMOTE);
  await fire(sw.handlers.notificationclick, { action: "", notification: note({ url: "#/tracking" }) });
  assert.deepEqual(plain(sw.opened), ["https://van.example.org/fnadmin/admin/#/tracking"]);
  assert.deepEqual(sw.fetches, []);
});
