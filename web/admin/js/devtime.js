// The device's clock (pure helpers, tests/admin/devtime.test.mjs). The device names its recordings by ITS local time
// ("2026-11-02_13-40-10.ts") and its time zone follows the van (fnav/core/timezone.py), while the phone may be in
// another zone: abroad, or looking remotely from home. The zone (IANA) comes from api/session and telemetry
// (system.timezone); unknown (an older device, the dev mock on Windows) — the browser's own zone, as before.

let zone = null;

const fmts = new Map(); // zone → Intl.DateTimeFormat (or null: the browser does not know the zone)
function partsFmt(tz) {
  let f = fmts.get(tz);
  if (f === undefined) {
    try {
      f = new Intl.DateTimeFormat("en-US", {
        timeZone: tz, hourCycle: "h23", year: "numeric", month: "numeric", day: "numeric",
        hour: "numeric", minute: "numeric", second: "numeric",
      });
    } catch {
      f = null;
    }
    fmts.set(tz, f);
  }
  return f;
}

/** A zone name the browser knows ("Europe/Warsaw"). */
export function validZone(tz) {
  return typeof tz === "string" && tz !== "" && partsFmt(tz) !== null;
}

/** Remember the device's zone; anything else (null, "", unknown) → null: the browser's zone. Returns what is set. */
export function setDeviceZone(tz) {
  zone = validZone(tz) ? tz : null;
  return zone;
}

export function deviceZone() {
  return zone;
}

/** UTC offset (ms) of zone tz at the moment ms (unix ms); null — the browser does not know the zone. */
export function zoneOffsetMs(tz, ms) {
  const f = partsFmt(tz);
  if (!f) return null;
  const p = {};
  for (const x of f.formatToParts(new Date(ms))) p[x.type] = x.value;
  const wall = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute, +p.second);
  return wall - Math.floor(ms / 1000) * 1000;
}

/**
 * Wall-clock time in zone tz (the device's by default) → unix ms. A time that happens twice when the clocks go back —
 * the first one (as datetime.timestamp() on the device); a time skipped when they go forward — with the offset before
 * the change. tz null or unknown — the browser's own zone.
 */
export function wallToMs(y, mo, d, hh = 0, mi = 0, ss = 0, tz = zone) {
  if (!validZone(tz)) return new Date(y, mo - 1, d, hh, mi, ss).getTime();
  const wall = Date.UTC(y, mo - 1, d, hh, mi, ss);
  const HALF_DAY = 12 * 3600e3; // the offset before and after a change near this time
  const offs = [zoneOffsetMs(tz, wall - HALF_DAY), zoneOffsetMs(tz, wall + HALF_DAY)];
  const fits = offs.map((o) => wall - o).filter((t) => zoneOffsetMs(tz, t) === wall - t);
  return fits.length ? Math.min(...fits) : wall - offs[0];
}

/** "22:05:27" — a moment (unix s) on the device's clock (the browser's without a zone); "" — not a number. */
export function deviceClock(ts, tz = zone) {
  if (!Number.isFinite(ts)) return "";
  const opts = { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" };
  return new Date(ts * 1000).toLocaleTimeString("en-GB", validZone(tz) ? { ...opts, timeZone: tz } : opts);
}

/** "UTC+01:00", "UTC−03:30", "UTC" — an offset in minutes (SystemStatus.utc_offset_min); "" — not known. */
export function fmtUtcOffset(min) {
  if (!Number.isFinite(min)) return "";
  if (min === 0) return "UTC";
  const a = Math.abs(Math.round(min));
  return `UTC${min < 0 ? "−" : "+"}${String(Math.floor(a / 60)).padStart(2, "0")}:${String(a % 60).padStart(2, "0")}`;
}

/** The device's zone for the System page: "Europe/Lisbon (UTC)"; only the offset, or "—". */
export function zoneText(sys) {
  const s = sys || {};
  const off = fmtUtcOffset(s.utc_offset_min);
  if (typeof s.timezone === "string" && s.timezone) return off ? `${s.timezone} (${off})` : s.timezone;
  return off || "—";
}
