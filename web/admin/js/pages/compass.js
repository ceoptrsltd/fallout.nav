// Compass: live X/Y/Z and heading, calibration wizard, mounting offset, declination.
import { h, clear, isNum, compassWord, clone } from "../dom.js";
import { api, errText } from "../api.js";
import { SettingsForm, section } from "../form.js";
import { store, on, applySettings } from "../store.js";
import { toast, confirmDialog, poll, banner, segmented, switchEl } from "../ui.js";

const IDENTITY = [[1, 0, 0], [0, 1, 0], [0, 0, 1]];

/** Per-sector point counts (36 x 10 deg, index 0 = north). Falls back to an even spread by coverage %. */
function sectorCounts(cal) {
  if (cal && Array.isArray(cal.sectors) && cal.sectors.length === 36) return cal.sectors;
  const n = Math.round(((cal && cal.coverage_pct) || 0) * 36 / 100);
  return Array.from({ length: 36 }, (_, i) => (Math.floor(((i + 1) * n) / 36) > Math.floor((i * n) / 36) ? 1 : 0));
}

function sectorDiagram() {
  const C = 100, R1 = 94, R0 = 56;
  const pt = (r, deg) => [C + r * Math.sin((deg * Math.PI) / 180), C - r * Math.cos((deg * Math.PI) / 180)];
  const paths = [];
  const svg = h("svg", { viewBox: "0 0 200 200", style: { width: "200px", height: "200px", flex: "none" } });
  for (let i = 0; i < 36; i++) {
    const a0 = i * 10 + 0.8, a1 = (i + 1) * 10 - 0.8;
    const [x0, y0] = pt(R1, a0), [x1, y1] = pt(R1, a1), [x2, y2] = pt(R0, a1), [x3, y3] = pt(R0, a0);
    const p = h("path", { d: `M${x0},${y0} A${R1},${R1} 0 0 1 ${x1},${y1} L${x2},${y2} A${R0},${R0} 0 0 0 ${x3},${y3} Z`, fill: "#5dff8a", "fill-opacity": 0.06, stroke: "#2b3427", "stroke-width": 0.8 });
    p.append(h("title", null, ""));
    paths.push(p);
    svg.append(p);
  }
  const centerPct = h("text", { x: C, y: C + 2, "text-anchor": "middle", fill: "#dbe4d3", "font-size": 22, "font-family": "monospace", "font-weight": "bold" }, "0%");
  svg.append(
    centerPct,
    h("text", { x: C, y: C + 17, "text-anchor": "middle", fill: "#8d9987", "font-size": 8.5, "font-family": "monospace" }, "coverage"),
    h("text", { x: C, y: 9, "text-anchor": "middle", fill: "#ffb62e", "font-size": 9, "font-family": "monospace" }, "N"),
    h("text", { x: 195, y: C + 3, "text-anchor": "end", fill: "#5d6858", "font-size": 8, "font-family": "monospace" }, "E"),
    h("text", { x: C, y: 198, "text-anchor": "middle", fill: "#5d6858", "font-size": 8, "font-family": "monospace" }, "S"),
    h("text", { x: 5, y: C + 3, fill: "#5d6858", "font-size": 8, "font-family": "monospace" }, "W"),
  );
  svg.update = (cal) => {
    const counts = sectorCounts(cal);
    const scale = Math.max(8, ...counts);
    counts.forEach((c, i) => {
      paths[i].setAttribute("fill-opacity", c > 0 ? (0.3 + 0.7 * Math.min(1, c / scale)).toFixed(2) : "0.06");
      paths[i].firstChild.textContent = `${i * 10}-${(i + 1) * 10} deg: ${c} point${c === 1 ? "" : "s"}`;
    });
    centerPct.textContent = cal ? Math.round(cal.coverage_pct || 0) + "%" : "-";
  };
  return svg;
}

const SRC = { gps: "GPS", mag: "compass", blend: "compass + GPS", none: "none" };

