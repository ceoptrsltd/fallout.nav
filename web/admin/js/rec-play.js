// Recordings playback: playlist URL, the choice of player, which camera (pure helpers, tests/admin/recordings.test.mjs).
import { appUrl, encPath } from "./api.js";
import { wallToMs } from "./devtime.js";

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

// ── which camera (GET /api/recordings → camera; the rear's files are under "rear/") ──

/** "front" / "rear" of a recording: the list says it; an older device — from the path. */
export function recCamera(r) {
  if (r && (r.camera === "front" || r.camera === "rear")) return r.camera;
  return /^rear\//.test(String((r && r.path) || "")) ? "rear" : "front";
}

/** Recordings of one camera; "all" (or anything else) — all of them. */
export function filterRecs(list, cam) {
  const l = Array.isArray(list) ? list : [];
  return cam === "front" || cam === "rear" ? l.filter((r) => recCamera(r) === cam) : l;
}

/** Name of a downloaded file: both cameras name segments by their start time, so the rear's get "rear-" in front. */
export function downloadName(r) {
  const name = String((r && r.path) || "").split("/").pop();
  return recCamera(r) === "rear" ? "rear-" + name : name;
}

/** Player title: "Rear camera · 2026-10-28 10:00:00" (the file name when the time is not known). */
export function recTitle(r) {
  const cam = recCamera(r) === "rear" ? "Rear camera" : "Front camera";
  const m = String((r && r.start) || "").match(/^(\d{4}-\d\d-\d\d)T(\d\d:\d\d:\d\d)/);
  return `${cam} · ${m ? `${m[1]} ${m[2]}` : String((r && r.path) || "").split("/").pop()}`;
}

// ── alarm events inside a recording (GET /api/recordings → events[].offset_s) ──
export const LEAD_S = 5; // a jump to an event starts this much earlier: what led to it is seen too

/** Start (unix ms) of a segment from its file name ("…/2026-10-27_22-05-00.ts"): the DEVICE's local time, so it is
 *  read in the device's zone (devtime.js), not the phone's; tz — another zone (null: the browser's). null — nosync. */
export function segmentStartMs(path, tz) {
  const m = String(path || "").match(/(\d{4})-(\d\d)-(\d\d)_(\d\d)-(\d\d)-(\d\d)\.ts$/);
  if (!m) return null;
  const [y, mo, d, hh, mi, ss] = m.slice(1).map(Number);
  return wallToMs(y, mo, d, hh, mi, ss, tz);
}

/** Start (unix ms) of a recording (GET api/recordings item): start_ts — the true start the recorder kept, right across
 *  time zone changes; otherwise its local "start" (or name) in the device's zone; null — not known (nosync). */
export function recStartMs(r, tz) {
  if (r && Number.isFinite(r.start_ts)) return r.start_ts * 1000;
  const m = String((r && r.start) || "").match(/^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)/);
  if (!m) return segmentStartMs(r && r.path, tz);
  const [y, mo, d, hh, mi, ss] = m.slice(1).map(Number);
  return wallToMs(y, mo, d, hh, mi, ss, tz);
}

/** Link that opens the recording in the player at the moment of an alarm event (ts — unix seconds): t — the offset by
 *  the file name, at — the moment itself (the player prefers it with the recording's start_ts: a recording made
 *  before the device changed its time zone is named by the old one). */
export function recLink(path, ts) {
  const t0 = segmentStartMs(path);
  const off = t0 !== null && Number.isFinite(ts) ? Math.max(0, Math.round(ts - t0 / 1000)) : null;
  return "#/recordings?play=" + encodeURIComponent(path) + (off !== null ? `&t=${off}&at=${Math.round(ts)}` : "");
}

/** {play, t, at} from "#/recordings?play=…&t=…&at=…"; null — no player link. */
export function parseRecLink(hash) {
  const q = String(hash || "").split("?")[1];
  if (!q) return null;
  const p = new URLSearchParams(q);
  const play = p.get("play");
  if (!play) return null;
  const num = (k) => {
    const raw = p.get(k);
    const v = raw === null || raw === "" ? NaN : Number(raw);
    return Number.isFinite(v) && v >= 0 ? v : null;
  };
  return { play, t: num("t"), at: num("at") };
}

/** Where the player starts for a link (s into the recording rec): the moment (at) − the recording's start when both are
 *  known — right even for a recording named in the zone before a change; otherwise the link's own t. */
export function linkOffset(link, rec) {
  if (!link) return null;
  const t0 = recStartMs(rec);
  if (Number.isFinite(link.at) && t0 !== null) return Math.max(0, Math.round(link.at - t0 / 1000));
  return link.t;
}

/** Where playback starts for an event at offset (s). */
export function jumpTo(offset) {
  return Math.max(0, (Number(offset) || 0) - LEAD_S);
}

/** Marker position 0…1 on a timeline of the given duration (s); null — duration unknown. */
export function markPos(offset, duration) {
  if (!(Number.isFinite(duration) && duration > 0)) return null;
  return Math.min(1, Math.max(0, (Number(offset) || 0) / duration));
}
