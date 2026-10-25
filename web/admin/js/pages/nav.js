// Navigation: map orientation, speed -> zoom table, thresholds, layers, tile cache, display options.
import { h, icon, clear, fmt, isNum, clone } from "../dom.js";
import { api, appUrl, errText } from "../api.js";
import { SettingsForm, section } from "../form.js";
import { store, on } from "../store.js";
import { segmented, toast, confirmDialog, poll } from "../ui.js";

const KMH = 3.6;
const r1 = (x) => Number(x.toFixed(1));
const r2 = (x) => Number(x.toFixed(2));

// ───── orientation pictograms ─────
function pictogram(kind) {
  const north = kind === "north_up";
  const car = (rot) => h("g", { transform: `translate(50 52) rotate(${rot})` },
    h("polygon", { points: "0,-17 11,13 0,7 -11,13", fill: "#5dff8a", stroke: "#0a0c0a", "stroke-width": 2 }));
  const nMark = h("text", { x: 50, y: 21, "text-anchor": "middle", "font-size": 15, fill: "#ffb62e", "font-family": "monospace", "font-weight": "bold" }, "N");
  const marker = north ? nMark : h("g", { transform: "rotate(-40 50 50)" }, nMark);
  const grid = h("g", { stroke: "#3a4634", "stroke-width": 1.2, transform: north ? "" : "rotate(-40 50 50)" },
    h("line", { x1: 12, y1: 35, x2: 88, y2: 35 }), h("line", { x1: 12, y1: 65, x2: 88, y2: 65 }),
    h("line", { x1: 35, y1: 12, x2: 35, y2: 88 }), h("line", { x1: 65, y1: 12, x2: 65, y2: 88 }));
  return h("svg", { viewBox: "0 0 100 100" },
    h("circle", { cx: 50, cy: 50, r: 46, fill: "#0a0c0a", stroke: "#4a5944", "stroke-width": 2 }),
    grid, marker, car(north ? 40 : 0));
}

function orientationWidget(f) {
  const wrap = h(".orient");
  const items = [
    { value: "north_up", title: "North up", desc: "The map stays fixed with north at the top. The car icon rotates with the heading." },
    { value: "heading_up", title: "Heading up", desc: "The map rotates so the car always points up and north moves around." },
  ];
  const labels = items.map((it) => {
    const input = h("input", { type: "radio", name: "orient", value: it.value });
    const lab = h("label", null, input, pictogram(it.value), h("div", null, h("b", null, it.title), h("span", null, it.desc)));
    input.addEventListener("change", () => f.change(it.value));
    wrap.append(lab);
    return { lab, input, value: it.value };
  });
  return { el: wrap, update: (v) => labels.forEach((l) => { l.lab.classList.toggle("sel", l.value === v); l.input.checked = l.value === v; }) };
}

