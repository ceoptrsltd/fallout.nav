// Connection to the device (web/admin/js/conn-fmt.js): WebSocket liveness, the online state with hysteresis,
// the banner, the last data kept for the offline view.
import test from "node:test";
import assert from "node:assert/strict";

import {
  PING_MS, SILENCE_MS, CONNECT_MS, PROBE_MS, FRESH_MS, OFF_MS, ON_MS, RETRY_MS, wsCheck, nextRetry, linkStep, runStart,
  linkBanner, packKept, unpackKept, lastSeenText, KEEP_PREFIX,
} from "../../web/admin/js/conn-fmt.js";

const T0 = 1_800_000_000_000;
const open = (o = {}) => ({ state: 1, now: T0, createdAt: T0 - 60_000, openedAt: T0 - 59_000, msgAt: T0, pingAt: T0, probeAt: 0, hidden: false, ...o });

test("thresholds: a silent socket is caught long before the ~90 s of a half-dead tunnel", () => {
  assert.ok(SILENCE_MS < OFF_MS, "a zombie is replaced before the UI goes offline");
  assert.ok(PROBE_MS < SILENCE_MS && PING_MS > 0 && CONNECT_MS < 60_000);
  assert.ok(ON_MS < OFF_MS && FRESH_MS < OFF_MS);
});

test("wsCheck: telemetry flowing — ok; ping every PING_MS", () => {
  assert.equal(wsCheck(open()), "ok");
  assert.equal(wsCheck(open({ pingAt: T0 - PING_MS })), "ping");
  assert.equal(wsCheck(open({ pingAt: 0 })), "ping");
  assert.equal(wsCheck(open({ msgAt: T0 - 1000, pingAt: T0 - 100 })), "ok");
});

test("wsCheck: an open socket silent for SILENCE_MS is dropped (zombie)", () => {
  assert.equal(wsCheck(open({ msgAt: T0 - SILENCE_MS + 1 })), "ok");
  assert.equal(wsCheck(open({ msgAt: T0 - SILENCE_MS - 1 })), "drop");
  // never said anything since it opened
  assert.equal(wsCheck(open({ msgAt: 0, openedAt: T0 - SILENCE_MS - 1 })), "drop");
  assert.equal(wsCheck(open({ msgAt: 0, openedAt: T0 - 100 })), "ok");
});

test("wsCheck: hidden — no verdict on silence (timers are throttled, the OS may suspend the page)", () => {
  assert.notEqual(wsCheck(open({ msgAt: T0 - 600_000, hidden: true })), "drop");
});

test("wsCheck: back on screen the probe decides — PROBE_MS for any message, the old silence does not count", () => {
  const probeAt = T0 - 500;
  // the page was frozen for minutes: no drop while the probe waits
  assert.equal(wsCheck(open({ msgAt: T0 - 600_000, probeAt })), "ok");
  assert.equal(wsCheck(open({ msgAt: T0 - 600_000, probeAt: T0 - PROBE_MS - 1 })), "drop");
  // something came after the probe: alive
  assert.equal(wsCheck(open({ msgAt: probeAt + 10, probeAt: T0 - PROBE_MS - 1, pingAt: T0 })), "ok");
});

test("wsCheck: stuck connecting / never-closing sockets are given up", () => {
  assert.equal(wsCheck({ state: 0, now: T0, createdAt: T0 - CONNECT_MS + 1 }), "ok");
  assert.equal(wsCheck({ state: 0, now: T0, createdAt: T0 - CONNECT_MS - 1 }), "drop");
  assert.equal(wsCheck({ state: 2, now: T0, createdAt: T0 - 60_000, openedAt: T0 - 59_000, msgAt: T0 - SILENCE_MS - 1 }), "drop");
  assert.equal(wsCheck({ state: 2, now: T0, createdAt: T0 - 60_000, openedAt: T0 - 59_000, msgAt: T0 - 100 }), "ok");
  assert.equal(wsCheck({ state: -1, now: T0 }), "ok");
});

test("nextRetry: back-off from 0.5 s up to 5 s", () => {
  assert.equal(nextRetry(RETRY_MS.first), RETRY_MS.first * RETRY_MS.factor);
  let r = RETRY_MS.first;
  for (let i = 0; i < 20; i++) r = nextRetry(r);
  assert.equal(r, RETRY_MS.max);
  assert.equal(nextRetry(0), RETRY_MS.first * RETRY_MS.factor);
});

const L = (o = {}) => ({ now: T0, telAt: T0 - 100, runAt: T0 - 10_000, wsOpen: true, hidden: false, resumeAt: 0, startAt: T0 - 60_000, ...o });

