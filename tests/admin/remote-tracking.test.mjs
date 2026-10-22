import test from "node:test";
import assert from "node:assert/strict";

import { appRoot, appUrl, safeNext } from "../../web/admin/js/api.js";
import * as dispRoot from "../../web/display/map/root.js";
import { tileUrl } from "../../web/display/map/layers.js";
import { esriTileUrl as demoEsri } from "../../web/display/demo/demo.js";
import { esriTileUrl } from "../../web/display/map/esri.js";
import {
  RANGES, rangeFrom, rangeQuery, fmtStay, fmtDay, fmtTime, fmtWhen, fmtVisit, agoText, sortVisits, esriTemplates,
} from "../../web/admin/js/tracking-fmt.js";

// ── address prefix (docs/contracts/remote.md §1) ──

test("appRoot: device, remote prefix, public demo, edge cases", () => {
  assert.equal(appRoot("/admin/"), "/");
  assert.equal(appRoot("/admin/index.html"), "/");
  assert.equal(appRoot("/admin"), "/");
  assert.equal(appRoot("/display/"), "/");
  assert.equal(appRoot("/fnadmin/admin/"), "/fnadmin/");
  assert.equal(appRoot("/fnadmin/admin/login.html"), "/fnadmin/");
  assert.equal(appRoot("/fnadmin/display/"), "/fnadmin/");
  assert.equal(appRoot("/apps/fallout.nav/display/"), "/apps/fallout.nav/");
  assert.equal(appRoot("/"), "/");
  assert.equal(appRoot(""), "/");
  // "fnadmin" itself must not be mistaken for the "admin" segment
  assert.equal(appRoot("/fnadmin/"), "/fnadmin/");
});

test("appUrl: relative to the root, absolute URLs untouched", () => {
  assert.equal(appUrl("api/session", "/"), "/api/session");
  assert.equal(appUrl("/api/session", "/"), "/api/session");
  assert.equal(appUrl("/api/session", "/fnadmin/"), "/fnadmin/api/session");
  assert.equal(appUrl("display/?preview=1", "/fnadmin/"), "/fnadmin/display/?preview=1");
  assert.equal(appUrl("https://server.arcgisonline.com/x", "/fnadmin/"), "https://server.arcgisonline.com/x");
  assert.equal(appUrl("//cdn.example/x", "/fnadmin/"), "//cdn.example/x");
});

test("admin and display root helpers are the same functions", () => {
  for (const p of ["/admin/", "/fnadmin/admin/x", "/fnadmin/display/", "/apps/fallout.nav/display/", "/"]) {
    assert.equal(dispRoot.appRoot(p), appRoot(p), p);
  }
  for (const [u, r] of [["api/x", "/"], ["/ws", "/fnadmin/"], ["https://a/b", "/q/"]]) {
    assert.equal(dispRoot.appUrl(u, r), appUrl(u, r));
  }
  // under node there is no location: the device root
  assert.equal(dispRoot.ROOT, "/");
  assert.equal(dispRoot.REMOTE, false);
});

test("safeNext: only admin/ paths, no open redirect", () => {
  assert.equal(safeNext("admin/#/camera", "/fnadmin/"), "/fnadmin/admin/#/camera");
  assert.equal(safeNext("/admin/#/gps", "/"), "/admin/#/gps"); // the device middleware sends absolute paths
  assert.equal(safeNext("/admin/", "/fnadmin/"), "/fnadmin/admin/");
  for (const bad of [null, "", "https://evil.example/", "//evil.example/admin/", "admin/../api/x", "admin//x", "admin/login.html", "api/settings", "javascript:alert(1)"]) {
    assert.equal(safeNext(bad, "/fnadmin/"), "/fnadmin/admin/", String(bad));
  }
});

test("tiles: via the device cache by default, Esri directly when shared helper is used", () => {
  assert.equal(tileUrl("imagery", 17, 1, 2), "/tiles/imagery/17/1/2");
  assert.equal(tileUrl("imagery", 17, 1, 2, "/fnadmin/"), "/fnadmin/tiles/imagery/17/1/2");
  const s = { tiles: { imagery_url: "https://e/{z}/{y}/{x}", esri_api_key: "k y" } };
  assert.equal(esriTileUrl(s, "imagery", 3, 4, 5), "https://e/3/5/4?token=k%20y");
  assert.equal(demoEsri, esriTileUrl); // the demo re-exports the shared function
});

// ── tracking helpers ──

const T = (y, mo, d, hh, mm) => new Date(y, mo - 1, d, hh, mm, 0).getTime() / 1000;