// ───── editable zoom table ─────
function zoomTableWidget(f) {
  let rows = [];
  const tbody = h("tbody");
  const msg = h(".hint");
  const sortRows = () => rows.sort((a, b) => a.min_mps - b.min_mps);
  const emit = () => f.change(rows.map((r) => ({ min_mps: r.min_mps, zoom: r.zoom })));

  function draw() {
    clear(tbody);
    rows.forEach((r, i) => {
      const next = rows[i + 1];
      const kmhIn = h("input", { type: "number", min: 0, step: 1, value: r1(r.min_mps * KMH), title: "km/h" });
      const mpsIn = h("input", { type: "number", min: 0, step: 0.5, value: r2(r.min_mps), title: "m/s" });
      const zIn = h("input", { type: "number", min: 1, max: 22, step: 0.5, value: r.zoom, title: "zoom" });
      const setMps = (mps, from) => {
        if (!Number.isFinite(mps) || mps < 0) return;
        r.min_mps = mps;
        if (from !== "kmh") kmhIn.value = r1(mps * KMH);
        if (from !== "mps") mpsIn.value = r2(mps);
        emit();
      };
      kmhIn.addEventListener("input", () => setMps(r2(Number(kmhIn.value) / KMH), "kmh"));
      mpsIn.addEventListener("input", () => setMps(Number(mpsIn.value), "mps"));
      const resort = () => { sortRows(); draw(); emit(); };
      kmhIn.addEventListener("change", resort);
      mpsIn.addEventListener("change", resort);
      zIn.addEventListener("input", () => { const z = Number(zIn.value); if (zIn.value !== "" && Number.isFinite(z)) { r.zoom = z; emit(); } });
      const range = next ? `${r1(r.min_mps * KMH)} … ${r1(next.min_mps * KMH)} km/h` : `from ${r1(r.min_mps * KMH)} km/h`;
      tbody.append(h("tr", null,
        h("td", null, kmhIn),
        h("td", null, mpsIn),
        h("td", null, zIn),
        h("td.dim.mono", { style: { fontSize: "12.5px" } }, range),
        h("td.right", null, h("button.btn.sm.danger.icon", { type: "button", title: "Delete row", disabled: rows.length <= 1, onclick: () => { rows.splice(i, 1); draw(); emit(); } }, icon("trash"))),
      ));
    });
    const first = rows[0];
    msg.textContent = first && first.min_mps > 0 ? "Warning: there is no row starting at 0 km/h, so the zoom is undefined at low speed." : "The row with the highest threshold not above the current speed is used.";
    msg.style.color = first && first.min_mps > 0 ? "var(--amber)" : "";
  }

  const add = h("button.btn.sm", { type: "button", onclick: () => {
    const last = rows[rows.length - 1];
    rows.push({ min_mps: last ? r2(last.min_mps + 5) : 0, zoom: last ? Math.max(1, last.zoom - 1) : 19 });
    draw(); emit();
  } }, icon("plus"), "Add row");

  const el = h("div", { style: { width: "100%" } },
    h(".tbl-wrap", null, h("table.zt", null,
      h("thead", null, h("tr", null, h("th", null, "From speed, km/h"), h("th", null, "m/s"), h("th", null, "Zoom"), h("th", null, "Range"), h("th"))),
      tbody)),
    h("div", { style: { display: "flex", gap: "12px", alignItems: "center", marginTop: "8px" } }, add, msg),
  );
  return { el, update: (v) => { rows = clone(v || []).map((x) => ({ min_mps: x.min_mps, zoom: x.zoom })); sortRows(); draw(); } };
}

/** Compass design list from the display UI module (agent C); falls back to v1..v3. */
async function loadCompassStyles() {
  const fallback = [{ id: "v1", name: "v1" }, { id: "v2", name: "v2" }, { id: "v3", name: "v3" }];
  try {
    const m = await import(appUrl("display/ui/compass.js"));
    if (Array.isArray(m.compassStyles) && m.compassStyles.length) return m.compassStyles.map((s) => ({ id: s.id, name: s.name || s.id }));
  } catch { /* module not available */ }
  return fallback;
}

