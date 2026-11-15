// Power stations card (Bluetooth page): BLE addresses and live readings (docs/contracts/power.md).
// Main — Fossibot (charge, % large on screen), second — Daly BMS, third — the lead battery heard from the Victron
// solar charger's broadcasts (no connection, so no «Release»).
// A station holds one Bluetooth connection: «Release» lets it go for power.release_min so its own phone app can see it.
import { h, clear } from "./dom.js";
import { api, errText } from "./api.js";
import { store, on, applySettings } from "./store.js";
import { toast, switchEl } from "./ui.js";

const SLOTS = [
  { slot: "main", key: "main_mac", title: "Main — Fossibot", hint: "Charge, % — large on the screen" },
  { slot: "second", key: "second_mac", title: "Second — Daly BMS", hint: "Charge and voltage — middle column on the screen" },
  { slot: "third", key: "victron_mac", title: "Third — lead battery (Victron SmartSolar)", listen: true,
    hint: "Heard from the solar charger's broadcasts: voltage, charge (estimated from the resting voltage), charging power — right column" },
];
const MAC_RE = /^([0-9A-F]{2}:){5}[0-9A-F]{2}$/;

/** «12 min» / «45 s» — how long a released station stays released. */
export function releaseLeft(s) {
  if (!Number.isFinite(s) || s <= 0) return "";
  return s >= 60 ? `${Math.ceil(s / 60)} min` : `${Math.ceil(s)} s`;
}

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
  if (kind === "victron") {
    return [
      ["Battery", `${n1(r.voltage_v)} V (≈ ${n1(r.soc_pct)} %)`],
      ["Charger", `${String(r.state || "").replace(/_/g, " ")}${r.error ? ` · error ${r.error}` : ""}`],
      ["Charging", `${n1(r.current_a)} A · panels ${r.pv_power_w ?? "—"} W`],
      ["Yield today", `${r.yield_today_wh ?? "—"} Wh`],
      ["Load", `${n1(r.load_a)} A`],
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

  async function release(slot, minutes) {
    try { st = await api.post(`/api/power/${slot}/release`, minutes === undefined ? {} : { minutes }); render(); }
    catch (e) { toast("error", errText(e)); }
  }

  async function save(key, value) {
    try { applySettings(await api.patch("/api/settings", { power: { [key]: value } })); toast("ok", "Saved"); load(); }
    catch (e) { toast("error", errText(e)); renderSettings(store.settings); }
  }

  function renderSettings(s) {
    const p = s?.power;
    if (!p) return;
    clear(setBox).append(
      h(".bt-set-row", null, switchEl({ checked: !!p.show_on_screen, label: "Show on the screen", onchange: (v) => save("show_on_screen", v) })),
      h(".bt-set-row", null, switchEl({ checked: p.light_on_alarm !== false, label: "Flash the Fossibot light when the alarm goes off", onchange: (v) => save("light_on_alarm", v) }),
        h(".hint", null, "For as long as the hazard lights flash (Alarm → hazard seconds); then the light is put back as it was")),
      releaseRow(p),
    );
    for (const { key } of SLOTS) if (inputs[key] && document.activeElement !== inputs[key]) inputs[key].value = p[key] || "";
  }

  // how long «Release» lets a station go (power.release_min)
  const relInp = h("input", { type: "number", min: 1, max: 240, step: 1, inputmode: "numeric", style: { width: "70px" },
    "aria-label": "Release for, minutes" });
  relInp.addEventListener("change", () => {
    const v = Number(relInp.value);
    if (!Number.isInteger(v) || v < 1 || v > 240) { toast("error", "1 to 240 minutes"); renderSettings(store.settings); return; }
    save("release_min", v);
  });
  function releaseRow(p) {
    if (document.activeElement !== relInp) relInp.value = p.release_min ?? 15;
    return h(".bt-set-row", null,
      h("label", { style: { display: "flex", gap: "8px", alignItems: "center" } }, "Release a station for its phone app for", relInp, "min"),
      h(".hint", { style: { marginLeft: 0, marginBottom: "16px" } }, "A station takes one Bluetooth connection: while the device holds it, the station's own app cannot see it. "
        + "«Release» below disconnects for this long, then the device connects again by itself."));
  }

  // the Victron key (VictronConnect → Product info → Instant readout details → Encryption data): shown masked
  const keyInp = h("input", { type: "password", spellcheck: "false", autocomplete: "off", placeholder: "32 hex characters",
    style: { width: "190px", fontFamily: "var(--mono)" }, "aria-label": "Victron encryption key" });
  function keyRow() {
    if (!keyInp.value && document.activeElement !== keyInp) keyInp.value = store.settings?.power?.victron_key || "";
    return h("div", { style: { display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap", marginTop: "6px" } },
      keyInp, h("button.btn.sm", { type: "button", onclick: () => {
        const v = keyInp.value.trim().toLowerCase();
        if (v && !/^[0-9a-f]{32}$/.test(v)) { toast("error", "The key is 32 hex characters"); return; }
        save("victron_key", v);
      } }, "Save key"),
      h("span.hint", { style: { margin: 0 } }, "VictronConnect: Product info, Instant readout"));
  }

  function stationBlock(def, s) {
    const inp = inputs[def.key] ??= h("input", { type: "text", spellcheck: "false", placeholder: "AA:BB:CC:DD:EE:FF", style: { width: "190px", fontFamily: "var(--mono)" }, "aria-label": `${def.title} address` });
    if (!inp.value && store.settings?.power) inp.value = store.settings.power[def.key] || "";
    const apply = () => {
      const v = inp.value.trim().toUpperCase().replace(/-/g, ":");
      if (v && !MAC_RE.test(v)) { toast("error", "Bluetooth address looks like AA:BB:CC:DD:EE:FF"); return; }
      save(def.key, v);
    };
    const relMin = store.settings?.power?.release_min ?? 15;
    const released = !!(s?.mac && s.released_s);
    let state;
    if (released) state = h("span.amber", null, `released for the phone app · back in ${releaseLeft(s.released_s)}`);
    else if (!s || !s.mac) state = h("span.dim", null, "not set — shown as dashes");
    else if (def.listen && s.connected) state = h("span.green", null, `listening · heard ${Math.round(s.age_s)} s ago`);
    else if (def.listen) state = h("span.amber", null, s.error || "listening, nothing heard yet");
    else if (s.connected && s.age_s != null) state = h("span.green", null, `connected · read ${Math.round(s.age_s)} s ago`);
    else if (s.connected) state = h("span.amber", null, "connected, waiting for data");
    else state = h("span.amber", null, "connecting…" + (s.error ? ` (${s.error})` : ""));
    // a station takes one connection: let it go for a while so its own phone app can see it
    const relBtn = !s?.mac || def.listen ? null : released
      ? h("button.btn.sm", { type: "button", onclick: () => release(def.slot, 0) }, "Reconnect now")
      : h("button.btn.sm", { type: "button", title: "Disconnect so the station's own phone app can connect",
        onclick: () => release(def.slot) }, `Release for ${relMin} min`);
    const kv = h(".kv", { style: { marginTop: "8px" } });
    for (const [k, v] of released ? [] : readingRows(s?.kind, s?.reading)) kv.append(h("span.k", null, k), h("span.v", null, v));
    return h(".pw-st", { style: { marginBottom: "14px" } },
      h("h3", { style: { margin: "0 0 4px" } }, def.title),
      h(".hint", null, def.hint),
      h("div", { style: { display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap", marginTop: "6px" } },
        inp, h("button.btn.sm", { type: "button", onclick: apply }, "Save")),
      def.listen ? keyRow() : null,
      h("div", { style: { display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap", marginTop: "6px" } }, state, relBtn),
      kv);
  }

  function render() {
    clear(body);
    for (const def of SLOTS) body.append(stationBlock(def, st?.stations?.find((x) => x.slot === def.slot)));
    body.append(h(".hint", null, "The device only writes what the Dashboard switches ask for (and the Fossibot light on alarm)."));
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
