// GPS: satellite table + skyplot, raw NMEA, module reconfiguration, gps.* settings
import { h, icon, clear, fmt, isNum } from "../dom.js";
import { api, errText } from "../api.js";
import { SettingsForm, section } from "../form.js";
import { store, on, subscribeTopic } from "../store.js";
import { toast, confirmDialog, poll, banner, switchEl } from "../ui.js";

const GN = {
  gps: { name: "GPS", color: "#5dff8a" },
  glonass: { name: "GLONASS", color: "#ffb62e" },
  galileo: { name: "Galileo", color: "#6cb8ff" },
  beidou: { name: "BeiDou", color: "#ff8a7a" },
  sbas: { name: "SBAS", color: "#b9a3ff" },
  qzss: { name: "QZSS", color: "#9aa" },
  other: { name: "Other", color: "#8d9987" },
};

function skyplot(sats) {
  const S = 360, C = S / 2, R = 160;
  const svg = h("svg", { viewBox: `0 0 ${S} ${S}`, style: { width: "100%", maxWidth: "380px" } });
  svg.append(h("circle", { cx: C, cy: C, r: R + 14, fill: "#060806", stroke: "#2b3427" }));
  for (const [el, lab] of [[0, "0°"], [30, "30°"], [60, "60°"]]) {
    const r = R * (1 - el / 90);
    svg.append(h("circle", { cx: C, cy: C, r, fill: "none", stroke: "#2b3427", "stroke-dasharray": el ? "3 4" : "" }));
    if (el) svg.append(h("text", { x: C + 3, y: C - r + 11, fill: "#5d6858", "font-size": 9, "font-family": "monospace" }, lab));
  }
  svg.append(h("line", { x1: C - R - 4, y1: C, x2: C + R + 4, y2: C, stroke: "#2b3427" }), h("line", { x1: C, y1: C - R - 4, x2: C, y2: C + R + 4, stroke: "#2b3427" }));
  for (const [t, x, y] of [["N", C, 12], ["E", S - 7, C + 4], ["S", C, S - 4], ["W", 8, C + 4]]) {
    svg.append(h("text", { x, y, "text-anchor": "middle", fill: "#ffb62e", "font-size": 12, "font-family": "monospace" }, t));
  }
  for (const s of sats) {
    if (!isNum(s.el) || !isNum(s.az)) continue;
    const r = R * (1 - Math.max(0, Math.min(90, s.el)) / 90);
    const a = (s.az * Math.PI) / 180;
    const x = C + r * Math.sin(a), y = C - r * Math.cos(a);
    const col = (GN[s.gnss] || GN.other).color;
    const g = h("g");
    g.append(
      h("circle", { cx: x, cy: y, r: 10, fill: s.used ? col : "#0a0c0a", "fill-opacity": s.used ? 0.9 : 1, stroke: col, "stroke-width": 1.5, opacity: isNum(s.snr) && s.snr > 0 ? 1 : 0.45 }),
      h("text", { x, y: y + 3.5, "text-anchor": "middle", "font-size": 9.5, "font-family": "monospace", fill: s.used ? "#0a0c0a" : col, "font-weight": "bold" }, String(s.prn)),
    );
    svg.append(g);
  }
  return svg;
}

