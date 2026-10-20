// Dashboard: all telemetry, large and clear.
import { h, icon, fmt, kmh, isNum, compassWord, fmtUptime, clear } from "../dom.js";
import { api, errText } from "../api.js";
import { store, on } from "../store.js";
import { segmented, badge, toast, banner } from "../ui.js";
import { openPreview } from "../preview.js";

const REASON = { speed: "by speed", no_fix: "no GPS fix", forced: "forced", startup: "startup" };
const CAM_STATE = { starting: "starting", recording: "recording", stopped: "stopped", error: "error" };
const SRC = { gps: "GPS", mag: "compass", blend: "compass + GPS", none: "no data" };

function throttledText(hex) {
  if (hex == null) return { text: "—", kind: "" };
  const v = parseInt(String(hex), 16);
  if (!Number.isFinite(v)) return { text: String(hex), kind: "" };
  const now = [];
  if (v & 0x1) now.push("under-voltage");
  if (v & 0x2) now.push("frequency capped");
  if (v & 0x4) now.push("throttled");
  if (v & 0x8) now.push("temperature limit");
  const past = [];
  if (v & 0x10000) past.push("under-voltage occurred");
  if (v & 0x20000) past.push("frequency cap occurred");
  if (v & 0x40000) past.push("throttling occurred");
  if (v & 0x80000) past.push("temperature limit occurred");
  if (now.length) return { text: now.join(", "), kind: "bad" };
  if (past.length) return { text: past.join(", "), kind: "warn" };
  return { text: "OK", kind: "ok" };
}

