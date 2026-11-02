// Guest read-only links (web/admin/js/guest-fmt.js; the server side: fnav/core/guests.py, tests/core/test_core_guests.py).
import test from "node:test";
import assert from "node:assert/strict";

import {
  guestToken, ttlSeconds, localInputValue, guestUrls, fmtLeft, fmtAgo, EXPIRY_CHOICES, MIN_TTL_S, MAX_TTL_S, REMOTE_ROOT,
} from "../../web/admin/js/guest-fmt.js";

const TOKEN = "Yq3_kP-0aZ9xYq3_kP-0aZ9xYq3_kP-0aZ9xYq3_kP0"; // secrets.token_urlsafe(32): 43 chars

test("guestToken: only #guest=<urlsafe token>, nothing else", () => {
  assert.equal(guestToken("#guest=" + TOKEN), TOKEN);
  assert.equal(guestToken("#guest=short"), null);
  assert.equal(guestToken("#guest=" + TOKEN + "&x=1"), null);
  assert.equal(guestToken("#guest=" + TOKEN.slice(0, 20) + "<script>"), null);
  assert.equal(guestToken("#/camera"), null);
  assert.equal(guestToken(""), null);
  assert.equal(guestToken(undefined), null);
});

test("ttlSeconds: presets, a custom moment, limits", () => {
  const now = new Date(2026, 10, 2, 12, 0).getTime();
  assert.deepEqual(ttlSeconds("1d", "", now), { s: 86400 });
  assert.deepEqual(ttlSeconds("7d", "", now), { s: 7 * 86400 });
  assert.deepEqual(ttlSeconds("custom", "2026-11-05T12:00", now), { s: 3 * 86400 });
  assert.ok(ttlSeconds("custom", "", now).error);
  assert.ok(ttlSeconds("custom", "2026-11-02T12:05", now).error); // under 10 minutes
  assert.ok(ttlSeconds("custom", "2028-01-01T00:00", now).error); // over a year
  assert.ok(ttlSeconds("2d", "", now).error);
  assert.equal(ttlSeconds("custom", localInputValue(now + MIN_TTL_S * 1000), now).s, MIN_TTL_S);
  assert.ok(ttlSeconds("custom", localInputValue(now + (MAX_TTL_S + 120) * 1000), now).error);
  assert.deepEqual(EXPIRY_CHOICES.map((c) => c.value), ["1d", "7d", "custom"]);
});

test("guestUrls: here, plus through the server when opened on the van's Wi-Fi and the server is known", () => {
  const path = "admin/login.html#guest=" + TOKEN;
  assert.deepEqual(guestUrls(path, { origin: "http://172.23.204.16", root: "/", remoteOrigin: null }),
    [{ where: "On the van's network", url: "http://172.23.204.16/" + path }]);
  assert.deepEqual(guestUrls(path, { origin: "http://172.23.204.16", root: "/", remoteOrigin: "https://van.example.org" }), [
    { where: "On the van's network", url: "http://172.23.204.16/" + path },
    { where: "Through the server", url: "https://van.example.org" + REMOTE_ROOT + path },
  ]);
  assert.deepEqual(guestUrls(path, { origin: "https://van.example.org", root: "/fnadmin/", remoteOrigin: "https://van.example.org" }),
    [{ where: "Through the server", url: "https://van.example.org/fnadmin/" + path }]);
});

test("fmtLeft / fmtAgo", () => {
  assert.equal(fmtLeft(-5), "expired");
  assert.equal(fmtLeft(0), "expired");
  assert.equal(fmtLeft(20), "in 1 min");
  assert.equal(fmtLeft(1800), "in 30 min");
  assert.equal(fmtLeft(5 * 3600), "in 5 h");
  assert.equal(fmtLeft(6.6 * 86400), "in 7 days");
  assert.equal(fmtAgo(null, 1000), "never");
  assert.equal(fmtAgo(990, 1000), "just now");
  assert.equal(fmtAgo(1000 - 600, 1000), "10 min ago");
  assert.equal(fmtAgo(1000 - 7200, 1000), "2 h ago");
  assert.equal(fmtAgo(1000 - 3 * 86400, 1000), "3 days ago");
});
