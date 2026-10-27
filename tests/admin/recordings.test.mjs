// Recordings player: playlist URL and the choice of player (web/admin/js/rec-play.js), docs/contracts/api.md.
import test from "node:test";
import assert from "node:assert/strict";

import { hlsUrl, playerKind, HLS_MIME } from "../../web/admin/js/rec-play.js";

test("hlsUrl: the recording path + .m3u8, encoded, under the app root (device and remote prefix)", () => {
  assert.equal(hlsUrl("2026-10-20/2026-10-20_16-40-00.ts", "/"), "/api/recordings/hls/2026-10-20/2026-10-20_16-40-00.ts.m3u8");
  assert.equal(hlsUrl("nosync/boot-abcd1234_0001.ts", "/fnadmin/"), "/fnadmin/api/recordings/hls/nosync/boot-abcd1234_0001.ts.m3u8");
  assert.equal(hlsUrl("a b/c#d.ts", "/"), "/api/recordings/hls/a%20b/c%23d.ts.m3u8");
  // chunk URIs in the playlist are relative ("../../file/…"): they land on the file endpoint under any prefix
  for (const root of ["/", "/fnadmin/"]) {
    const pl = new URL(hlsUrl("2026-10-20/x.ts", root), "https://example.org");
    assert.equal(new URL("../../file/2026-10-20/x.ts", pl).pathname, root + "api/recordings/file/2026-10-20/x.ts");
  }
});

test("playerKind: WebKit plays HLS itself, others use hls.js over MSE", () => {
  const apple = "Apple Computer, Inc.";
  assert.equal(playerKind({ vendor: apple, nativeHls: true, mse: false }), "native"); // iPhone (no MediaSource)
  assert.equal(playerKind({ vendor: apple, nativeHls: true, mse: true }), "native"); // macOS Safari, iOS 17 ManagedMediaSource
  assert.equal(playerKind({ vendor: "Google Inc.", nativeHls: true, mse: true }), "hlsjs"); // Chrome with its own HLS
  assert.equal(playerKind({ vendor: "Google Inc.", nativeHls: false, mse: true }), "hlsjs"); // desktop / Android Chrome
  assert.equal(playerKind({ vendor: "", nativeHls: false, mse: true }), "hlsjs"); // Firefox
  assert.equal(playerKind({ vendor: "", nativeHls: true, mse: false }), "native");
  assert.equal(playerKind({ vendor: "", nativeHls: false, mse: false }), "none");
  assert.equal(playerKind(), "none");
  assert.equal(HLS_MIME, "application/vnd.apple.mpegurl");
});

test("alarm events in recordings: segment start, deep link, jump and marker position", async () => {
  const { segmentStartMs, recLink, parseRecLink, jumpTo, markPos, LEAD_S } = await import("../../web/admin/js/rec-play.js");
  const path = "2026-10-27/2026-10-27_22-05-00.ts";
  const t0 = new Date(2026, 9, 27, 22, 5, 0).getTime();
  assert.equal(segmentStartMs(path), t0);
  assert.equal(segmentStartMs("nosync/boot-abcd_0001.ts"), null);
  const link = recLink(path, t0 / 1000 + 27.4);
  assert.equal(link, "#/recordings?play=2026-10-27%2F2026-10-27_22-05-00.ts&t=27");
  assert.deepEqual(parseRecLink(link), { play: path, t: 27 });
  assert.deepEqual(parseRecLink("#/recordings?play=nosync%2Fa.ts"), { play: "nosync/a.ts", t: null });
  assert.equal(recLink("nosync/a.ts", 123), "#/recordings?play=nosync%2Fa.ts");
  assert.equal(parseRecLink("#/recordings"), null);
  assert.equal(jumpTo(27), 27 - LEAD_S);
  assert.equal(jumpTo(2), 0);
  assert.equal(markPos(150, 600), 0.25);
  assert.equal(markPos(700, 600), 1);
  assert.equal(markPos(10, NaN), null);
});