test("linkStep: the first telemetry — online at once", () => {
  assert.equal(linkStep(null, L({ runAt: T0 - 100 })), true);
  assert.equal(linkStep(null, L({ telAt: 0, startAt: T0 - 1000 })), null); // still connecting
  assert.equal(linkStep(null, L({ telAt: 0, startAt: T0 - OFF_MS - 1 })), false); // never came: known lost
  assert.equal(linkStep(null, L({ telAt: 0, startAt: T0 - OFF_MS - 1, hidden: true })), null);
});

test("linkStep: online → offline only after OFF_MS without telemetry (not 3 s), a closed socket alone is not enough", () => {
  assert.equal(linkStep(true, L({ telAt: T0 - 3500 })), true);
  assert.equal(linkStep(true, L({ telAt: T0 - 3500, wsOpen: false })), true); // reconnecting right away
  assert.equal(linkStep(true, L({ telAt: T0 - OFF_MS - 1 })), false);
});

test("linkStep: hidden keeps the state; back on screen the socket gets OFF_MS before 'lost'", () => {
  assert.equal(linkStep(true, L({ telAt: T0 - 600_000, hidden: true })), true);
  assert.equal(linkStep(true, L({ telAt: T0 - 600_000, resumeAt: T0 - 1000 })), true);
  assert.equal(linkStep(true, L({ telAt: T0 - 600_000, resumeAt: T0 - OFF_MS - 1 })), false);
});

test("linkStep: offline → online only after ON_MS of steady telemetry (no lost / restored pairs)", () => {
  assert.equal(linkStep(false, L({ runAt: T0 - 200 })), false); // one packet after a drop
  assert.equal(linkStep(false, L({ runAt: T0 - ON_MS })), true);
  assert.equal(linkStep(false, L({ runAt: T0 - ON_MS, telAt: T0 - FRESH_MS - 1 })), false); // the run stopped
  assert.equal(linkStep(false, L({ runAt: T0 - ON_MS, wsOpen: false })), false);
});

test("linkStep: a weak link with 4 s gaps stays online instead of flapping", () => {
  let s = true;
  for (let t = 0; t < 60_000; t += 500) {
    const telAt = T0 + Math.floor(t / 4000) * 4000; // a packet every 4 s
    s = linkStep(s, L({ now: T0 + t, telAt, runAt: telAt }));
    assert.equal(s, true, "at " + t);
  }
});

test("runStart: packets closer than FRESH_MS continue the run, a gap starts a new one", () => {
  assert.equal(runStart(T0 - 5000, T0 - 500, T0), T0 - 5000);
  assert.equal(runStart(T0 - 5000, T0 - FRESH_MS - 1, T0), T0);
  assert.equal(runStart(0, 0, T0), T0);
  assert.equal(runStart(0, T0 - 100, T0), T0);
});

test("linkBanner: server-confirmed offline, lost link, nothing while online or not known yet", () => {
  assert.match(linkBanner({ offline: true, online: false }).text, /^Device is offline/);
  assert.match(linkBanner({ offline: true, online: null }).text, /from the server/);
  assert.match(linkBanner({ offline: false, online: false }).text, /reconnecting/);
  assert.equal(linkBanner({ offline: false, online: true }), null);
  assert.equal(linkBanner({ offline: false, online: null }), null);
});

test("kept values: round trip, junk and wrong clocks are rejected", () => {
  assert.equal(KEEP_PREFIX, "fnav.last.");
  const raw = packKept({ gps: { lat: 52.2 } }, T0);
  assert.deepEqual(unpackKept(raw, T0 + 1000), { at: T0, v: { gps: { lat: 52.2 } } });
  assert.equal(unpackKept(null, T0), null);
  assert.equal(unpackKept("", T0), null);
  assert.equal(unpackKept("{bad", T0), null);
  assert.equal(unpackKept(JSON.stringify({ at: "x", v: {} }), T0), null);
  assert.equal(unpackKept(JSON.stringify({ at: T0, v: 5 }), T0), null);
  assert.equal(unpackKept(JSON.stringify({ at: 0, v: {} }), T0), null);
  assert.equal(unpackKept(packKept({}, T0 + 2 * 86400000), T0), null); // from the future: the phone's clock jumped
});

test("lastSeenText", () => {
  assert.equal(lastSeenText(T0 - 12 * 60_000, T0), "Last seen 12 min ago");
  assert.equal(lastSeenText(T0 - 10_000, T0), "Last seen just now");
  assert.equal(lastSeenText(T0 - 3 * 3600_000, T0), "Last seen 3 h ago");
  assert.equal(lastSeenText(0, T0), "");
  assert.equal(lastSeenText(null, T0), "");
});