export function mount(root) {
  const plot = h("div", { style: { display: "flex", justifyContent: "center" } });
  const legend = h("div", { style: { display: "flex", gap: "12px", flexWrap: "wrap", marginTop: "8px", fontSize: "12.5px" } },
    Object.values(GN).slice(0, 5).map((g) => h("span", null, h("span", { style: { color: g.color } }, "● "), g.name)),
    h("span.dim", null, "● filled = used in the fix"));
  const tbody = h("tbody");
  const sum = h("span.dim.mono");

  function renderSats(list) {
    const sorted = [...list].sort((a, b) => (b.used - a.used) || ((b.snr || 0) - (a.snr || 0)));
    clear(plot).append(skyplot(list));
    clear(tbody);
    for (const s of sorted) {
      const g = GN[s.gnss] || GN.other;
      const snr = isNum(s.snr) ? s.snr : null;
      tbody.append(h("tr", null,
        h("td", { style: { color: g.color } }, g.name),
        h("td.num", null, String(s.prn)),
        h("td.num", null, fmt(s.el, 0)),
        h("td.num", null, fmt(s.az, 0)),
        h("td", { style: { width: "150px" } }, h("div", { style: { display: "flex", alignItems: "center", gap: "8px" } },
          h(".progress" + (snr >= 30 ? ".green" : ""), { style: { flex: 1, height: "8px" } }, h("i", { style: { width: Math.min(100, ((snr || 0) / 50) * 100) + "%" } })),
          h("span.mono", { style: { width: "26px", textAlign: "right" } }, snr === null ? "—" : snr.toFixed(0)))),
        h("td", null, s.used ? h("span.badge.ok", null, "yes") : h("span.badge", null, "no")),
      ));
    }
    if (!list.length) tbody.append(h("tr", null, h("td", { colspan: 6, class: "dim" }, "No satellite data")));
    sum.textContent = `visible: ${list.length} · used: ${list.filter((s) => s.used).length}`;
  }
  async function loadSats() {
    try { renderSats(await api.get("/api/gps/satellites")); } catch (e) { sum.textContent = errText(e); }
  }
  const stopSats = poll(loadSats, 1000);

  const fixInfo = h("span.mono");
  function renderFix() {
    const g = store.telemetry?.gps;
    if (!g) return;
    fixInfo.textContent = `fix ${g.fix === "none" ? "none" : g.fix.toUpperCase()} · HDOP ${fmt(g.hdop, 2)} · ${fmt(g.speed_mps, 1)} m/s`;
    fixInfo.className = "mono " + (g.fix === "3d" ? "green" : g.fix === "2d" ? "amber" : "red");
  }

  // ── NMEA ──
  const log = h(".log", { style: { height: "260px" } });
  let lines = [];
  let paused = false;
  let filter = "";
  const flt = h("input", { type: "text", placeholder: "Filter (e.g. GGA)", style: { width: "180px" } });
  flt.addEventListener("input", () => { filter = flt.value.trim().toUpperCase(); drawLog(); });
  const pause = switchEl({ checked: false, label: "Pause", onchange: (v) => { paused = v; } });
  function drawLog() {
    const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
    log.textContent = (filter ? lines.filter((l) => l.toUpperCase().includes(filter)) : lines).join("\n");
    if (stick) log.scrollTop = log.scrollHeight;
  }
  const unsubNmea = subscribeTopic("nmea");
  const offN = on("nmea", (ls) => {
    if (paused) return;
    lines.push(...ls);
    if (lines.length > 300) lines = lines.slice(-300);
    drawLog();
  });

  // ── reconfiguration ──
  const cfgRes = h("div");
  const cfgBtn = h("button.btn", { type: "button", onclick: async () => {
    if (!(await confirmDialog({ title: "Reconfigure the GPS module?", text: "The module will be configured over UBX using the parameters below (rate, constellations, dynamic model). GPS will drop out for a few seconds. Save any changed parameters first.", okText: "Reconfigure" }))) return;
    cfgBtn.disabled = true;
    clear(cfgRes);
    try {
      const r = await api.post("/api/gps/configure");
      const det = r && r.details != null ? (typeof r.details === "string" ? r.details : JSON.stringify(r.details)) : "";
      cfgRes.append(banner(r && r.ok ? "ok" : "bad", (r && r.ok ? "Module reconfigured. " : "Reconfiguration failed. ") + det));
    } catch (e) { cfgRes.append(banner("bad", errText(e))); }
    cfgBtn.disabled = false;
  } }, icon("refresh"), "Reconfigure module");

  // ── settings ──
  const form = new SettingsForm();
  const f = (s) => form.add(s);
  const setCard = section("GPS settings",
    f({ path: "gps.device", label: "Device", type: "text" }),
    f({ path: "gps.baud", label: "Port speed", type: "select", options: [9600, 38400, 57600, 115200, 230400, 460800].map((b) => ({ value: b, label: String(b) })), unit: "baud" }),
    f({ path: "gps.rate_hz", label: "Update rate", type: "number", min: 1, max: 18, step: 1, unit: "Hz" }),
    f({ path: "gps.constellations", label: "Constellations", type: "multi", options: [{ value: "gps", label: "GPS" }, { value: "glonass", label: "GLONASS" }, { value: "galileo", label: "Galileo" }, { value: "beidou", label: "BeiDou" }] }),
    f({ path: "gps.dyn_model", label: "Dynamic model", type: "select", options: [{ value: "automotive", label: "Automotive" }, { value: "portable", label: "Portable" }, { value: "airborne1g", label: "Airborne (1g)" }] }),
    form.bar,
  );

  root.append(
    h(".page-head", null, h("h1", null, "GPS"), fixInfo),
    h(".grid", { style: { gridTemplateColumns: "minmax(300px, 400px) minmax(0, 1fr)", alignItems: "start" } },
      h(".card", null, h("h2", null, "Sky plot"), plot, legend),
      h(".card", null, h(".card-head", null, h("h2", null, "Satellites"), sum), h(".tbl-wrap", { style: { maxHeight: "420px" } }, h("table", null,
        h("thead", null, h("tr", null, h("th", null, "System"), h("th.num", null, "PRN"), h("th.num", null, "Elev.°"), h("th.num", null, "Azim.°"), h("th", null, "SNR, dB-Hz"), h("th", null, "Used"))), tbody))),
    ),
    h(".card", null,
      h(".card-head", null, h("h2", null, "Raw NMEA"), flt, pause, h("button.btn.sm", { type: "button", onclick: () => { lines = []; drawLog(); } }, "Clear")),
      log),
    h(".card", null, h("h2", null, "Module"), h("div.btnrow", null, cfgBtn, h("span.dim", null, "Applies the parameters below to the u-blox chip.")), cfgRes),
    setCard,
  );
  renderFix();
  const offT = on("telemetry", renderFix);
  return () => { stopSats(); unsubNmea(); offN(); offT(); form.destroy(); };
}
