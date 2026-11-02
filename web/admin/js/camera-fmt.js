// Camera page: pure helpers (no DOM) for the front / rear switch, the rear camera's status and settings and the
// car screen control (docs/contracts/camera.md «Задняя камера», api.md «Камера»). Tests: tests/admin/camera.test.mjs.

const num = (v) => typeof v === "number" && Number.isFinite(v);
const fps1 = (v) => String(Math.round(v * 10) / 10);

export const CAMS = ["front", "rear"];

/** "Front" / "Rear". */
export function camName(cam) {
  return cam === "rear" ? "Rear" : "Front";
}

/** Camera tab from the page link: "#/camera?cam=rear" → "rear"; null — not in the link. */
export function camFromHash(hash) {
  const q = String(hash || "").split("?")[1];
  if (!q) return null;
  const c = new URLSearchParams(q).get("cam");
  return CAMS.includes(c) ? c : null;
}

const STATES = {
  recording: { text: "recording", kind: "ok", rec: true },
  live: { text: "live, not recording", kind: "ok", rec: false },
  starting: { text: "starting", kind: "warn", rec: false },
  stopped: { text: "stopped", kind: "warn", rec: false },
  error: { text: "error", kind: "bad", rec: false },
  disabled: { text: "off", kind: "", rec: false },
};

/** Camera state for the status card and the tab dot: {text, kind, rec}; "live" — works, recording is off. */
export function camState(c) {
  const st = c && typeof c === "object" ? c.state : null;
  if (!st) return { text: "—", kind: "", rec: false };
  const s = STATES[st] || { text: String(st), kind: "warn", rec: false };
  return { ...s, rec: s.rec && c.recording !== false };
}

/** Dot of a camera tab: "rec" (recording), "ok", "warn", "bad" or "off". */
export function camDot(c) {
  const s = camState(c);
  if (s.rec) return "rec";
  return s.kind || "off";
}

const CODECS = { h264: "H.264", hevc: "H.265", h265: "H.265", mjpeg: "MJPEG" };

/** Codec the camera sends: {text, kind}; only H.264 is recorded as is (camera.md: no transcoding). */
export function codecInfo(codec) {
  if (!codec) return { text: "—", kind: "" };
  const k = String(codec).toLowerCase();
  const name = CODECS[k] || String(codec).toUpperCase();
  return k === "h264" ? { text: name, kind: "" } : { text: `${name} — set the camera to H.264`, kind: "bad" };
}

/** Values of the rear camera's status card (CameraStatus from telemetry camera_rear). */
export function rearStatus(c) {
  const s = c && typeof c === "object" ? c : {};
  const dropped = num(s.frames_dropped) ? s.frames_dropped : 0;
  return {
    state: camState(s),
    error: s.error ? { text: String(s.error), kind: "bad" } : { text: "—", kind: "" },
    codec: codecInfo(s.codec),
    size: s.output_size ? String(s.output_size).replace("x", "×") : "—",
    streamFps: num(s.stream_fps) ? fps1(s.stream_fps) : "—",
    fps: num(s.fps_actual) ? fps1(s.fps_actual) : "—",
    bitrate: num(s.bitrate_kbps) ? `${Math.round(s.bitrate_kbps)} kbit/s` : "—",
    dropped: { text: String(dropped), kind: dropped > 0 ? "warn" : "" },
    segment: s.current_segment ? String(s.current_segment).split("/").pop() : "—",
  };
}

const gb1 = (v) => String(Math.round(v * 10) / 10);

/**
 * Protected recordings for the status card: {text, kind}, e.g. "3.2 GB · unprotected ~95 min" (CameraStatus
 * protected_gb / loop_minutes, camera.md «Минимум петли»); amber while they squeeze the unprotected ones (over_limit).
 */
export function protectedInfo(c) {
  const s = c && typeof c === "object" ? c : {};
  if (!num(s.protected_gb)) return { text: "—", kind: "" };
  const loop = num(s.loop_minutes) ? ` · unprotected ~${Math.round(s.loop_minutes)} min` : "";
  return { text: `${gb1(s.protected_gb)} GB${loop}`, kind: s.over_limit ? "warn" : "" };
}

/**
 * Warning text while protected recordings leave the unprotected ones only their minimum (CameraStatus.over_limit,
 * camera.md «Минимум петли»), or null. front / rear: the cameras' CameraStatus — the front cleans both cameras'
 * recordings, so its numbers come first; limitGb: settings.storage.max_recordings_gb.
 */
