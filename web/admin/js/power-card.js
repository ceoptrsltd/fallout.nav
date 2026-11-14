// Power stations card (Bluetooth page): BLE addresses and live readings (docs/contracts/power.md).
// Main — Fossibot (charge, % large on screen), second — Daly BMS, third — lead battery via the FNAV-LEAD module.
import { h, clear } from "./dom.js";
import { api, errText } from "./api.js";
import { store, on, applySettings } from "./store.js";
import { toast, switchEl } from "./ui.js";

const SLOTS = [
  { slot: "main", key: "main_mac", title: "Main — Fossibot", hint: "Charge, % — large on the screen" },
  { slot: "second", key: "second_mac", title: "Second — Daly BMS", hint: "Charge and voltage — middle column on the screen" },
  { slot: "third", key: "third_mac", title: "Third — lead battery (FNAV-LEAD)", hint: "XIAO ESP32-C6 module: charge (by voltage), voltage, charger — right column" },
];
const MAC_RE = /^([0-9A-F]{2}:){5}[0-9A-F]{2}$/;

const n1 = (v) => (Number.isFinite(v) ? (Math.round(v * 10) / 10).toString() : "—");
const mins = (m) => (Number.isFinite(m) && m > 0 ? `${Math.floor(m / 60)} h ${m % 60} min` : "—");

/** Readings as key/value pairs for the kind of station. */
export function readingRows(kind, r) {
  if (!r) return [];
  if (kind === "fossibot") {
    return [
      ["Charge", `${n1(r.soc_pct)} %`],
      ["Input", `${r.input_w} W (AC ${r.ac_in_w} W, DC ${r.dc_in_w} W)`],
      ["Output", `${r.output_w} W (AC ${r.ac_out_w} W, USB ${n1(r.usb_w)} W)`],
      ["AC out", `${n1(r.ac_out_v)} V, ${n1(r.ac_out_hz)} Hz`],
      ["To empty / full", `${mins(r.to_empty_min)} / ${mins(r.to_full_min)}`],
      ["Battery", `${n1(r.capacity_ah)} Ah`],
    ];
  }
  if (kind === "lead") {
    return [
      ["Battery", `${n1(r.voltage_v)} V (≈ ${n1(r.soc_pct)} %)`],
      ["12 V input", `${n1(r.input_v)} V — ${r.input_on ? "present" : "none"}`],
      ["Charger", r.charging ? "on" : "off"],
    ];
  }
  const cells = r.cells_mv || [];
  return [
    ["Voltage", `${n1(r.voltage_v)} V`],
    ["Charge (BMS)", `${n1(r.soc_pct)} %`],
    ["Current", `${n1(r.current_a)} A`],
    ["Cells", cells.length ? `${cells.map((c) => (c / 1000).toFixed(3)).join(" ")} V (Δ ${r.delta_mv} mV)` : "—"],
    ["Temperature", (r.temps_c || []).length ? r.temps_c.map((t) => `${t} °C`).join(", ") : "—"],
  ];
}

export function powerCard() {
  let stopped = false;
  let st = null;
  let timer = null;
  const body = h(".pw-body");
  const setBox = h(".bt-set");
  const inputs = {};

  async function save(key, value) {
    try { applySettings(await api.patch("/api/settings", { power: { [key]: value } })); toast("ok", "Saved"); load(); }
    catch (e) { toast("error", errText(e)); renderSettings(store.settings); }
  }

  function renderSettings(s) {
    const p = s?.power;
    if (!p) return;
    clear(setBox).append(h(".bt-set-row", null, switchEl({ checked: !!p.show_on_screen, label: "Show on the screen", onchange: (v) => save("show_on_screen", v) })));
    for (const { key } of SLOTS) if (inputs[key] && document.activeElement !== inputs[key]) inputs[key].value = p[key] || "";
  }

  function stationBlock(def, s) {
    const inp = inputs[def.key] ??= h("input", { type: "text", spellcheck: "false", placeholder: "AA:BB:CC:DD:EE:FF", style: { width: "190px", fontFamily: "var(--mono)" }, "aria-label": `${def.title} address` });
    if (!inp.value && store.settings?.power) inp.value = store.settings.power[def.key] || "";
    const apply = () => {
      const v = inp.value.trim().toUpperCase().replace(/-/g, ":");
      if (v && !MAC_RE.test(v)) { toast("error", "Bluetooth address looks like AA:BB:CC:DD:EE:FF"); return; }
      save(def.key, v);
    };
    let state;
    if (!s || !s.mac) state = h("span.dim", null, "not set — shown as dashes");
    else if (s.connected && s.age_s != null) state = h("span.green", null, `connected · read ${Math.round(s.age_s)} s ago`);
    else if (s.connected) state = h("span.amber", null, "connected, waiting for data");
    else state = h("span.amber", null, "connecting…" + (s.error ? ` (${s.error})` : ""));
    const kv = h(".kv", { style: { marginTop: "8px" } });
    for (const [k, v] of readingRows(s?.kind, s?.reading)) kv.append(h("span.k", null, k), h("span.v", null, v));
    return h(".pw-st", { style: { marginBottom: "14px" } },
      h("h3", { style: { margin: "0 0 4px" } }, def.title),
      h(".hint", null, def.hint),
      h("div", { style: { display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap", marginTop: "6px" } },
        inp, h("button.btn.sm", { type: "button", onclick: apply }, "Save"), state),
      kv);
  }

  function render() {
    clear(body);
    for (const def of SLOTS) body.append(stationBlock(def, st?.stations?.find((x) => x.slot === def.slot)));
    body.append(h(".hint", null, "Read-only: nothing is ever written to the stations. "
      + "The Daly BMS accepts one connection — while the device holds it, the Daly phone app cannot connect."));
  }

  async function load() {
    try { st = await api.get("/api/power"); }
    catch (e) { if (e.status !== 404) toast("error", "Power stations: " + errText(e)); st = null; }
    if (!stopped) render();
  }
  function schedule() {
    clearTimeout(timer);
    if (stopped) return;
    timer = setTimeout(async () => { if (!document.hidden) await load(); schedule(); }, 5000);
  }

  const offSet = on("settings", renderSettings);
  renderSettings(store.settings);
  render();
  load().then(schedule);

  const el = h(".card", null, h("h2", null, "Power stations"), setBox, body);
  el.cleanup = () => { stopped = true; clearTimeout(timer); offSet(); };
  return el;
}
