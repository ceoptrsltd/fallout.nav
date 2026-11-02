// GPS page: pure helpers (no DOM). Tests: tests/admin/gps.test.mjs.
// Speed accuracy: Telemetry.gps.eps_mps — gpsd's TPV "eps", taken from the u-blox NAV-PVT sAcc (fnav/sensors/m8n.py
// turns NAV-PVT on when the module is reconfigured). null — not known: NAV-PVT off, no fix. A core without the field
// (older device behind the remote web copy) — undefined: no accuracy, no hint.

// gpsd reads this port (DEVICES in pi/gpsd/gpsd.default); settings.gps.device is only the UBX configurator's port
export const GPSD_DEVICE = "/dev/gps0";

const num = (v) => typeof v === "number" && Number.isFinite(v);

/** "±0.25 m/s" (two decimals under 1 m/s, one above); "" when not known. */
export function speedAccText(eps) {
  if (!num(eps) || eps < 0) return "";
  return `±${eps < 1 ? eps.toFixed(2) : eps.toFixed(1)} m/s`;
}

/** The page head line: "fix 3D · HDOP 0.91 · 12.3 m/s ±0.25 m/s". */
export function fixLine(g) {
  if (!g) return "";
  const fix = g.fix === "2d" || g.fix === "3d" ? g.fix.toUpperCase() : "none";
  const hdop = num(g.hdop) ? g.hdop.toFixed(2) : "—";
  const speed = num(g.speed_mps) ? `${g.speed_mps.toFixed(1)} m/s` : "— m/s";
  const acc = speedAccText(g.eps_mps);
  return `fix ${fix} · HDOP ${hdop} · ${speed}${acc ? " " + acc : ""}`;
}

/** A fix without the speed accuracy from a core that knows the field: the module does not send NAV-PVT yet. */
export function needsPvt(g) {
  return !!g && (g.fix === "2d" || g.fix === "3d") && "eps_mps" in g && g.eps_mps === null;
}

/** The configurator's port differs from gpsd's: "Reconfigure" would talk to another port than the one gpsd reads. */
export function deviceMismatch(device) {
  return typeof device === "string" && device !== "" && device !== GPSD_DEVICE;
}
