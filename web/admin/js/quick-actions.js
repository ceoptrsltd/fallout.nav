// Quick actions (Camera page): protect the recording, the car monitor on / off, what the car screen shows.
// quickActions() → {el, dispose}; it follows telemetry, settings and the device's events itself.
import { h, icon } from "./dom.js";
import { api, errText } from "./api.js";
import { store, on } from "./store.js";
import { toast, segmented, confirmDialog } from "./ui.js";
import {
  ACT_COOL_MS, actCooldown, motionInfo, videoAction, SCREEN_MODES, screenNowText, quickConfirm, protectedText,
} from "./dash-fmt.js";

const EXPECT_MS = 2500;
const NOTE_MS = 6000; // a quick action's note next to its button

function put(el, text) {
  const s = text == null ? "" : String(text);
  if (el.textContent !== s) el.textContent = s;
}
const kindCls = (k) => (k === "bad" ? "red" : k === "warn" ? "amber" : k === "ok" ? "green" : "");

export function quickActions() {
  let stopped = false;
  const qBusy = { protect: false, video: false, screen: false };
  const qDone = { protect: null, video: null }; // the answer's Date.now(): a double tap must not repeat it (actCooldown)
  let qTimer = null;
  let noteTimer = null;
  let vidOn = null; // api/display/output: true / false / null — not known
  let vidErr = null;
  let vidMissing = false; // an older device without the endpoint: the row is hidden
  let modeExpect = null; // {mode, until}: the mode just set, until settings over WS show it
  const protBtn = h("button.btn.db-qbtn", { type: "button", title: "Keep the current recording (both cameras) from being overwritten", onclick: () => protect() },
    icon("lock"), h("span", null, "Protect recording"));
  const protNote = h("span.db-qnote.green", { hidden: true });
  const vidState = h("b.db-qstate", null, "—");
  const vidBtn = h("button.btn.db-qbtn", { type: "button", onclick: () => toggleVideo() }, icon("screen"), h("span", null, "On / off"));
  const vidRow = h(".db-qrow", null, h("span.db-qlbl", null, "Car monitor", vidState), vidBtn);
  const modeSeg = segmented({ name: "qa-scrmode", options: SCREEN_MODES, value: forceMode(), onchange: (v) => setMode(v) });
  modeSeg.classList.add("db-qseg");
  const modeNow = h("span.db-qnow");
  const el = h(".card.db-quick", null,
    h("h2", null, "Quick actions"),
    h(".db-qrow", null, protBtn, protNote),
    vidRow,
    h(".db-qrow", null, h("span.db-qlbl", null, "Car screen", modeNow), modeSeg));

  function forceMode() {
    const s = (store.settings && store.settings.display && store.settings.display.force_mode) || "auto";
    if (modeExpect && (modeExpect.mode === s || Date.now() > modeExpect.until)) modeExpect = null;
    return modeExpect ? modeExpect.mode : s;
  }
  const qCool = (k) => actCooldown(qDone[k], Date.now()) > 0;
  function qFinish(k) {
    qBusy[k] = false;
    if (k in qDone) qDone[k] = Date.now();
    if (stopped) return;
    clearTimeout(qTimer);
    qTimer = setTimeout(() => { if (!stopped) render(); }, ACT_COOL_MS + 20);
    render();
  }
  const movingNow = () => {
    const t = store.online && store.telemetry;
    return !!t && motionInfo(t.gps, t.heading).moving;
  };

  async function protect() {
    if (qBusy.protect || qCool("protect")) return;
    qBusy.protect = true;
    render();
    try {
      const r = await api.post("/api/recordings/protect-current");
      if (stopped) return;
      put(protNote, protectedText(r));
      protNote.hidden = false;
      clearTimeout(noteTimer);
      noteTimer = setTimeout(() => { protNote.hidden = true; }, NOTE_MS);
    } catch (e) {
      toast("error", "Protect recording: " + errText(e));
    } finally {
      qFinish("protect");
    }
  }

  async function toggleVideo() {
    if (qBusy.video || qCool("video")) return;
    const a = videoAction(vidOn);
    const ask = quickConfirm("video", { to: a.to });
    if (ask && !(await confirmDialog(ask))) return;
    if (stopped || qBusy.video) return;
    qBusy.video = true;
    render();
    try {
      const r = await api.post("/api/display/output", a.to === null ? {} : { on: a.to });
      if (stopped) return;
      if (r && typeof r.on === "boolean") vidOn = r.on;
      vidErr = null;
    } catch (e) {
      toast("error", "Car monitor: " + errText(e));
    } finally {
      qFinish("video");
    }
  }

  async function setMode(v) {
    const before = forceMode();
    if (v === before) return;
    const ask = quickConfirm("screen", { mode: v, moving: movingNow() });
    if (ask && !(await confirmDialog(ask))) { modeSeg.set(forceMode()); return; }
    if (stopped) return;
    qBusy.screen = true;
    render();
    try {
      await api.post("/api/display/force", { mode: v });
      if (stopped) return;
      modeExpect = { mode: v, until: Date.now() + 4 * EXPECT_MS }; // settings come over WS (later through the tunnel)
    } catch (e) {
      toast("error", "Car screen: " + errText(e));
    } finally {
      qFinish("screen");
    }
  }

  async function loadVideo() {
    try {
      const r = await api.get("/api/display/output");
      if (stopped) return;
      vidOn = r && typeof r.on === "boolean" ? r.on : null;
      vidErr = null;
      vidMissing = false;
    } catch (e) {
      if (stopped) return;
      if (e.status === 404) vidMissing = true;
      else vidErr = errText(e);
    }
    render();
  }

  /** The spinner in place of the button's icon while its request runs. */
  function busyIcon(btn, busy, ic) {
    const spinning = !!btn.querySelector(".spin");
    if (busy !== spinning) btn.firstChild.replaceWith(busy ? h("span.spin") : icon(ic));
  }

  function render() {
    const off = store.offline; // the server answered for the device: nothing to switch
    busyIcon(protBtn, qBusy.protect, "lock");
    protBtn.disabled = off || qBusy.protect || qCool("protect");
    vidRow.hidden = vidMissing;
    const a = videoAction(vidOn);
    put(vidState, a.state);
    vidState.className = "db-qstate " + kindCls(a.kind);
    put(vidBtn.lastChild, a.label);
    vidBtn.title = off ? "The device is offline" : vidErr ? "Car monitor: " + vidErr : a.title;
    busyIcon(vidBtn, qBusy.video, "screen");
    vidBtn.disabled = off || qBusy.video || qCool("video");
    if (!qBusy.screen) modeSeg.set(forceMode());
    modeSeg.classList.toggle("busy", off || qBusy.screen);
    const t = store.online && store.telemetry;
    put(modeNow, t ? screenNowText(t.display) : "");
  }

  render();
  loadVideo();
  let wasOnline = store.online;
  const offs = [
    on("settings", render),
    on("telemetry", render),
    on("conn", (o) => { render(); if (o && !wasOnline) loadVideo(); wasOnline = o; }), // switched by a key meanwhile
    // the car monitor switched elsewhere (a key, another phone): fnav-core tells it over WS
    on("event", (m) => { if (m.event === "video_output" && typeof m.on === "boolean") { vidOn = m.on; render(); } }),
  ];
  return {
    el,
    dispose() {
      stopped = true;
      clearTimeout(qTimer);
      clearTimeout(noteTimer);
      for (const off of offs) off();
    },
  };
}
