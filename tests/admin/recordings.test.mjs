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