export async function mount(root) {
  const styles = await loadCompassStyles();
  const form = new SettingsForm();
  const f = (s) => form.add(s);

  const orientCard = section("Map orientation",
    f({ path: "display.orientation", type: "custom", bare: true, build: orientationWidget }),
    h(".hint", { style: { marginTop: "8px" } }, "Applied on the device screen after saving. Use the \"Screen\" button at the top to check the result."),
    h("hr.sep"),
    h("h3", { style: { marginBottom: "4px" } }, "Rotation smoothing (heading-up mode)"),
    f({ path: "display.compass_smoothing_s", label: "Compass smoothing on display", hint: "Time constant for the on-screen compass needle/scale", type: "range", min: 0, max: 2, step: 0.05, unit: "s" }),
    f({ path: "display.vehicle_rotation_lag_s", label: "Vehicle icon turn smoothing", hint: "Time constant: how quickly the car icon turns to the new heading", type: "range", min: 0, max: 3, step: 0.05, unit: "s" }),
    f({ path: "display.map_rotation_lag_s", label: "Map catch-up time", hint: "Time constant: the map follows the icon slowly (heading-up)", type: "range", min: 0, max: 10, step: 0.1, unit: "s" }),
    f({ path: "display.map_rotation_deadband_deg", label: "Ignore heading jitter below", hint: "Small heading wobbles do not rotate the map", type: "range", min: 0, max: 20, step: 0.5, unit: "°" }),
  );

  // forced screen mode (instant, separate endpoint)
  const force = segmented({
    name: "forcemode",
    value: store.settings?.display?.force_mode || "auto",
    options: [{ value: "auto", label: "Auto (by speed)" }, { value: "map", label: "Always map" }, { value: "camera", label: "Always camera" }],
    onchange: async (v) => {
      try { await api.post("/api/display/force", { mode: v }); toast("ok", "Screen mode changed"); }
      catch (e) { toast("error", errText(e)); force.set(store.settings?.display?.force_mode || "auto"); }
    },
  });
  const offS = on("settings", (s) => force.set(s?.display?.force_mode || "auto"));
  const forceCard = section("Screen mode", h("div", { style: { display: "flex", gap: "16px", alignItems: "center", flexWrap: "wrap" } }, force, h("span.dim", null, "Applied immediately, no need to press \"Save\".")));

  const zoomCard = section("Speed → map zoom",
    f({ path: "nav.zoom_table", type: "custom", bare: true, build: zoomTableWidget,
      validate: (list) => {
        if (!Array.isArray(list) || !list.length) return "At least one row is required";
        const seen = new Set();
        for (const r of list) {
          if (!(r.zoom >= 1 && r.zoom <= 22)) return "Zoom must be between 1 and 22";
          if (!(r.min_mps >= 0)) return "Speed cannot be negative";
          if (seen.has(r.min_mps)) return "Two rows have the same speed";
          seen.add(r.min_mps);
        }
        return list.some((r) => r.min_mps === 0) ? null : "A row with threshold 0 is required";
      } }),
  );

  const mpsToKmh = (v) => (isNum(v) ? `= ${r1(v * KMH)} km/h` : "");
  const thrCard = section("Camera and thresholds",
    f({ path: "nav.camera_below_mps", label: "Show camera below", hint: "Speed under which the camera is shown", type: "number", min: 0, step: 0.1, unitFn: (v) => "m/s " + mpsToKmh(v) }),
    f({ path: "nav.map_above_mps", label: "Return to map above", hint: "Hysteresis: must be at least the camera threshold", type: "number", min: 0, step: 0.1, unitFn: (v) => "m/s " + mpsToKmh(v),
      validate: (v) => (v < (form.get("nav.camera_below_mps") ?? 0) ? "Must not be lower than the camera threshold" : null) }),
    f({ path: "nav.switch_delay_s", label: "Switch delay", hint: "How long the speed must hold before switching", type: "number", min: 0, step: 0.5, unit: "s" }),
    f({ path: "nav.zoom_delay_s", label: "Zoom change delay", hint: "Pause before changing the zoom step", type: "number", min: 0, step: 0.5, unit: "s" }),
    f({ path: "nav.no_fix_mode", label: "Without GPS fix", hint: "What to show when there are no coordinates", type: "seg", options: [{ value: "camera", label: "Camera" }, { value: "map", label: "Map" }] }),
  );

  const layersCard = section("Map layers",
    f({ path: "display.layers.imagery", label: "Satellite imagery", type: "switch" }),
    f({ path: "display.layers.transport", label: "Roads over imagery", type: "switch" }),
    f({ path: "display.layers.places", label: "Place labels", type: "switch" }),
    f({ path: "display.layers.street_map_instead", label: "Street map instead of satellite", hint: "Esri World Street Map", type: "switch" }),
    f({ path: "tiles.max_zoom", label: "Maximum tile zoom", type: "number", min: 10, max: 23, step: 1 }),
    f({ path: "tiles.esri_api_key", label: "Esri API key", hint: "Empty = legacy URLs without a key", type: "password", placeholder: "API key" }),
  );

  // tile cache
  const stat = { size: h("span.v", null, "—"), count: h("span.v", null, "—"), hit: h("span.v", null, "—") };
  const bar = h("i");
  const cacheKv = h(".kv", null, h("span.k", null, "Used"), stat.size, h("span.k", null, "Tiles"), stat.count, h("span.k", null, "Cache hit rate"), stat.hit);
  async function loadStats() {
    try {
      const s = await api.get("/api/tiles/stats");
      stat.size.textContent = `${fmt(s.size_mb, 1)} of ${fmt(s.limit_mb, 0)} MB`;
      stat.count.textContent = String(s.count ?? "—");
      stat.hit.textContent = isNum(s.hit_rate) ? (s.hit_rate * 100).toFixed(0) + " %" : "—";
      bar.style.width = isNum(s.size_mb) && s.limit_mb > 0 ? Math.min(100, (s.size_mb / s.limit_mb) * 100) + "%" : "0%";
    } catch (e) { stat.size.textContent = errText(e); }
  }
  const stopStats = poll(loadStats, 15000);
  const cacheCard = section("Tile cache",
    h(".progress", { style: { marginBottom: "10px" } }, bar),
    cacheKv,
    h("div", { style: { margin: "10px 0" } }, h("button.btn.danger", { type: "button", onclick: async () => {
      if (!(await confirmDialog({ title: "Clear the tile cache?", text: "All stored map tiles will be deleted. Without internet the map stays empty until tiles are downloaded again.", okText: "Clear cache", danger: true }))) return;
      try { await api.del("/api/tiles"); toast("ok", "Cache cleared"); loadStats(); } catch (e) { toast("error", errText(e)); }
    } }, icon("trash"), "Clear cache")),
    f({ path: "storage.tile_cache_mb", label: "Cache limit", hint: "Oldest tiles are evicted first", type: "number", min: 50, step: 50, unit: "MB" }),
  );

  const dispCard = section("Display and on-screen UI",
    f({ path: "display.compass_style", label: "Compass style", type: "select", options: styles.map((s) => ({ value: s.id, label: s.name })),
      help: "Preview the variants with the \"Screen\" button, or in the gallery at /display/ui/gallery.html." }),
    f({ path: "display.speed_units", label: "Speed units on screen", type: "seg", options: [{ value: "kmh", label: "km/h" }, { value: "mps", label: "m/s" }] }),
    f({ path: "display.screen.w_mm", label: "Visible area width", hint: "Real size used by the 1:1 preview", type: "number", min: 11, max: 499, step: 0.5, unit: "mm" }),
    f({ path: "display.screen.h_mm", label: "Visible area height", type: "number", min: 11, max: 499, step: 0.5, unit: "mm" }),
    f({ path: "display.safe_area_pct", label: "Safe area", hint: "Margin the CRT may cut off, per side", type: "range", min: 0, max: 20, step: 0.5, unit: "%" }),
    f({ path: "display.vehicle_anchor_y", label: "Vehicle position", hint: "Heading-up mode: fraction of the height from the top (0.5 = center, 0.67 = lower third)", type: "range", min: 0.3, max: 0.9, step: 0.01 }),
    f({ path: "display.show_trail", label: "Show trail", type: "switch" }),
    store.settings?.display?.show_clock !== undefined
      ? f({ path: "display.show_clock", label: "Clock on screen", hint: "Time, weekday and date in the top-left corner", type: "switch" })
      : null,
    store.settings?.display?.weather_panel !== undefined
      ? f({ path: "display.weather_panel", label: "Weather on screen", hint: "Now and the next two days, in place of the radio panel while the radio is off. Weather data by Open-Meteo.com (CC BY 4.0)", type: "switch" })
      : null,
    store.settings?.display?.speed_limit !== undefined
      ? f({ path: "display.speed_limit", label: "Speed limit sign", hint: "Next to the speedometer, from the offline OpenStreetMap road data (tools/speedlimits.sh); blinks when you drive faster. © OpenStreetMap contributors", type: "switch" })
      : null,
    store.settings?.display?.wind_on_map !== undefined
      ? f({ path: "display.wind_on_map", label: "Wind around the vehicle", hint: "Animated streaks on the map show where the wind blows, relative to the vehicle", type: "switch" })
      : null,
    f({ path: "display.mirror_x", label: "Mirror screen left-right", hint: "For monitors that mirror the picture themselves (rear-view CRTs like the WAECO RV-50). Affects only the physical screen, not this preview", type: "switch" }),
    f({ path: "display.trail_seconds", label: "Trail length", type: "number", min: 0, max: 600, step: 10, unit: "s" }),
    // until the device software has the radio (docs/contracts/radio.md) the setting does not exist: no switch
    store.settings?.display?.radio_panel !== undefined
      ? f({ path: "display.radio_panel", label: "Radio on screen", hint: "While the radio plays, the station and track are shown on the car screen", type: "switch" })
      : null,
  );

  root.append(
    h(".page-head", null, h("h1", null, "Navigation")),
    orientCard, forceCard, zoomCard,
    h(".grid.g2", null, thrCard, layersCard),
    h(".grid.g2", null, cacheCard, dispCard),
    form.bar,
  );
  return () => { form.destroy(); offS(); stopStats(); };
}
