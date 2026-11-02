// WebSocket экрана: сторож тишины не ждёт onclose (web/display/map/link.js, B25).
import test from "node:test";
import assert from "node:assert/strict";

import { Link } from "../../web/display/map/link.js";

class FakeWS {
  static all = [];
  constructor(url) {
    this.url = url;
    this.closed = false;
    this.onopen = this.onmessage = this.onclose = this.onerror = null;
    FakeWS.all.push(this);
  }
  close() {
    this.closed = true; // настоящий Chromium на мёртвом соединении пришлёт onclose только через ~60 с
  }
  send() {}
}

function setup() {
  FakeWS.all = [];
  globalThis.WebSocket = FakeWS;
  let now = 100;
  const msgs = [];
  const link = new Link("ws://x/ws", { onMessage: (m) => msgs.push(m), now: () => now });
  link._connect();
  return { link, msgs, tick: (s) => (now += s), ws: () => FakeWS.all[FakeWS.all.length - 1] };
}

test("Link: silent connection — dropped and reopened at once, without waiting for onclose", () => {
  const { link, msgs, tick, ws } = setup();
  const first = ws();
  first.onopen();
  first.onmessage({ data: '{"type":"telemetry","n":1}' });
  assert.equal(msgs.length, 1);
  tick(4);
  link.check();
  assert.equal(FakeWS.all.length, 1, "4 s of silence: still waiting");
  tick(1.5);
  link.check();
  assert.equal(FakeWS.all.length, 2, "reconnected at once");
  assert.ok(first.closed);
  assert.equal(first.onclose, null, "the old socket is detached");
  assert.equal(link.open, false);
  assert.equal(link.ws, ws());
  assert.equal(link.drops, 1);
  // старый сокет, ожив, ничего не портит
  first.onmessage?.({ data: '{"type":"x"}' });
  assert.equal(msgs.length, 1);
  // новый открылся: свежий сокет без данных не рвётся сразу (у него свои 5 с)
  ws().onopen();
  assert.equal(link.open, true);
  tick(2);
  link.check();
  assert.equal(FakeWS.all.length, 2);
  ws().onmessage({ data: '{"type":"telemetry","n":2}' });
  assert.equal(msgs.length, 2);
  assert.ok(link.connected(3));
});

test("Link: a connection that never opens is retried after connectS", () => {
  const { link, tick, ws } = setup();
  const first = ws();
  tick(9);
  link.check();
  assert.equal(FakeWS.all.length, 1);
  tick(2);
  link.check();
  assert.equal(FakeWS.all.length, 2);
  assert.ok(first.closed);
});

test("Link: a normal close goes through the backoff as before", () => {
  const { link, ws } = setup();
  const first = ws();
  first.onopen();
  first.onclose();
  assert.equal(link.ws, null);
  assert.ok(link.timer, "reconnect scheduled");
  clearTimeout(link.timer);
});
