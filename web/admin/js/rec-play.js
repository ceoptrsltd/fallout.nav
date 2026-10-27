// Recordings playback: playlist URL and the choice of player (pure helpers, tests/admin/recordings.test.mjs).
import { appUrl, encPath } from "./api.js";

/** HLS playlist of a recording (docs/contracts/api.md): byte ranges of the .ts itself, so any point is seekable. */
export function hlsUrl(path, root) {
  return appUrl("api/recordings/hls/" + encPath(path) + ".m3u8", root);
}

/**
 * Which player: "native" — Safari and every iPhone browser (WebKit plays HLS itself; an iPhone has no
 * MediaSource for hls.js), "hlsjs" — everyone else with MSE, "none" — neither.
 * vendor: navigator.vendor; nativeHls: video.canPlayType(HLS) is not ""; mse: MediaSource or ManagedMediaSource.
 */
export function playerKind({ vendor = "", nativeHls = false, mse = false } = {}) {
  if (nativeHls && (/Apple/.test(vendor) || !mse)) return "native";
  if (mse) return "hlsjs";
  return "none";
}

export const HLS_MIME = "application/vnd.apple.mpegurl";
