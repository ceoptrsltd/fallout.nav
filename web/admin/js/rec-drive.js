// Driving in a recording (pure helpers, tests/admin/rec-drive.test.mjs): its GPS track and impacts while driving.
// fnav-core writes ~1 GPS point/s next to the current segment of each camera (<segment>.ts.gps, docs/contracts/camera.md
// «Трек GPS записи»); the player shows the speed and the place at the playback moment: the recording's start (start_ts —
// the true UTC start) + currentTime, matched to the points by unix time.
import { appUrl, encPath } from "./api.js";
import { recCamera, recStartMs } from "./rec-play.js";

export const GPS_MAX_GAP_S = 3; // no point closer than this to the playback moment — "no GPS here"

/** API path of the track (for api.get): GET → {path, samples: [{t, lat, lon, alt, speed, course, fix}]}. */
export function gpsPath(path) {
  return "/api/recordings/gps/" + encPath(path);
}

/** The track as a GPX file to download (speed and course in Garmin's TrackPointExtension), under the app root. */
export function gpxUrl(path, root) {
  return appUrl(gpsPath(path) + "?format=gpx", root);
}

/** Name of the downloaded GPX: like the recording's ("rear-" in front for the rear camera). */
export function gpxName(r) {
  const name = String((r && r.path) || "").split("/").pop().replace(/\.ts$/, "") + ".gpx";
  return recCamera(r) === "rear" ? "rear-" + name : name;
}

const num = (v) => typeof v === "number" && Number.isFinite(v);

/** Valid points by time (the API sorts already; an odd answer must not break the player). */
export function cleanTrack(samples) {
  const out = (Array.isArray(samples) ? samples : [])
    .filter((s) => s && num(s.t) && num(s.lat) && num(s.lon) && Math.abs(s.lat) <= 90 && Math.abs(s.lon) <= 180);
  return out.sort((a, b) => a.t - b.t);
}

/** Unix seconds at playback position currentTime (s) of recording rec; null — its start is not known (nosync). */
export function playUnix(rec, currentTime) {
  const t0 = recStartMs(rec);
  if (t0 === null || !num(t0) || !num(currentTime)) return null;
  return t0 / 1000 + Math.max(0, currentTime);
}

/** Shortest-way angle interpolation, degrees 0…360. */
export function lerpAngle(a, b, k) {
  const d = ((((b - a) % 360) + 540) % 360) - 180;
  return (((a + d * k) % 360) + 360) % 360;
}

const lerpNum = (a, b, k, near) => (num(a) && num(b) ? a + (b - a) * k : near);

/**
 * The track (cleanTrack) at unix time t: between two points at most 2·maxGap apart — interpolated (position, speed,
 * course the short way round); otherwise the nearest point if it is within maxGap; null — no point near (no fix
 * there, before the track starts or after it ends).
 */
export function sampleAt(track, t, maxGap = GPS_MAX_GAP_S) {
  if (!Array.isArray(track) || !track.length || !num(t)) return null;
  const first = track[0], last = track[track.length - 1];
  if (t <= first.t) return first.t - t <= maxGap ? { ...first } : null;
  if (t >= last.t) return t - last.t <= maxGap ? { ...last } : null;
  let lo = 0, hi = track.length - 1; // track[lo].t < t < track[hi].t
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (track[mid].t <= t) lo = mid; else hi = mid;
  }
  const a = track[lo], b = track[hi];
  const near = t - a.t <= b.t - t ? a : b;
  if (b.t - a.t > 2 * maxGap) return Math.abs(near.t - t) <= maxGap ? { ...near } : null;
  const k = (t - a.t) / (b.t - a.t);
  return {
    t,
    lat: a.lat + (b.lat - a.lat) * k,
    lon: a.lon + (b.lon - a.lon) * k,
    alt: lerpNum(a.alt, b.alt, k, near.alt ?? null),
    speed: lerpNum(a.speed, b.speed, k, near.speed ?? null),
    course: num(a.course) && num(b.course) ? lerpAngle(a.course, b.course, k) : near.course ?? null,
    fix: near.fix,
  };
}

/** "52 km/h" from m/s; "— km/h" — not known. */
export function fmtKmh(mps) {
  return num(mps) ? `${Math.round(Math.max(0, mps) * 3.6)} km/h` : "— km/h";
}

/** "52.22977, 21.01178" (5 decimals ≈ 1 m). */
export function fmtLatLon(lat, lon) {
  return num(lat) && num(lon) ? `${lat.toFixed(5)}, ${lon.toFixed(5)}` : "—";
}

/** [[lat, lon], …] for a map polyline. */
export function trackLatLngs(track) {
  return (Array.isArray(track) ? track : []).map((s) => [s.lat, s.lon]);
}

// ── impacts while driving (fnav/core/impact.py): marks of kind "impact" in GET api/recordings → marks ──

/** The recording's marks split: impacts while driving and the rest (movement, loud sounds — without an alarm). */
export function splitMarks(marks) {
  const all = Array.isArray(marks) ? marks : [];
  return { impacts: all.filter((m) => m && m.kind === "impact"), others: all.filter((m) => m && m.kind !== "impact") };
}

/** "Impact 1.8 g while driving" (peak_g — the strength after the high-pass filter, at least the sensor's limit). */
export function impactText(m) {
  const g = m && num(m.peak_g) ? ` ${m.peak_g.toFixed(1)} g` : "";
  return `Impact${g} while driving`;
}
