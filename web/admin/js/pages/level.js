// Level (docs/contracts/level.md): live pitch and roll (telemetry `level`), the raw sensor vector and stillness
// (GET api/level, polled while the page is open), the calibration wizard — level: precise two-point or single on
// level ground; forward: the board axis or learned while driving — and settings.level. The preview is the car
// screen's own widget (web/display/ui/level.js), drawn into a canvas here.
import { h, icon, clear } from "../dom.js";
import { api } from "../api.js";
import { SettingsForm } from "../form.js";
import { store, on } from "../store.js";
import { toast, banner, badge, confirmDialog, poll } from "../ui.js";
import {
  AXES, MEASURE_S, LEARN_NEED_S, REF_MAX_DEG, fmtDeg, fmtSigned, pitchText, rollText, levelBadge, fmtVec, fmtCm,
  calibSummary, learnFrac, levelErrorText, signMeaning, parseDeg, toggleSign, degError, hasLevelRef,
} from "../level-fmt.js";
import { fmtWhen } from "../tracking-fmt.js";

const POLL_MS = 1000;
const PV_SCALE = 1.25; // preview: screen px → CSS px

export function mount(root) {
  root.classList.add("lv-page");
  let stopped = false;
  let st = null; // LevelStatus
  let hud = null; // web/display/ui/index.js: drawLevelPanel, LEVEL_BOX
  let measuring = null; // {t0} — the spirit level measurement (3 s) in progress
  let fwdMsg = null;
  let fwdBusy = false;
  let wasLearning = false;

  const headBadge = h("span");
  const unavail = h("div", { hidden: true });

  /** Live angles: telemetry while it flows, otherwise the last GET. */
  function live() {
    const t = store.online && store.telemetry ? store.telemetry.level : null;
    if (t) return t;
    if (st) return { sensor_ok: st.sensor_ok, calibrated: st.calibrated, pitch_deg: st.pitch_deg, roll_deg: st.roll_deg, wheels_cm: st.wheels_cm };
    return null;
  }

  // ───── live card ─────
  const canvas = h("canvas.lv-canvas", { width: 1, height: 1, "aria-label": "Level widget as on the car screen" });
  const pvBox = h(".lv-pv", null, canvas);
  const pitchV = h("b.mono.lv-ang");
  const pitchW = h("span.dim");
  const rollV = h("b.mono.lv-ang");
  const rollW = h("span.dim");
  const stillV = h("span");
  const rawV = h("span.mono");
  const magV = h("span.mono.dim");
  const wheelEls = Object.fromEntries(["fl", "fr", "rl", "rr"].map((k) => [k, h("b.mono")]));
  const wheelsBox = h(".lv-wheels", null,
    h(".lv-w.fl", null, h("span", null, "Front left"), wheelEls.fl),
    h(".lv-w.fr", null, h("span", null, "Front right"), wheelEls.fr),
    h(".lv-van", null, h("span", null, "▲ front")),
    h(".lv-w.rl", null, h("span", null, "Rear left"), wheelEls.rl),
    h(".lv-w.rr", null, h("span", null, "Rear right"), wheelEls.rr));

  function drawPv() {
    if (!hud) return;
    const l = live();
    const { w, h: hh } = hud.LEVEL_BOX;
    const dpr = Math.min(3, window.devicePixelRatio || 1);
    const k = PV_SCALE * dpr;
    const cw = Math.round(w * k);
    const ch = Math.round(hh * k);
    if (canvas.width !== cw || canvas.height !== ch) {
      canvas.width = cw;
      canvas.height = ch;
      canvas.style.width = Math.round(w * PV_SCALE) + "px";
    }
    const g = canvas.getContext("2d");
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.clearRect(0, 0, cw, ch);
    g.setTransform(k, 0, 0, k, 0, 0);
    try {
      hud.drawLevelPanel(g, 0, 0, l, performance.now() / 1000, { noSensor: !l || l.sensor_ok === false });
    } catch (e) {
      console.error("level preview", e);
      hud = null;
      pvBox.hidden = true;
    }
  }

  function renderLive() {
    const l = live();
    const b = levelBadge(l);
    clear(headBadge).append(badge(b.text, b.kind));
    const ok = l && l.sensor_ok !== false;
    pitchV.textContent = ok ? fmtDeg(l.pitch_deg) : "—";
    pitchW.textContent = ok ? pitchText(l.pitch_deg) : "";
    rollV.textContent = ok ? fmtDeg(l.roll_deg) : "—";
    rollW.textContent = ok ? rollText(l.roll_deg) : "";
    for (const [k, el] of Object.entries(wheelEls)) {
      const v = ok && l.wheels_cm ? l.wheels_cm[k] : null;
      el.textContent = fmtCm(v);
      el.parentElement.classList.toggle("top", typeof v === "number" && Math.round(v * 2) / 2 <= 0);
    }
    drawPv();
  }

  function renderStatusBits() {
    clear(stillV);
    if (!st) stillV.append("—");
    else if (!st.sensor_ok) stillV.append(h("span.red", null, "no sensor"));
    else stillV.append(st.still ? h("span.green", null, "● still") : h("span.amber", null, "● moving or rocking"));
    const v = st ? fmtVec(st.raw) : null;
    rawV.textContent = v ? v.parts.join("  ") : "—";
    magV.textContent = v ? `  |a| ${v.mag}` : "";
  }

  // ───── calibration card ─────
  const calKv = h(".kv.lv-kv");
  const resetBtn = h("button.btn.sm.danger", { type: "button", onclick: () => resetCal() }, icon("trash"), "Reset calibration");
  const uncalNote = h("div");

  // spirit level: two signed angles, 0.1°; the ± button for number pads without a minus key (iPhone)
  const refIn = {};
  const refErr = {};
  let refMsg = null; // {kind, text, entered?, read?} — result of the last save
  const refRow = (axis, label, hint) => {
    const inp = h("input.lv-deg", { type: "text", inputmode: "decimal", autocomplete: "off", spellcheck: "false", placeholder: "0.0", "aria-label": label });
    const pm = h("button.btn.lv-pm", { type: "button", title: "Change the sign (+ / −)", "aria-label": "Change the sign" }, "±");
    const mean = h("span.lv-mean.dim");
    const err = h(".err.lv-referr", { hidden: true });
    const upd = () => {
      const v = parseDeg(inp.value);
      mean.textContent = v === null ? "" : signMeaning(axis, v);
      mean.className = "lv-mean" + (v !== null && Math.abs(v) > REF_MAX_DEG ? " red" : "");
      err.hidden = true;
      inp.classList.remove("invalid");
      if (refMsg) { refMsg = null; renderRefMsg(); }
    };
    inp.addEventListener("input", upd);
    inp.addEventListener("blur", () => { const v = parseDeg(inp.value); if (v !== null) inp.value = String(v); }); // rounded to 0.1
    pm.addEventListener("click", () => { inp.value = toggleSign(inp.value); upd(); inp.focus(); });
    refIn[axis] = inp;
    refErr[axis] = err;
    return h(".lv-ref", null,
      h(".lv-reflab", null, h("b", null, label), h("span.dim", null, hint)),
      h(".lv-refctl", null, pm, inp, h("span.unit", null, "°"), mean),
      err);
  };
  const saveBtn = h("button.btn.primary", { type: "button", onclick: () => saveRef() }, "Save calibration");
  const measBar = h(".progress.lv-meas", { hidden: true }, h("i"));
  const measTxt = h(".hint", { hidden: true }, "Measuring — keep still, nobody inside or moving…");
  const refMsgBox = h("div");
  const stillHint = h(".lv-still");

  function renderRefMsg() {
    clear(refMsgBox);
    const noSensor = st && !st.sensor_ok;
    if (refMsg) {
      refMsgBox.append(banner(refMsg.kind, h("div", null, refMsg.text,
        refMsg.entered ? h(".lv-cmp", null,
          h("span", null, "Entered ", h("b.mono", null, `${fmtSigned(refMsg.entered.pitch)} / ${fmtSigned(refMsg.entered.roll)}`)),
          h("span", null, "the device reads ", h("b.mono", null, `${fmtSigned(refMsg.read.pitch)} / ${fmtSigned(refMsg.read.roll)}`)),
          h("span.dim", null, "(along / across)")) : null)));
    } else if (noSensor) {
      refMsgBox.append(banner("bad", "Level sensor is not connected. It is the alarm's shock sensor (MPU-6050) — see the Alarm page."));
    }
  }

  let axisPick = null; // the user's choice in the select until applied (the poll must not reset it)
  const axisSel = h("select", { "aria-label": "Board axis pointing forward", onchange: () => { axisPick = axisSel.value; } },
    AXES.map((a) => h("option", { value: a.value }, a.label)));
  const axisBtn = h("button.btn", { type: "button", onclick: () => setAxis() }, "Apply");
  const learnBtn = h("button.btn", { type: "button", onclick: () => learn(true) }, icon("track"), "Learn while driving");
  const cancelBtn = h("button.btn.sm", { type: "button", onclick: () => learn(false) }, "Cancel");
  const learnBar = h(".progress.green.lv-learnbar", null, h("i"));
  const learnTxt = h("span.mono.dim");
  const learnBox = h(".lv-learn", { hidden: true },
    h(".lv-learn-row", null, learnBar, cancelBtn), learnTxt,
    h(".hint", null, "Drive normally on straight roads: speed up and brake a few times (above 5 km/h). It finishes by itself."));
  const fwdMsgBox = h("div");

  function renderCal() {
    const cal = st ? st.calibration : null;
    const sum = calibSummary(cal);
    clear(calKv);
    if (sum) {
      calKv.append(
        h("span.k", null, "Level"), h("span.v", null, sum.level),
        h("span.k", null, "Forward"), h("span.v", null, sum.forward),
        h("span.k", null, "Calibrated"), h("span.v", null, cal.ts ? fmtWhen(cal.ts, Date.now() / 1000) : "—"));
    }
    resetBtn.hidden = !cal;
    clear(uncalNote);
    if (st && !hasLevelRef(cal)) {
      uncalNote.append(banner("warn", "Not calibrated: the angles are measured from the sensor board as it is mounted. Set the forward direction, then calibrate with a spirit level (steps 1 and 2)."));
    }

    // spirit level
    const busy = !!measuring;
    const noSensor = st && !st.sensor_ok;
    if (saveBtn.dataset.k !== String(busy)) { // only when it changes: the button keeps its content under the pointer
      saveBtn.dataset.k = String(busy);
      clear(saveBtn);
      if (busy) saveBtn.append(h("span.spin"), " ");
      saveBtn.append(busy ? "Measuring…" : "Save calibration");
    }
    saveBtn.disabled = busy || noSensor;
    for (const k of ["pitch", "roll"]) refIn[k].disabled = busy;
    measBar.hidden = !busy;
    measTxt.hidden = !busy;
    renderRefMsg();
    clear(stillHint);
    if (st && st.sensor_ok && !busy) {
      stillHint.append(st.still ? h("span.green", null, "● The van is still — ready to measure") : h("span.amber", null, "● The van is moving or rocking — wait until it settles"));
    }

    // forward direction (works before the spirit level too)
    const w = (st && st.wizard) || {};
    const learning = !!w.learning;
    axisSel.value = axisPick || (cal && cal.axis) || "x+";
    axisSel.disabled = fwdBusy || learning;
    axisBtn.disabled = fwdBusy || learning;
    learnBtn.disabled = fwdBusy || learning || noSensor;
    learnBtn.hidden = learning;
    learnBox.hidden = !learning;
    if (learning) {
      learnBar.firstChild.style.width = learnFrac(w) * 100 + "%";
      learnTxt.textContent = `${(w.learn_s || 0).toFixed(1)} of ${LEARN_NEED_S} s` + (typeof w.learn_spread_deg === "number" ? ` · spread ${w.learn_spread_deg.toFixed(0)}° (needs ≤ 10°)` : "");
    }
    if (wasLearning && !learning && cal && cal.forward_source === "driving") {
      fwdMsg = { kind: "ok", text: "Forward direction learned while driving." };
      axisPick = null;
    }
    wasLearning = learning;
    clear(fwdMsgBox);
    if (fwdMsg) fwdMsgBox.append(banner(fwdMsg.kind, fwdMsg.text));
  }

  async function saveRef() {
    if (measuring) return;
    const vals = {};
    let bad = false;
    for (const k of ["pitch", "roll"]) {
      const v = parseDeg(refIn[k].value);
      const e = degError(v);
      refErr[k].hidden = !e;
      refErr[k].textContent = e || "";
      refIn[k].classList.toggle("invalid", !!e);
      if (e) bad = true;
      else {
        vals[k] = v;
        refIn[k].value = String(v);
      }
    }
    if (bad) return;
    measuring = { t0: Date.now() };
    refMsg = null;
    renderCal();
    try {
      const r = await api.post("/api/level/calibration/reference", { pitch_deg: vals.pitch, roll_deg: vals.roll });
      if (stopped) return;
      st = r;
      refMsg = { kind: "ok", text: "Calibration saved.", entered: { pitch: vals.pitch, roll: vals.roll }, read: { pitch: r.pitch_deg, roll: r.roll_deg } };
    } catch (e) {
      refMsg = { kind: e.status === 409 ? "warn" : "bad", text: levelErrorText(e) };
    }
    measuring = null;
    if (!stopped) {
      renderCal();
      renderStatusBits();
      renderLive();
    }
  }

  async function setAxis() {
    fwdBusy = true;
    fwdMsg = null;
    renderCal();
    try {
      const axis = axisSel.value;
      st = await api.post("/api/level/calibration/forward", { axis });
      axisPick = null;
      fwdMsg = { kind: "ok", text: `Forward is the board's ${AXES.find((a) => a.value === axis).label} axis.` };
    } catch (e) {
      fwdMsg = { kind: e.status === 409 ? "warn" : "bad", text: levelErrorText(e) };
    }
    fwdBusy = false;
    if (!stopped) { renderCal(); renderLive(); }
  }

  async function learn(onOff) {
    fwdBusy = true;
    fwdMsg = null;
    renderCal();
    try {
      st = await api.post("/api/level/calibration/forward", { learn: onOff });
      wasLearning = !!(st.wizard && st.wizard.learning);
      if (!onOff) fwdMsg = { kind: "", text: "Learning cancelled — the forward direction is unchanged." };
    } catch (e) {
      fwdMsg = { kind: e.status === 409 ? "warn" : "bad", text: levelErrorText(e) };
    }
    fwdBusy = false;
    if (!stopped) renderCal();
  }

  async function resetCal() {
    const ok = await confirmDialog({ title: "Reset the level calibration?", text: "Level and forward direction are forgotten; angles are measured from the board as it is mounted until you calibrate again.", okText: "Reset", danger: true });
    if (!ok) return;
    try {
      st = await api.del("/api/level/calibration");
      axisPick = null;
      refMsg = null;
      fwdMsg = null;
      toast("ok", "Level calibration reset");
    } catch (e) {
      toast("error", levelErrorText(e));
    }
    renderCal();
    renderLive();
  }

  async function load() {
    try {
      const s = await api.get("/api/level");
      if (stopped) return;
      st = s;
      unavail.hidden = true;
      renderStatusBits();
      renderCal();
      if (!store.online) renderLive();
    } catch (e) {
      if (stopped) return;
      clear(unavail).append(banner("bad", levelErrorText(e)));
      unavail.hidden = false;
    }
  }

  // ───── settings ─────
  let form = null;
  let setCard;
  if (store.settings && !store.settings.level) {
    setCard = h(".card", null, h("h2", null, "Van and screen"), banner("warn", "This device has no level settings yet — update the device software."));
  } else {
    form = new SettingsForm();
    const f = (s) => form.add(s);
    setCard = h(".card", null, h("h2", null, "Van and screen"),
      f({ path: "level.show_on_screen", label: "On the car screen", type: "switch", switchLabel: "show the level when the van stands still",
        help: "Replaces the speedometer after 3 s at a standstill; the speedometer comes back as soon as the van moves." }),
      f({ path: "level.wheelbase_m", label: "Wheelbase", hint: "front to rear axle", type: "number", min: 1.5, max: 7, step: 0.01, unit: "m" }),
      f({ path: "level.track_m", label: "Track", hint: "left to right wheel", type: "number", min: 1, max: 3, step: 0.01, unit: "m",
        help: "Used for how much to put under each wheel." }),
      form.bar);
  }

  // ───── layout ─────
  root.append(
    h(".page-head", null, h("h1", null, "Level"), headBadge),
    unavail,
    h(".grid.g2.lv-top", null,
      h(".card", null,
        h("h2", null, "Now"),
        h(".lv-live", null,
          pvBox,
          h(".lv-nums", null,
            h(".lv-row", null, h("span.k", null, "Pitch"), pitchV, pitchW),
            h(".lv-row", null, h("span.k", null, "Roll"), rollV, rollW),
            h(".lv-sub", null, "Put under the wheels"),
            wheelsBox)),
        h(".kv.lv-kv", null,
          h("span.k", null, "Sensor"), h("span.v", null, stillV),
          h("span.k", null, "Raw vector, g"), h("span.v", null, rawV, magV))),
      h(".card", null,
        h(".card-head", null, h("h2", null, "Calibration"), resetBtn),
        calKv,
        uncalNote,
        h("h3.lv-h3", null, h("span.lv-n", null, "1"), "Forward direction"),
        h("p.lv-p.dim", null, "Which way the sensor board faces the front of the van — best set before the spirit level step."),
        h(".lv-axis", null, h("span", null, "Board axis pointing forward"), axisSel, axisBtn),
        h(".hint", null, "The X and Y arrows printed on the board; Z points out of its face."),
        h(".lv-or.dim", null, "or"),
        learnBtn,
        learnBox,
        fwdMsgBox,
        h("h3.lv-h3", null, h("span.lv-n", null, "2"), "Calibrate with a spirit level"),
        h("p.lv-p.dim", null, "Put a spirit level (or a digital / phone level) on the van floor and type in what it shows. ",
          "Nobody inside or moving while the device measures (3 s)."),
        refRow("pitch", "Along the van (pitch)", "level pointing forward · + when the front is higher"),
        refRow("roll", "Across the van (roll)", "level across the floor · + when the right side is higher"),
        h(".btnrow.lv-save", null, saveBtn),
        measBar,
        measTxt,
        stillHint,
        refMsgBox)),
    setCard,
  );

  renderLive();
  renderStatusBits();
  renderCal();

  // the screen's widget (fonts, sprites) — a failure only hides the preview
  (async () => {
    try {
      const mod = await import("../../../display/ui/index.js");
      await Promise.race([mod.loadUi(), new Promise((r) => setTimeout(r, 4000))]);
      if (stopped || typeof mod.drawLevelPanel !== "function") return;
      hud = mod;
      drawPv();
    } catch (e) {
      console.warn("level preview", e);
      pvBox.hidden = true;
    }
  })();

  const offTel = on("telemetry", renderLive);
  const offSet = on("settings", () => renderLive());
  const tick = setInterval(() => {
    if (measuring) measBar.firstChild.style.width = Math.min(100, ((Date.now() - measuring.t0) / (MEASURE_S * 1000)) * 100) + "%";
  }, 100);
  const stopPoll = poll(load, POLL_MS);

  return () => {
    stopped = true;
    stopPoll();
    clearInterval(tick);
    offTel();
    offSet();
    if (form) form.destroy();
  };
}