export function mount(root) {
  const refs = {};
  const row = (key, label, unit) => {
    const v = h("span.v", null, "—");
    refs[key] = v;
    return [h("span.k", null, label), v];
  };
  const kv = (...rows) => h(".kv", null, rows.flat());

  // heading arrow
  const arrow = h("g", null, h("polygon", { points: "50,10 58,52 50,46 42,52", fill: "#5dff8a" }), h("polygon", { points: "50,90 58,52 50,46 42,52", fill: "#3a4634" }));
  const mini = h("svg.compass-mini", { viewBox: "0 0 100 100" },
    h("circle", { cx: 50, cy: 50, r: 46, fill: "#0a0c0a", stroke: "#3a4634", "stroke-width": 2 }),
    h("text", { x: 50, y: 20, "text-anchor": "middle", fill: "#ffb62e", "font-size": 11, "font-family": "monospace" }, "N"),
    arrow,
  );

  const bigSpeed = h(".big", null, "—", h("small", null, "km/h"));
  const bigHead = h(".big.amber", null, "—");

  const force = segmented({
    name: "force",
    value: store.settings?.display?.force_mode || "auto",
    options: [{ value: "auto", label: "Auto" }, { value: "map", label: "Map" }, { value: "camera", label: "Camera" }],
    onchange: async (v) => {
      try {
        await api.post("/api/display/force", { mode: v });
        toast("ok", "Screen mode: " + v);
      } catch (e) {
        toast("error", errText(e));
        force.set(store.settings?.display?.force_mode || "auto");
      }
    },
  });

  const errBox = h("div");
  const diskBar = h("i");
  const diskProg = h(".progress.green", null, diskBar);

  root.append(
    h(".page-head", null,
      h("h1", null, "Dashboard"),
      h("button.btn.primary", { type: "button", onclick: () => openPreview() }, icon("screen"), "Screen"),
    ),
    errBox,
    h(".grid.g3", null,
      h(".card", null, h("h2", null, "Speed"),
        bigSpeed,
        h("hr.sep"), kv(row("mps", "m/s"), row("speedSrc", "Screen units")),
      ),
      h(".card", null, h("h2", null, "Heading"),
        h(".bigrow", null, bigHead, mini),
        h("hr.sep"),
        kv(row("hsrc", "Source"), row("hmag", "Compass"), row("hgps", "GPS (COG)"), row("hdecl", "Declination")),
      ),
      h(".card", null, h("h2", null, "GPS"),
        kv(row("fix", "Fix"), row("sats", "Satellites (used / visible)"), row("hdop", "HDOP"), row("age", "Data age"), row("gtime", "GPS time (UTC)")),
        h("hr.sep"),
        kv(row("lat", "Latitude"), row("lon", "Longitude"), row("alt", "Altitude")),
      ),
      h(".card", null, h("h2", null, "Camera / recording"),
        kv(row("cstate", "State"), row("rec", "Recording"), row("cfps", "Frames/s"), row("cbr", "Bitrate"), row("cmode", "Sensor mode → output"), row("cseg", "Current segment"), row("cdrop", "Dropped frames"), row("cerr", "Error")),
      ),
      h(".card", null, h("h2", null, "Disk"),
        h("div", { style: { marginBottom: "10px" } }, diskProg),
        kv(row("dfree", "Free"), row("dtotal", "Total"), row("drec", "Recordings"), row("dtiles", "Tile cache"), row("cnt", "Recording files")),
      ),
      h(".card", null, h("h2", null, "System"),
        kv(row("temp", "CPU temperature"), row("thr", "Power / throttling"), row("load", "Load (1 min)"), row("mem", "Memory"), row("up", "Uptime"), row("ip", "IP"), row("wifi", "Wi‑Fi"), row("inet", "Internet (tiles)"), row("tsync", "Time synchronized")),
      ),
    ),
    h(".card", null,
      h(".card-head", null, h("h2", null, "Device screen")),
      h("div", { style: { display: "flex", gap: "24px", alignItems: "center", flexWrap: "wrap" } },
        h(".kv", { style: { minWidth: "300px" } }, row("dmode", "Showing now"), row("dreason", "Reason"), row("dzoom", "Target zoom"), row("dori", "Map orientation")),
        h("div", null, h("div.dim", { style: { marginBottom: "6px", fontSize: "12.5px" } }, "Forced mode"), force),
      ),
    ),
  );

  const set = (k, txt, cls) => {
    const el = refs[k];
    if (!el) return;
    el.textContent = txt;
    el.className = "v" + (cls ? " " + cls : "");
  };

  function update() {
    const t = store.telemetry;
    if (!t) return;
    const g = t.gps || {}, hd = t.heading || {}, cam = t.camera || {}, d = t.disk || {}, sys = t.system || {}, dp = t.display || {};
    bigSpeed.firstChild.textContent = isNum(g.speed_mps) ? fmt(kmh(g.speed_mps), 0) : "—";
    set("mps", isNum(g.speed_mps) ? fmt(g.speed_mps, 1) + " m/s" : "—");
    set("speedSrc", store.settings?.display?.speed_units === "mps" ? "m/s" : "km/h");
    bigHead.textContent = isNum(hd.deg) ? `${hd.deg.toFixed(0)}° ${compassWord(hd.deg)}` : "—";
    arrow.setAttribute("transform", `rotate(${isNum(hd.deg) ? hd.deg : 0} 50 50)`);
    arrow.style.opacity = isNum(hd.deg) ? 1 : 0.2;
    set("hsrc", SRC[hd.source] || "—");
    set("hmag", isNum(hd.mag_deg) ? hd.mag_deg.toFixed(1) + "°" : "—");
    set("hgps", isNum(hd.gps_course_deg) ? hd.gps_course_deg.toFixed(1) + "°" : "—");
    set("hdecl", isNum(hd.declination_deg) ? hd.declination_deg.toFixed(1) + "°" : "—");

    set("fix", g.fix === "3d" ? "3D" : g.fix === "2d" ? "2D" : "none", g.fix === "3d" ? "green" : g.fix === "2d" ? "amber" : "red");
    set("sats", `${g.sats_used ?? 0} / ${g.sats_visible ?? 0}`);
    set("hdop", fmt(g.hdop, 2), isNum(g.hdop) && g.hdop > 3 ? "amber" : "");
    set("age", isNum(g.age_s) ? g.age_s.toFixed(2) + " s" : "—", isNum(g.age_s) && g.age_s > 2 ? "red" : "");
    set("gtime", g.time_utc ? g.time_utc.replace("T", " ").replace(/\.\d+/, "").replace(/[+-]\d\d:\d\d$|Z$/, "") : "—");
    set("lat", isNum(g.lat) ? g.lat.toFixed(6) + "°" : "—");
    set("lon", isNum(g.lon) ? g.lon.toFixed(6) + "°" : "—");
    set("alt", isNum(g.alt_m) ? g.alt_m.toFixed(0) + " m" : "—");

    set("cstate", CAM_STATE[cam.state] || cam.state || "—", cam.state === "error" ? "red" : cam.state === "recording" ? "green" : "amber");
    set("rec", cam.recording ? "● REC" : "○ no", cam.recording ? "red" : "");
    set("cfps", fmt(cam.fps_actual, 1));
    set("cbr", isNum(cam.bitrate_kbps) ? Math.round(cam.bitrate_kbps) + " kbit/s" : "—");
    set("cmode", `${cam.sensor_mode || "—"} → ${cam.output_size || "—"}`);
    set("cseg", cam.current_segment ? cam.current_segment.split("/").pop() : "—");
    set("cdrop", String(cam.frames_dropped ?? 0), cam.frames_dropped > 0 ? "amber" : "");
    set("cerr", cam.error || "—", cam.error ? "red" : "");

    const total = d.total_gb, free = d.free_gb ?? cam.free_gb;
    set("dfree", isNum(free) ? free.toFixed(1) + " GB" : "—", isNum(free) && free < 2 ? "red" : "");
    set("dtotal", isNum(total) ? total.toFixed(1) + " GB" : "—");
    set("drec", isNum(d.recordings_gb ?? cam.recordings_gb) ? (d.recordings_gb ?? cam.recordings_gb).toFixed(2) + " GB" : "—");
    set("dtiles", isNum(d.tiles_mb) ? d.tiles_mb.toFixed(0) + " MB" : "—");
    set("cnt", String(cam.recordings_count ?? "—"));
    diskBar.style.width = isNum(total) && isNum(free) && total > 0 ? Math.max(0, Math.min(100, ((total - free) / total) * 100)) + "%" : "0%";

    set("temp", isNum(sys.cpu_temp_c) ? sys.cpu_temp_c.toFixed(1) + " °C" : "—", isNum(sys.cpu_temp_c) ? (sys.cpu_temp_c >= 80 ? "red" : sys.cpu_temp_c >= 70 ? "amber" : "") : "");
    const th = throttledText(sys.throttled);
    set("thr", th.text, th.kind === "bad" ? "red" : th.kind === "warn" ? "amber" : th.kind === "ok" ? "green" : "");
    set("load", fmt(sys.load_1m, 2));
    set("mem", isNum(sys.mem_used_pct) ? sys.mem_used_pct.toFixed(0) + " %" : "—");
    set("up", fmtUptime(sys.uptime_s));
    set("ip", sys.ip || "—");
    set("wifi", sys.wifi_ssid ? `${sys.wifi_ssid}${isNum(sys.wifi_signal_pct) ? " · " + sys.wifi_signal_pct + "%" : ""}` : "—");
    set("inet", sys.internet === true ? "yes" : sys.internet === false ? "no" : "—", sys.internet === false ? "amber" : sys.internet ? "green" : "");
    set("tsync", sys.time_synced === true ? "yes" : sys.time_synced === false ? "no" : "—", sys.time_synced === false ? "amber" : "");

    set("dmode", dp.mode === "map" ? "map" : dp.mode === "camera" ? "camera" : "—");
    set("dreason", REASON[dp.reason] || "—");
    set("dzoom", isNum(dp.target_zoom) ? dp.target_zoom.toFixed(1) : "—");
    set("dori", dp.orientation === "north_up" ? "north up" : dp.orientation === "heading_up" ? "heading up" : "—");

    const errs = [...(t.errors || [])];
    clear(errBox);
    if (errs.length) errBox.append(banner("warn", [h("b", null, "Sensor / system problems:"), h("ul", null, errs.map((e) => h("li", null, e)))]));
  }

  const offT = on("telemetry", update);
  const offS = on("settings", (s) => force.set(s?.display?.force_mode || "auto"));
  update();
  return () => { offT(); offS(); };
}