export function storageWarning(front, rear, limitGb) {
  const c = [front, rear].find((s) => s && typeof s === "object" && s.over_limit);
  if (!c) return null;
  const take = num(c.protected_gb) ? `take ${gb1(c.protected_gb)} GB of` : "fill";
  const limit = num(limitGb) ? `the ${gb1(limitGb)} GB recordings limit` : "the recordings limit";
  const kept = num(c.loop_minutes) ? `~${Math.round(c.loop_minutes)} min` : "hour or less";
  return `Protected recordings ${take} ${limit}: only the newest ${kept} of unprotected footage is kept, older driving is deleted. Unprotect recordings you no longer need.`;
}

/** What the car screen shows (telemetry display): {camera, text}; the rear view overrides the map / camera logic. */
export function screenInfo(d) {
  if (!d || typeof d !== "object") return { camera: null, text: "—" };
  if (d.camera === "rear") return { camera: "rear", text: d.rear_mirror === false ? "Rear camera" : "Rear camera, mirrored" };
  return { camera: "front", text: d.mode === "map" ? "Map" : "Front camera" };
}

/**
 * Problem with an RTSP address for its field, or null. required — the camera is (being) turned on.
 * The login must not be in the address: settings are readable by the admin, the password lives in secrets.env.
 */
export function rtspProblem(url, { required = false } = {}) {
  const raw = String(url ?? "");
  if (!raw.trim()) return required ? "Enter the camera's RTSP address" : null;
  if (/\s/.test(raw)) return "No spaces in the address";
  const m = raw.match(/^rtsps?:\/\/([^/?#]*)/i);
  if (!m) return "The address starts with rtsp://";
  if (m[1].includes("@")) return "Leave the login out of the address: enter it in Username and Password";
  if (!m[1] || m[1].startsWith(":")) return "The camera's IP address is missing";
  return null;
}

/**
 * Status of the write-only password field: {text, kind}. set: password_set reported by the camera or the last save
 * (null — not known); value: what is being edited (null — untouched, "" — remove it); camOn: the rear camera is on
 * (only then it reports password_set).
 */
export function passwordInfo(set, value, camOn = false) {
  if (value === "") return { text: "Removed when you press Save", kind: "warn" };
  if (typeof value === "string" && value) return { text: "New password: saved when you press Save", kind: "warn" };
  if (set === true) return { text: "Set: kept on the device, never shown", kind: "ok" };
  if (set === false) return { text: "Not set", kind: "" };
  return { text: camOn ? "Not reported by the camera yet" : "Not known while the camera is off", kind: "" };
}

/**
 * Takes the rear password out of a settings patch (it is not a setting: POST api/camera/rear/credentials puts it in
 * secrets.env). Changes the patch in place; returns the password ("" — remove) or undefined — unchanged.
 */
export function takePassword(patch) {
  const rc = patch && patch.rear_camera;
  if (!rc || typeof rc !== "object" || !("password" in rc)) return undefined;
  const pw = rc.password;
  delete rc.password;
  if (!Object.keys(rc).length) delete patch.rear_camera;
  return typeof pw === "string" ? pw : undefined;
}

/**
 * Confirmation before saving rear camera settings ({title, text, okText}) or null. rc: the rear_camera part of the
 * patch; cur: settings.rear_camera now; pwChanged: the password changes too. Everything but the mirror and the
 * fps/bitrate estimates reconnects the camera (camera.md): only worth asking while it records.
 */
const REAR_ON_THE_FLY = new Set(["mirror", "enabled", "fps", "bitrate_kbps"]);
export function rearConfirm(rc, cur, pwChanged = false) {
  const c = cur || {};
  const p = rc || {};
  if (!c.enabled) return null; // off: nothing to interrupt
  if (p.enabled === false) {
    return { title: "Turn the rear camera off?", okText: "Turn off",
      text: "Its recording and live view stop until you turn it on again. Recordings already made stay." };
  }
  const reconnect = pwChanged || Object.keys(p).some((k) => !REAR_ON_THE_FLY.has(k));
  if (!reconnect || c.record === false) return null;
  return { title: "Rear camera restart", okText: "Apply and reconnect",
    text: "Applying these settings reconnects the rear camera: its current segment is closed and there will be a gap of a few seconds in its footage. Continue?" };
}

/** Snapshot file name: "rear-2026-10-28_10-00-05.jpg" (local time). */
export function snapName(cam, date) {
  const d = date instanceof Date ? date : new Date(date);
  const p = (x) => String(x).padStart(2, "0");
  const t = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}_${p(d.getHours())}-${p(d.getMinutes())}-${p(d.getSeconds())}`;
  return `${cam === "rear" ? "rear" : "front"}-${t}.jpg`;
}
