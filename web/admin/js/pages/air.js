// Air (docs/contracts/air.md): the Qingping air quality sensor — what it says now and the last 7 days. One chart
// per value: the line is the average, the band behind it the min…max of every 15 minutes; the colour is the zone
// (green — fine, amber — raised, red — off the scale). The sensor is only listened to (Bluetooth LE broadcasts).
import { h, clear, put, setCls } from "../dom.js";
import { api, errText } from "../api.js";
import { store, on, applySettings } from "../store.js";
import { toast, switchEl } from "../ui.js";
import { METRICS, fmtAge, fmtValue, levelOf, seriesStats } from "../air-fmt.js";
import { weekChart } from "../air-chart.js";

const HOURS = 168;
const REFRESH_MS = 5 * 60 * 1000;
const MAC_RE = /^([0-9A-F]{2}:){5}[0-9A-F]{2}$/;

export function mount(root) {
  root.classList.add("air-page");
  let stopped = false;
  let hist = null;
  let timer = null;
  const cards = new Map(); // key → {now, unit, stats, box}

  const age = h("span.dim");
  const head = h(".air-head", null, h("h1", null, "Air"), age);
  const list = h(".air-list");
  for (const m of METRICS) {
    const now = h("span.air-now", null, "—");
    const stats = h(".air-stats");
    const box = h(".air-box");
    cards.set(m.key, { now, stats, box });
    list.append(h(".card.air-card", null,
      h(".air-card-head", null,
        h("div", null, h("h2", null, m.label), h(".hint", null, m.hint)),
        h(".air-val", null, now, h("span.air-unit", null, m.unit))),
      box, stats));
  }

  function renderNow() {
    const a = store.online ? store.telemetry?.air : null;
    put(age, a ? `sensor heard ${fmtAge(a.age_s)}` + (a.battery_pct != null ? ` · battery ${a.battery_pct} %` : "") : "");
    for (const m of METRICS) {
      const c = cards.get(m.key);
      const v = a?.[m.key];
      put(c.now, fmtValue(m, v));
      setCls(c.now, "air-now " + levelOf(m, v));
    }
  }

  function renderCharts() {
    for (const m of METRICS) {
      const c = cards.get(m.key);
      const width = Math.max(240, Math.floor(c.box.clientWidth || list.clientWidth - 30));
      clear(c.box).append(weekChart(m, hist, width));
      const st = seriesStats(hist?.[m.key]);
      clear(c.stats);
      if (!st) continue;
      for (const [k, v] of [["min", st.min], ["avg", st.avg], ["max", st.max]]) {
        c.stats.append(h("span.air-stat", null, h("span.k", null, k), h("b", { class: levelOf(m, v) }, fmtValue(m, v)), h("span.u", null, m.unit)));
      }
      c.stats.append(h("span.air-stat.dim", null, "last 7 days"));
    }
  }

  async function load() {
    try { hist = await api.get(`/api/air/history?hours=${HOURS}`); }
    catch (e) { if (!stopped) toast("error", "Air history: " + errText(e)); }
    if (!stopped) renderCharts();
  }

  // ───── settings ─────
  const macInp = h("input", { type: "text", spellcheck: "false", placeholder: "AA:BB:CC:DD:EE:FF", style: { width: "190px", fontFamily: "var(--mono)" }, "aria-label": "Sensor address" });
  const setBox = h(".bt-set");
  async function save(patch) {
    try { applySettings(await api.patch("/api/settings", { air: patch })); toast("ok", "Saved"); }
    catch (e) { toast("error", errText(e)); renderSettings(store.settings); }
  }
  function renderSettings(s) {
    const a = s?.air;
    if (!a) return;
    if (document.activeElement !== macInp) macInp.value = a.mac || "";
    clear(setBox).append(
      h(".bt-set-row", null, switchEl({ checked: a.enabled !== false, label: "Listen to the air sensor", onchange: (v) => save({ enabled: v }) })),
      h("div", { style: { display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap", marginTop: "8px" } },
        macInp, h("button.btn.sm", { type: "button", onclick: () => {
          const v = macInp.value.trim().toUpperCase().replace(/-/g, ":");
          if (v && !MAC_RE.test(v)) { toast("error", "Bluetooth address looks like AA:BB:CC:DD:EE:FF"); return; }
          save({ mac: v });
        } }, "Save")),
      h(".hint", { style: { marginLeft: 0, marginTop: "6px" } }, "Qingping Air Monitor Lite. It broadcasts its readings over Bluetooth: nothing is connected, "
        + "its own phone app keeps working. The device listens for a few seconds every minute (every three minutes while "
        + "music plays), so that is how often a new reading arrives."));
  }
  const settings = h(".card", null, h("h2", null, "Sensor"), setBox);

  root.append(head, list, settings);

  const offT = on("telemetry", renderNow);
  const offS = on("settings", renderSettings);
  const ro = typeof ResizeObserver === "function" ? new ResizeObserver(() => { if (hist) renderCharts(); }) : null;
  ro?.observe(list);
  renderNow();
  renderSettings(store.settings);
  renderCharts();
  load();
  timer = setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS);

  return () => { stopped = true; clearInterval(timer); ro?.disconnect(); offT(); offS(); };
}
