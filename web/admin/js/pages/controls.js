// Controls: the macro pad layout (editable: settings.keypad), the gamepad and the GPS-module button.
// Actions, labels and defaults come from the device (GET /api/controls, fnav/core/keypad.py).
// Every key and the knob press can have a second action on hold (settings.keypad.<place>_hold).
import { h, clear } from "../dom.js";
import { api, errText } from "../api.js";
import { poll, badge, banner, toast, confirmDialog } from "../ui.js";
import { store, on, applySettings } from "../store.js";

const KNOB_SYM = { knob_left: "↺", knob_right: "↻", knob_press: "●" };

function table(rows, first) {
  return h(".tbl-wrap", null, h("table.ctl-tbl", null,
    h("thead", null, h("tr", null, h("th", null, first), h("th", null, "Action"), h("th", null, ""))),
    h("tbody", null, rows.map((r) => h("tr", null,
      h("td.ctl-key", null, r.control), h("td", null, h("b", null, r.title)), h("td.dim", null, r.hint || "")))),
  ));
}

export function mount(root) {
  let stopped = false;
  let info = null; // the keypad part of /api/controls
  let bindings = null; // place (and place_hold) → action id
  const padBadge = h("span");
  const gpBadge = h("span");
  const padBox = h("div", null, h(".dim", null, "Loading…"));
  const gpBox = h("div");
  const btnBox = h("div");
  const notes = h("div");
  const resetBtn = h("button.btn.sm", { type: "button", hidden: true, onclick: () => reset() }, "Reset to default");

  const conn = (on_) => badge(on_ ? "connected" : "not connected", on_ ? "ok" : "");
  const action = (id) => (info.actions.find((a) => a.id === id) || { id, title: id, hint: "" });

  async function save(patch) {
    try {
      applySettings(await api.patch("/api/settings", { keypad: patch }));
      toast("ok", "Saved");
    } catch (e) {
      toast("error", errText(e));
    }
    renderPad();
  }

  async function reset() {
    if (!(await confirmDialog({ title: "Reset the macro pad layout?", text: "Every key gets its default action again.", okText: "Reset" }))) return;
    save(info.defaults);
  }

  function renderPad() {
    if (!info) return;
    const fromSettings = store.settings && store.settings.keypad;
    bindings = {
      ...Object.fromEntries(info.places.map((p) => [p.place, p.action])),
      ...Object.fromEntries(info.places.filter((p) => p.hold !== null && p.hold !== undefined).map((p) => [p.place + "_hold", p.hold])),
      ...(fromSettings || {}),
    };
    resetBtn.hidden = Object.keys(info.defaults).every((k) => bindings[k] === undefined || bindings[k] === info.defaults[k]);
    const holdOf = (place) => {
      const id = bindings[place + "_hold"];
      return id && id !== "none" ? action(id) : null;
    };
    const tile = (n) => {
      const a = action(bindings["key" + n]);
      const hold = holdOf("key" + n);
      return h(".pad-key" + (a.id === "none" && !hold ? ".off" : ""), null, h(".pad-num", null, n),
        h(".pad-title", null, a.title),
        hold ? h(".pad-hold", null, h("b", null, "hold: "), hold.title) : null);
    };
    const knob = (place, cls) => {
      const hold = place === "knob_press" ? holdOf(place) : null;
      return h(".pad-knob-lbl." + cls, null, h("span.pad-sym", null, KNOB_SYM[place]), " ", action(bindings[place]).title,
        hold ? h("span.pad-hold", null, ", hold: ", hold.title) : null);
    };
    const select = (key) => {
      const sel = h("select", { onchange: () => save({ [key]: sel.value }) },
        info.actions.map((a) => h("option", { value: a.id, selected: a.id === bindings[key] }, a.title)));
      return sel;
    };
    const rows = info.places.map((p) => {
      const canHold = p.hold !== null && p.hold !== undefined;
      const hold = canHold ? holdOf(p.place) : null;
      const hint = action(bindings[p.place]).hint;
      return h("tr", null,
        h("td.ctl-key", null, p.label),
        h("td", null, select(p.place)),
        h("td", null, canHold ? select(p.place + "_hold") : h("span.dim", null, "—")),
        h("td.dim", null, hold ? [hint, hint ? h("br") : null, "Hold: " + (hold.hint || hold.title)] : hint));
    });
    clear(padBox).append(
      h(".pad", null,
        h(".pad-keys", null, [1, 2, 3, 4, 5, 6].map(tile)),
        h(".pad-knob-wrap", null,
          knob("knob_left", "left"), h(".pad-knob", null, h("span", null, "KNOB")), knob("knob_right", "right"),
          knob("knob_press", "press"))),
      h(".tbl-wrap", null, h("table.ctl-tbl", null,
        h("thead", null, h("tr", null, h("th", null, "Key"), h("th", null, "Press"), h("th", null, "Hold"), h("th", null, ""))),
        h("tbody", null, rows))),
      h(".hint", null, "Changes apply at once, the pad itself is not reprogrammed. Several keys may share an action."),
      h(".hint", null, `A key with a hold action does its press action on release; held for ${info.hold_s || 0.8} s it does the hold action instead. Keys without one react at once.`),
    );
  }

  async function load() {
    let d;
    try {
      d = await api.get("/api/controls");
    } catch (e) {
      if (!stopped && !info) clear(padBox).append(banner("bad", "Cannot read the controls from the device: " + errText(e)));
      return;
    }
    if (stopped) return;
    clear(padBadge).append(conn(d.keypad.connected));
    clear(gpBadge).append(conn(d.gamepad.connected));
    if (info) return;
    info = d.keypad;
    renderPad();
    clear(gpBox).append(table(d.gamepad.controls, "Button"));
    clear(btnBox).append(table(d.button.controls, "Press"));
    clear(notes).append(...(d.notes || []).map((t) => h(".hint", null, t)));
  }
  const stopPoll = poll(load, 5000);
  const offSet = on("settings", () => renderPad()); // changed from another tab / the device

  root.append(
    h(".page-head", null, h("h1", null, "Controls")),
    h(".grid", null,
      h(".card", null, h(".card-head", null, h("h2", null, "Macro pad: 6 keys + knob"), resetBtn, padBadge), padBox),
      h(".card", null, h(".card-head", null, h("h2", null, "Gamepad"), gpBadge), gpBox),
      h(".card", null, h("h2", null, "Button on the GPS module"), btnBox, notes)),
  );
  return () => { stopped = true; stopPoll(); offSet(); };
}