test("ranges: from-timestamps and query strings", () => {
  assert.deepEqual(RANGES.map((r) => r.value), ["24h", "7d", "30d", "all"]);
  assert.equal(rangeFrom("24h", 100000), 100000 - 86400);
  assert.equal(rangeFrom("7d", 1e9), 1e9 - 7 * 86400);
  assert.equal(rangeFrom("all", 1e9), null);
  assert.equal(rangeFrom("bogus", 1e9), null);
  assert.equal(rangeQuery("30d", 1e9 + 0.7), "?from=" + Math.floor(1e9 + 0.7 - 30 * 86400));
  assert.equal(rangeQuery("all", 1e9), "");
});

test("fmtStay: short, minutes, hours, days", () => {
  assert.equal(fmtStay(0), "< 1 min");
  assert.equal(fmtStay(59.9), "< 1 min");
  assert.equal(fmtStay(60), "1 min");
  assert.equal(fmtStay(45 * 60 + 30), "45 min");
  assert.equal(fmtStay(3600), "1 h");
  assert.equal(fmtStay(2 * 3600 + 10 * 60), "2 h 10 min");
  assert.equal(fmtStay(2 * 86400 + 5 * 3600 + 59 * 60), "2 d 5 h");
  assert.equal(fmtStay(3 * 86400), "3 d");
  assert.equal(fmtStay(null), "—");
  assert.equal(fmtStay(-5), "—");
});

test("dates: day, time, relative 'when'", () => {
  const t = T(2026, 10, 22, 14, 5);
  assert.equal(fmtDay(t), "Thu 22 Oct 2026");
  assert.equal(fmtTime(t), "14:05");
  assert.equal(fmtWhen(t, T(2026, 10, 22, 18, 0)), "today 14:05");
  assert.equal(fmtWhen(t, T(2026, 10, 23, 9, 0)), "yesterday 14:05");
  assert.equal(fmtWhen(t, T(2026, 10, 26, 9, 0)), "Thu 22 Oct 14:05");
  assert.equal(fmtWhen(t, T(2026, 12, 1, 9, 0)), "22 Oct 14:05");
  assert.equal(fmtWhen(t, T(2027, 3, 1, 9, 0)), "22 Oct 2026 14:05");
});

test("fmtVisit: same day, overnight, passing", () => {
  const a = fmtVisit({ start_ts: T(2026, 10, 22, 14, 5), end_ts: T(2026, 10, 22, 15, 40), duration_s: 95 * 60 });
  assert.deepEqual(a, { date: "Thu 22 Oct 2026", arrived: "14:05", left: "15:40", duration: "1 h 35 min", passing: false });
  const b = fmtVisit({ start_ts: T(2026, 10, 22, 19, 30), end_ts: T(2026, 10, 23, 8, 10) });
  assert.equal(b.left, "Fri 23 Oct 08:10");
  assert.equal(b.duration, "12 h 40 min");
  const c = fmtVisit({ start_ts: T(2026, 10, 22, 9, 0), end_ts: T(2026, 10, 22, 9, 0), duration_s: 0 });
  assert.equal(c.passing, true);
  assert.equal(c.duration, "< 1 min");
});

test("agoText and sortVisits", () => {
  assert.equal(agoText(1000, 1030), "just now");
  assert.equal(agoText(1000, 1000 + 5 * 60), "5 min ago");
  assert.equal(agoText(1000, 1000 + 3 * 3600 + 5), "3 h ago");
  assert.equal(agoText(1000, 1000 + 2 * 86400), "2 d ago");
  assert.equal(agoText(2000, 1000), "just now"); // clock skew
  const v = sortVisits([{ start_ts: 1 }, { start_ts: 3 }, { start_ts: 2 }]);
  assert.deepEqual(v.map((x) => x.start_ts), [3, 2, 1]);
  assert.deepEqual(sortVisits(null), []);
});

test("esriTemplates: hybrid layers, settings templates and the API key", () => {
  const d = esriTemplates(null);
  assert.match(d.imagery, /World_Imagery\/MapServer\/tile\/\{z\}\/\{y\}\/\{x\}$/);
  assert.match(d.places, /Reference\/World_Boundaries_and_Places\//);
  assert.match(d.transport, /Reference\/World_Transportation\//);
  const k = esriTemplates({ tiles: { imagery_url: "https://x/{z}/{y}/{x}?f=1", esri_api_key: "abc" } });
  assert.equal(k.imagery, "https://x/{z}/{y}/{x}?f=1&token=abc");
  assert.ok(k.places.endsWith("?token=abc"));
});

test("googleMapsUrl: coordinates as a search query", async () => {
  const { googleMapsUrl } = await import("../../web/admin/js/tracking-fmt.js");
  assert.equal(googleMapsUrl(36.1146958, -115.1728), "https://www.google.com/maps/search/?api=1&query=36.114696%2C-115.172800");
  assert.equal(googleMapsUrl(-33.9, -70.1), "https://www.google.com/maps/search/?api=1&query=-33.900000%2C-70.100000");
});
