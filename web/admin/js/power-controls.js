// Power stations' switches on the Dashboard (docs/contracts/power.md «Управление»): slide-to-switch only.
// Fossibot — AC / DC / USB / light; Daly powerbank — charge / discharge MOSFET; lead battery — charger auto / manual.
// State comes from telemetry.power (outputs, charger_mode); after a slide the switch shows the wanted state until
// telemetry agrees (the station is read 1.5 s after a command) or EXPECT_MS passes.
import { h } from "./dom.js";
import { api, errText } from "./api.js";
import { toast } from "./ui.js";
import { shownState, slideSwitch } from "./slide-switch.js";

const EXPECT_MS = 8000;

const GROUPS = [
  { slot: "main", title: "Fossibot", items: [["ac", "AC 230 V"], ["dc", "DC 12 V"], ["usb", "USB"], ["light", "Light"]] },
  { slot: "second", title: "Powerbank", items: [["charge", "Charge"], ["discharge", "Discharge"]] },
];

export function powerControls() {
  const expect = new Map(); // "slot:output" → {on, until}
  const sw = new Map();
  const send = async (slot, body, key, want) => {
    expect.set(key, { on: want, until: Date.now() + EXPECT_MS });
    try {
      await api.post(`/api/power/${slot}/control`, body);
    } catch (e) {
      expect.delete(key);
      toast("error", errText(e));
    }
  };

  const groups = GROUPS.map((g) => {
    const row = h(".pc-row");
    for (const [out, label] of g.items) {
      const key = `${g.slot}:${out}`;
      const s = slideSwitch({ label, onslide: (on) => send(g.slot, { output: out, on }, key, on) });
      sw.set(key, s);
      row.append(s);
    }
    return h(".pc-grp", null, h(".pc-title", null, g.title), row);
  });

  // lead battery: «Auto» (charger follows the 12 V input) and, when not auto, the charger by hand
  let leadMode = null;
  let leadOn = false;
  const autoSw = slideSwitch({ label: "Auto (by 12 V input)", onslide: (on) => {
    const mode = on ? "auto" : leadOn ? "on" : "off"; // leaving auto keeps the charger as it is now
    return send("third", { output: "charger", mode }, "third:auto", on);
  } });
  const chgSw = slideSwitch({ label: "Charger", onslide: (on) => send("third", { output: "charger", mode: on ? "on" : "off" }, "third:charger", on) });
  groups.push(h(".pc-grp", null, h(".pc-title", null, "Lead battery"), h(".pc-row", null, autoSw, chgSw)));

  const el = h(".pc", null, ...groups);

  el.render = (power) => {
    const now = Date.now();
    for (const g of GROUPS) {
      const slot = power?.[g.slot] || {};
      const outs = slot.outputs;
      for (const [out] of g.items) {
        const key = `${g.slot}:${out}`;
        const reported = outs ? !!outs[out] : false;
        sw.get(key).set({ on: shownState(reported, expect.get(key), now), disabled: !outs, unknown: !outs });
      }
    }
    const t = power?.third || {};
    leadMode = t.charger_mode ?? null;
    leadOn = !!t.outputs?.charger;
    const known = leadMode !== null;
    const auto = shownState(leadMode === "auto", expect.get("third:auto"), now);
    autoSw.set({ on: auto, disabled: !known, unknown: !known });
    chgSw.set({ on: shownState(leadOn, expect.get("third:charger"), now), disabled: !known || auto, unknown: !known });
  };
  return el;
}
