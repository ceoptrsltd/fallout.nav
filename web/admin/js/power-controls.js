// Power stations' switches on the Dashboard (docs/contracts/power.md «Управление»): slide-to-switch only.
// Fossibot — AC / DC / USB / light; Daly powerbank — charge / discharge MOSFET; lead battery — charger auto / manual.
// State comes from telemetry.power (outputs, charger_mode); after a slide the switch shows the wanted state until
// telemetry agrees (the station is read 1 s after a command) or EXPECT_MS passes. A station with no data (released
// for its phone app or not connected) shows «Reconnect now» instead of its greyed-out switches.
// Fossibot and powerbank switches also take a timer (slide-switch.js): the thumb let go in the middle of the track —
// POST …/control {output, on, in_min}; the device keeps the timer (telemetry.power.<slot>.timers) and switches itself.
import { h } from "./dom.js";
import { api, errText } from "./api.js";
import { toast } from "./ui.js";
import { shownState, slideSwitch } from "./slide-switch.js";

const EXPECT_MS = 8000;
const TIMER_EXPECT_MS = 4000; // a timer just set or removed is shown at once, until telemetry carries it

const GROUPS = [
  { slot: "main", title: "Fossibot", items: [["ac", "AC 230 V"], ["dc", "DC 12 V"], ["usb", "USB"], ["light", "Light"]] },
  { slot: "second", title: "Powerbank", items: [["charge", "Charge"], ["discharge", "Discharge"]] },
];

export function powerControls() {
  const expect = new Map(); // "slot:output" → {on, until}
  const sw = new Map();
  const tExpect = new Map(); // "slot:output" → {timer: {on, in_s} | null, until}
  const setTimer = async (slot, out, key, on, minutes) => {
    const timer = minutes ? { on, in_s: minutes * 60 } : null;
    tExpect.set(key, { timer, until: Date.now() + TIMER_EXPECT_MS });
    sw.get(key).set({ timer });
    try {
      await api.post(`/api/power/${slot}/control`, { output: out, on, in_min: minutes });
    } catch (e) {
      tExpect.delete(key);
      toast("error", errText(e));
    }
  };
  const send = async (slot, body, key, want) => {
    expect.set(key, { on: want, until: Date.now() + EXPECT_MS });
    try {
      await api.post(`/api/power/${slot}/control`, body);
    } catch (e) {
      expect.delete(key);
      toast("error", errText(e));
    }
  };

  // «Reconnect now»: POST …/release {minutes: 0} — ends a release or the pause between failed tries
  const reconnect = (slot) => {
    const b = h("button.btn.sm.pc-reconnect", { type: "button" }, "Reconnect now");
    b.addEventListener("click", async () => {
      b.disabled = true;
      b.textContent = "Connecting…";
      try { await api.post(`/api/power/${slot}/release`, { minutes: 0 }); }
      catch (e) { toast("error", errText(e)); }
      setTimeout(() => { b.disabled = false; b.textContent = "Reconnect now"; }, 15000);
    });
    return b;
  };
  const rc = new Map();
  const state = (slot, row, power) => {
    const btn = rc.get(slot);
    const x = power?.[slot];
    const away = !!power && !!x && (x.released_s > 0 || x.soc_pct == null && x.voltage_v == null);
    row.hidden = away;
    btn.hidden = !away;
    const note = btn.previousElementSibling;
    note.textContent = x?.released_s > 0 ? `Released for the phone app · back in ${x.released_s >= 60 ? `${Math.ceil(x.released_s / 60)} min` : `${x.released_s} s`}` : "Not connected";
    note.hidden = !away;
  };
  const rows = {};

  const groups = GROUPS.map((g) => {
    const row = h(".pc-row");
    rows[g.slot] = row;
    rc.set(g.slot, reconnect(g.slot));
    for (const [out, label] of g.items) {
      const key = `${g.slot}:${out}`;
      const s = slideSwitch({ label,
        onslide: (on) => { // switched by hand: the device drops this output's timer
          tExpect.set(key, { timer: null, until: Date.now() + TIMER_EXPECT_MS });
          return send(g.slot, { output: out, on }, key, on);
        },
        ontimer: (on, minutes) => setTimer(g.slot, out, key, on, minutes) });
      sw.set(key, s);
      row.append(s);
    }
    return h(".pc-grp", null, h(".pc-title", null, g.title), row, h("span.pc-away", { hidden: true }), rc.get(g.slot));
  });

  // lead battery: «Auto» (charger follows the 12 V input) and, when not auto, the charger by hand
  let leadMode = null;
  let leadOn = false;
  const autoSw = slideSwitch({ label: "Auto (by 12 V input)", onslide: (on) => {
    const mode = on ? "auto" : leadOn ? "on" : "off"; // leaving auto keeps the charger as it is now
    return send("third", { output: "charger", mode }, "third:auto", on);
  } });
  const chgSw = slideSwitch({ label: "Charger", onslide: (on) => send("third", { output: "charger", mode: on ? "on" : "off" }, "third:charger", on) });
  rows.third = h(".pc-row", null, autoSw, chgSw);
  rc.set("third", reconnect("third"));
  // the FNAV-LEAD module's charger (firmware/lead-battery) — not in use now: the third station is the battery heard
  // from the Victron charger, which has nothing to switch. The group shows up again with a station that has a
  // charger mode (the future fourth one).
  const leadGrp = h(".pc-grp", { hidden: true }, h(".pc-title", null, "Lead battery"), rows.third, h("span.pc-away", { hidden: true }), rc.get("third"));
  groups.push(leadGrp);

  const el = h(".pc", null, ...groups);

  el.render = (power) => {
    const now = Date.now();
    for (const g of GROUPS) {
      const slot = power?.[g.slot] || {};
      const outs = slot.outputs;
      for (const [out] of g.items) {
        const key = `${g.slot}:${out}`;
        const reported = outs ? !!outs[out] : false;
        const te = tExpect.get(key);
        const timer = te && now < te.until ? te.timer : slot.timers?.[out] || null;
        sw.get(key).set({ on: shownState(reported, expect.get(key), now), disabled: !outs, unknown: !outs, timer });
      }
    }
    const t = power?.third || {};
    leadMode = t.charger_mode ?? null;
    leadOn = !!t.outputs?.charger;
    const known = leadMode !== null;
    const auto = shownState(leadMode === "auto", expect.get("third:auto"), now);
    autoSw.set({ on: auto, disabled: !known, unknown: !known });
    chgSw.set({ on: shownState(leadOn, expect.get("third:charger"), now), disabled: !known || auto, unknown: !known });
    leadGrp.hidden = !known;
    for (const slot of ["main", "second"]) state(slot, rows[slot], power);
  };
  return el;
}