function declinationWidget(f) {
  const seg = segmented({
    name: "decl",
    value: f.value === "auto" ? "auto" : "manual",
    options: [{ value: "auto", label: "Auto (from position)" }, { value: "manual", label: "Manual" }],
    onchange: (v) => {
      if (v === "auto") f.change("auto");
      else f.change(num.value !== "" && Number.isFinite(Number(num.value)) ? Number(num.value) : 0);
      sync(f.value);
    },
  });
  const num = h("input", { type: "number", step: 0.1, min: -90, max: 90, style: { width: "100px" } });
  num.addEventListener("input", () => { if (num.value !== "" && Number.isFinite(Number(num.value))) f.change(Number(num.value)); });
  const cur = h("span.dim.mono");
  function sync(v) {
    const auto = v === "auto" || v === undefined || v === null;
    seg.set(auto ? "auto" : "manual");
    num.disabled = auto;
    if (!auto) num.value = String(v);
    const d = store.telemetry?.heading?.declination_deg;
    cur.textContent = isNum(d) ? `currently applied: ${d.toFixed(1)}°` : "";
  }
  return { el: h("span", { style: { display: "inline-flex", gap: "12px", alignItems: "center", flexWrap: "wrap" } }, seg, num, "°", cur), update: sync };
}

export async function mount(root) {
  // ── live data ──
  const hd = h(".big.amber", null, "—");
  const arrow = h("g", null, h("polygon", { points: "50,8 58,52 50,46 42,52", fill: "#5dff8a" }), h("polygon", { points: "50,92 58,52 50,46 42,52", fill: "#3a4634" }));
  const rose = h("svg", { viewBox: "0 0 100 100", style: { width: "110px", height: "110px" } },
    h("circle", { cx: 50, cy: 50, r: 47, fill: "#0a0c0a", stroke: "#3a4634", "stroke-width": 2 }),
    h("text", { x: 50, y: 20, "text-anchor": "middle", fill: "#ffb62e", "font-size": 11, "font-family": "monospace" }, "N"),
    arrow);
  const hv = {};
  const kvr = (k, label) => { const v = h("span.v", null, "—"); hv[k] = v; return [h("span.k", null, label), v]; };
  const headCard = h(".card", null, h("h2", null, "Heading"),
    h(".bigrow", null, hd, rose), h("hr.sep"),
    h(".kv", null, kvr("src", "Source"), kvr("mag", "Compass"), kvr("gps", "GPS (COG)"), kvr("decl", "Declination")));

  const bars = {};
  const axisRow = (k) => {
    const i = h("i");
    const val = h("span", null, "—");
    const raw = h("span.dim", { style: { fontSize: "11px" } });
    bars[k] = { i, val, raw };
    return [h("b", null, k.toUpperCase()), h(".tr", null, i), h("div", { style: { textAlign: "right" } }, val, h("br"), raw)];
  };
  const cv = h("canvas", { width: 300, height: 300, style: { width: "100%", maxWidth: "300px", aspectRatio: "1", background: "#060806", border: "1px solid var(--line)", borderRadius: "6px" } });
  const chipInfo = h("div.dim.mono", { style: { fontSize: "12.5px", marginTop: "8px" } });
  const magCard = h(".card", null, h("h2", null, "Magnetometer (µT, calibrated)"),
    h("div", { style: { display: "flex", gap: "18px", flexWrap: "wrap", alignItems: "flex-start" } },
      h("div", { style: { flex: "1 1 260px" } }, h(".bar3", null, axisRow("x"), axisRow("y"), axisRow("z")), chipInfo),
      h("div", { style: { flex: "0 1 300px", width: "100%" } }, cv, h(".hint", null, "X–Y plot: after a good calibration the points form a circle around the center. Rotate the module to see it.")),
    ));

  const pts = [];
  const ctx = cv.getContext("2d");
  function drawScatter() {
    const W = cv.width, H = cv.height;
    ctx.clearRect(0, 0, W, H);
    ctx.strokeStyle = "#2b3427"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(W / 2, 0); ctx.lineTo(W / 2, H); ctx.moveTo(0, H / 2); ctx.lineTo(W, H / 2); ctx.stroke();
    if (!pts.length) return;
    let m = 10;
    for (const p of pts) m = Math.max(m, Math.abs(p[0]), Math.abs(p[1]));
    const k = (W / 2 - 10) / m;
    pts.forEach((p, i) => {
      ctx.fillStyle = `rgba(93,255,138,${0.15 + 0.85 * (i / pts.length)})`;
      ctx.fillRect(W / 2 + p[0] * k - 1.5, H / 2 - p[1] * k - 1.5, 3, 3);
    });
    ctx.fillStyle = "#ffb62e"; ctx.font = "11px monospace"; ctx.fillText(`±${m.toFixed(0)} µT`, 6, 14);
  }

  function setV(k, t) { hv[k].textContent = t; }
  function live() {
    const t = store.telemetry;
    if (!t) return;
    const hh = t.heading || {};
    hd.textContent = isNum(hh.deg) ? `${hh.deg.toFixed(0)}° ${compassWord(hh.deg)}` : "—";
    arrow.setAttribute("transform", `rotate(${isNum(hh.deg) ? hh.deg : 0} 50 50)`);
    setV("src", SRC[hh.source] || "—");
    setV("mag", isNum(hh.mag_deg) ? hh.mag_deg.toFixed(1) + "°" : "—");
    setV("gps", isNum(hh.gps_course_deg) ? hh.gps_course_deg.toFixed(1) + "°" : "—");
    setV("decl", isNum(hh.declination_deg) ? hh.declination_deg.toFixed(1) + "°" : "—");
    const c = t.compass;
    for (const k of ["x", "y", "z"]) {
      const b = bars[k];
      if (!c) { b.val.textContent = "—"; b.raw.textContent = ""; b.i.style.width = "0"; continue; }
      const v = c[k];
      const pct = Math.max(-1, Math.min(1, v / 100)) * 50;
      b.i.style.left = pct >= 0 ? "50%" : 50 + pct + "%";
      b.i.style.width = Math.abs(pct) + "%";
      b.val.textContent = v.toFixed(1);
      const r = c["raw_" + k];
      b.raw.textContent = isNum(r) ? `raw ${r.toFixed(1)}` : "";
    }
    chipInfo.textContent = c ? `chip: ${c.chip || "?"} · ${c.ok ? "responding" : "NOT RESPONDING"}` : "Compass is not responding";
    chipInfo.style.color = c && c.ok ? "" : "var(--red)";
    if (c) { pts.push([c.x, c.y]); if (pts.length > 300) pts.shift(); drawScatter(); }
  }

  // ── calibration wizard ──
  const wiz = h(".card");
  let wstate = "idle"; // idle | running | done
  let result = null;
  let calStatus = { active: false, samples: 0, coverage_pct: 0, message: "" };
  let stopCalPoll = null;

  const qualCls = (q) => (isNum(q) ? (q >= 0.8 ? "green" : q >= 0.5 ? "amber" : "red") : "");
  const calSummary = () => {
    const c = store.settings?.compass?.calibration || {};
    const q = c.quality;
    return h(".kv", null,
      h("span.k", null, "Last calibrated"), h("span.v", null, c.fitted_at ? c.fitted_at.replace("T", " ").replace(/\+.*$/, "") : "never"),
      h("span.k", null, "Quality"), h("span.v", { class: qualCls(q) }, isNum(q) ? (q * 100).toFixed(0) + " %" : "—"),
      h("span.k", null, "Offset (hard iron), µT"), h("span.v", null, (c.offset || [0, 0, 0]).map((x) => x.toFixed(2)).join(" ; ")),
    );
  };

  function renderWiz() {
    clear(wiz);
    const order = ["idle", "running", "done"];
    const steps = h(".steps", null, ["1. Prepare", "2. Rotate", "3. Result"].map((t, i) => h("div" + (order.indexOf(wstate) === i ? ".cur" : order.indexOf(wstate) > i ? ".done" : ""), null, t)));
    wiz.append(h("h2", { style: { marginBottom: "10px" } }, "Manual calibration"), steps);
    if (wstate === "idle") {
      wiz.append(
        h("ol", { style: { margin: "0 0 12px", paddingLeft: "20px", lineHeight: "1.7" } },
          h("li", null, "Mount the module where it will live (in the car, away from speakers and magnets), arrow pointing forward."),
          h("li", null, "Press \"Start calibration\"."),
          h("li", null, "Slowly rotate the module (or the car) through all directions, tilting it forward/back and sideways, until coverage reaches 80% or more."),
          h("li", null, "Press \"Finish\": the result is saved to the settings."),
        ),
        calSummary(),
        h("div.btnrow", { style: { marginTop: "12px" } },
          h("button.btn.primary", { type: "button", onclick: startCal }, "Start calibration"),
          h("button.btn.danger", { type: "button", onclick: resetCal }, "Reset calibration"),
        ),
      );
    } else if (wstate === "running") {
      const cov = Math.max(0, Math.min(100, calStatus.coverage_pct || 0));
      wiz.append(
        h("div", { style: { display: "flex", justifyContent: "space-between", marginBottom: "4px" } }, h("span", null, "Direction coverage"), h("b.mono", { class: cov >= 80 ? "green" : "amber" }, cov.toFixed(0) + " %")),
        h(".progress" + (cov >= 80 ? ".green" : ""), null, h("i", { style: { width: cov + "%" } })),
        h(".kv", { style: { marginTop: "12px" } }, h("span.k", null, "Samples collected"), h("span.v", null, String(calStatus.samples ?? 0)), h("span.k", null, "Hint"), h("span.v", { style: { textAlign: "right", fontFamily: "var(--sans)" } }, calStatus.message || "keep rotating the module")),
        cov < 80 ? h(".hint", null, "Keep going until coverage reaches 80% or more.") : h(".hint.green", null, "Coverage is sufficient: you can finish."),
        h("div.btnrow", { style: { marginTop: "12px" } },
          h("button.btn.primary", { type: "button", onclick: finishCal }, "Finish"),
          h("button.btn", { type: "button", onclick: cancelCal }, "Cancel"),
        ),
      );
    } else {
      const c = result || {};
      wiz.append(
        banner("ok", "Calibration saved and applied."),
        h("div", { style: { height: "10px" } }),
        h(".kv", null,
          h("span.k", null, "Fit quality"), h("span.v", { class: qualCls(c.quality) }, isNum(c.quality) ? (c.quality * 100).toFixed(0) + " %" : "—"),
          h("span.k", null, "Offset, µT"), h("span.v", null, (c.offset || []).map((x) => x.toFixed(2)).join(" ; ")),
          h("span.k", null, "Time"), h("span.v", null, c.fitted_at || "—")),
        isNum(c.quality) && c.quality < 0.6 ? h("div", { style: { marginTop: "10px" } }, banner("warn", "Low quality: rotate the module through more directions or move away from metal, then repeat.")) : null,
        h("div.btnrow", { style: { marginTop: "12px" } }, h("button.btn", { type: "button", onclick: () => { wstate = "idle"; renderWiz(); } }, "Done"), h("button.btn.primary", { type: "button", onclick: startCal }, "Calibrate again")),
      );
    }
  }

  async function refreshCal() {
    try {
      calStatus = await api.get("/api/compass/calibration");
      if (wstate === "running" && !calStatus.active) { wstate = "idle"; stopPoll(); }
      if (wstate === "idle" && calStatus.active) { wstate = "running"; startPoll(); }
      if (wstate === "running") renderWiz();
    } catch (e) { /* ignore a single failed poll */ }
  }
  function startPoll() { stopPoll(); stopCalPoll = poll(refreshCal, 500, { immediate: false }); }
  function stopPoll() { if (stopCalPoll) { stopCalPoll(); stopCalPoll = null; } }

  async function startCal() {
    try {
      await api.post("/api/compass/calibration/start");
      wstate = "running"; calStatus = { active: true, samples: 0, coverage_pct: 0, message: "" };
      renderWiz(); startPoll();
    } catch (e) { toast("error", errText(e)); }
  }
  async function finishCal() {
    if ((calStatus.coverage_pct || 0) < 50) {
      if (!(await confirmDialog({ title: "Low coverage", text: "Coverage is below 50%. The calibration will be inaccurate. Finish anyway?", okText: "Finish" }))) return;
    }
    try {
      stopPoll();
      result = await api.post("/api/compass/calibration/finish");
      wstate = "done";
      toast("ok", "Compass calibration saved");
      renderWiz();
    } catch (e) { toast("error", errText(e)); wstate = "running"; startPoll(); }
  }
  async function cancelCal() {
    stopPoll();
    try { await api.post("/api/compass/calibration/cancel"); } catch (e) { toast("error", errText(e)); }
    wstate = "idle"; renderWiz();
  }
  async function resetCal() {
    if (!(await confirmDialog({ title: "Reset calibration?", text: "Offset and matrix go back to identity values. The compass will be inaccurate until you calibrate again.", okText: "Reset", danger: true }))) return;
    try {
      const res = await api.patch("/api/settings", { compass: { calibration: { offset: [0, 0, 0], matrix: clone(IDENTITY), fitted_at: null, quality: null } } });
      if (res && res.compass) applySettings(res);
      toast("ok", "Calibration reset");
      renderWiz();
    } catch (e) { toast("error", errText(e)); }
  }

  // ── auto-calibration (while driving) ──
  const autoSw = switchEl({ checked: !!store.settings?.compass?.auto_calibrate, label: "Enabled", onchange: async (v) => {
    try {
      const res = await api.patch("/api/settings", { compass: { auto_calibrate: v } });
      if (res && res.compass) applySettings(res);
      toast("ok", v ? "Auto-calibration enabled" : "Auto-calibration disabled");
    } catch (e) { toast("error", errText(e)); autoSw.input.checked = !v; }
  } });
  const diagram = sectorDiagram();
  const av = {};
  const akv = (k, label) => { const v = h("span.v", null, "—"); av[k] = v; return [h("span.k", null, label), v]; };
  const stateBadge = h("span.badge", null, "—");
  const autoMsg = h(".hint", { style: { marginTop: "8px" } });
  const AUTO_STATE = { disabled: ["disabled", ""], collecting: ["collecting", "warn"], fitted: ["fitted", "ok"], error: ["error", "bad"] };
  const when = (t) => (t ? t.replace("T", " ").replace(/[+]\d\d:\d\d$|Z$/, "") : "never");
  function renderAuto() {
    const c = store.telemetry?.compass_cal;
    const en = store.settings?.compass?.auto_calibrate;
    if (en !== undefined) autoSw.input.checked = !!en;
    diagram.update(c);
    if (!c) { stateBadge.textContent = "no data"; stateBadge.className = "badge"; autoMsg.textContent = ""; return; }
    const [txt, kind] = AUTO_STATE[c.state] || [c.state, ""];
    stateBadge.textContent = txt;
    stateBadge.className = "badge" + (kind ? " " + kind : "");
    av.samples.textContent = String(c.samples ?? 0);
    av.cov.textContent = isNum(c.coverage_pct) ? c.coverage_pct.toFixed(0) + " %" : "—";
    av.rms.textContent = isNum(c.rms_deg) ? c.rms_deg.toFixed(1) + "°" : "—";
    av.applied.textContent = c.applied ? "yes" : "no";
    av.applied.className = "v " + (c.applied ? "green" : "");
    av.saved.textContent = when(c.last_saved);
    autoMsg.textContent = c.message || "";
  }
  async function resetAuto() {
    if (!(await confirmDialog({ title: "Reset auto-calibration?", text: "The samples collected so far are discarded and collection starts over. The currently saved calibration is not changed.", okText: "Reset", danger: true }))) return;
    try { await api.post("/api/compass/calibration/reset-auto"); toast("ok", "Auto-calibration samples cleared"); } catch (e) { toast("error", errText(e)); }
  }
  const autoCard = h(".card", null,
    h(".card-head", null, h("h2", null, "Auto-calibration (while driving)"), stateBadge, autoSw),
    h(".hint", { style: { margin: "0 0 12px" } }, "Collects samples while driving straight above 5 m/s and fits the compass to the GPS course. Drive in different directions to fill the circle."),
    h("div", { style: { display: "flex", gap: "24px", flexWrap: "wrap", alignItems: "center" } },
      diagram,
      h("div", { style: { flex: "1 1 280px" } },
        h(".kv", null, akv("samples", "Samples"), akv("cov", "Direction coverage"), akv("rms", "RMS error"), akv("applied", "Applied now"), akv("saved", "Last saved")),
        autoMsg,
        h("div.btnrow", { style: { marginTop: "12px" } }, h("button.btn.danger", { type: "button", onclick: resetAuto }, "Reset auto-calibration")),
      ),
    ),
  );

  // ── saved calibration ──
  const sv = {};
  const skv = (k, label) => { const v = h("span.v", null, "—"); sv[k] = v; return [h("span.k", null, label), v]; };
  const savedCard = h(".card", null, h("h2", null, "Saved calibration"),
    h(".kv", null, skv("source", "Source"), skv("when", "Calibrated"), skv("quality", "Quality"), skv("rms", "RMS error"), skv("cov", "Coverage"), skv("samples", "Samples"), skv("offset", "Offset (hard iron), µT"), skv("dev", "Deviation A, B, C, D, E (°)")));
  function renderSaved() {
    const c = store.settings?.compass?.calibration || {};
    sv.source.textContent = c.source === "auto" ? "auto (GPS course)" : c.source === "manual" ? "manual" : "none";
    sv.when.textContent = when(c.fitted_at);
    sv.quality.textContent = isNum(c.quality) ? (c.quality * 100).toFixed(0) + " %" : "—";
    sv.rms.textContent = isNum(c.rms_deg) ? c.rms_deg.toFixed(1) + "°" : "—";
    sv.cov.textContent = c.coverage_pct ? c.coverage_pct.toFixed(0) + " %" : "—";
    sv.samples.textContent = c.samples ? String(c.samples) : "—";
    sv.offset.textContent = (c.offset || []).map((x) => x.toFixed(1)).join(" ; ") || "—";
    sv.dev.textContent = c.deviation && c.deviation.length ? c.deviation.map((x) => x.toFixed(2)).join(" ; ") : "not applied";
  }

  // ── settings ──
  const form = new SettingsForm();
  const f = (s) => form.add(s);
  const setCard = section("Compass settings",
    f({ path: "compass.mount_offset_deg", label: "Mounting offset", hint: "How far the module is rotated relative to the car axis", type: "range", min: -180, max: 180, step: 0.5, unit: "°" }),
    f({ path: "compass.declination", label: "Magnetic declination", type: "custom", build: declinationWidget }),
    f({ path: "heading.gps_above_mps", label: "Use GPS heading above", hint: "Below this speed the compass is used", type: "number", min: 0, step: 0.5, unit: "m/s" }),
    f({ path: "heading.blend_band_mps", label: "Blend band", hint: "Width of the smooth compass/GPS transition", type: "number", min: 0, step: 0.5, unit: "m/s" }),
    f({ path: "heading.smoothing_s", label: "Heading smoothing", hint: "Time constant", type: "number", min: 0, step: 0.1, unit: "s" }),
    f({ path: "compass.chip", label: "Chip", type: "select", options: [{ value: "auto", label: "Auto" }, { value: "ist8310", label: "IST8310" }, { value: "qmc5883l", label: "QMC5883L" }, { value: "hmc5883l", label: "HMC5883L" }] }),
    f({ path: "compass.rate_hz", label: "Polling rate", type: "number", min: 1, max: 100, step: 1, unit: "Hz" }),
    f({ path: "compass.i2c_bus", label: "I²C bus", type: "number", min: 0, step: 1 }),
    f({ path: "compass.address", label: "I²C address", hint: "Empty = chip default (IST8310 = 14 = 0x0E)", type: "number", nullable: true, min: 0, max: 127, step: 1 }),
    h("details", { style: { marginTop: "12px" } },
      h("summary", { style: { cursor: "pointer", color: "var(--amber)", padding: "6px 0" } }, "Advanced settings"),
      f({ path: "compass.axes", label: "Axis override", hint: "Format \"-x,-y,-z\"; empty = chip default", type: "text", placeholder: "-x,-y,-z",
        validate: (v) => { const t = String(v || "").trim().toLowerCase(); if (!t) return null; const p = t.split(",").map((x) => x.trim()); return p.length === 3 && p.every((x) => /^[-+]?[xyz]$/.test(x)) && new Set(p.map((x) => x.replace(/^[-+]/, ""))).size === 3 ? null : "Use three distinct axes, e.g. -x,-y,-z"; } }),
      f({ path: "compass.auto_min_speed_mps", label: "Auto: minimum speed", hint: "Samples are taken only above this speed", type: "number", min: 1, step: 0.5, unit: "m/s" }),
      f({ path: "compass.auto_max_turn_rate_dps", label: "Auto: max turn rate", hint: "Only near-straight driving counts", type: "number", min: 0.1, step: 0.1, unit: "°/s" }),
      f({ path: "compass.auto_max_hdop", label: "Auto: max HDOP", hint: "Only with a good GPS fix", type: "number", min: 0.1, step: 0.1 }),
      f({ path: "compass.auto_min_coverage_pct", label: "Auto: apply at coverage", hint: "Fit is applied once this much of the circle is covered", type: "number", min: 10, max: 100, step: 1, unit: "%" }),
      f({ path: "compass.auto_save_interval_s", label: "Auto: save interval", hint: "How often an improved calibration is saved", type: "number", min: 10, step: 10, unit: "s" }),
    ),
    form.bar,
  );

  root.append(h(".page-head", null, h("h1", null, "Compass")), autoCard, savedCard, h(".grid.g2", null, headCard, magCard), setCard, wiz);

  try {
    const s = await api.get("/api/compass/calibration");
    calStatus = s;
    if (s.active) { wstate = "running"; startPoll(); }
  } catch { /* */ }
  renderWiz();
  live();
  renderAuto();
  renderSaved();
  const offT = on("telemetry", () => { live(); renderAuto(); });
  const offS = on("settings", () => { renderSaved(); renderAuto(); if (wstate === "idle") renderWiz(); });
  return () => { offT(); offS(); stopPoll(); form.destroy(); };
}
