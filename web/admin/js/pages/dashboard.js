// Dashboard: the alarm first (state, Arm / Disarm / Stop siren & lights, camera watch, problems), the latest alarm
// events, then the van at a glance (position, camera, quick actions, system, level, radio). Live data from telemetry;
// api/alarm (camera watch, events) is polled slowly and fetched at once on a new event (telemetry alarm.seq) or state
// change. Device unreachable: the last known data (this session's, or kept in localStorage — store.js keep) greyed out
// with "Last seen …"; remotely the position may come from the server's journal (api/tracking/last).
import { h, icon, clear, isNum } from "../dom.js";
import { api, errText, REMOTE } from "../api.js";
import { store, on, keep, kept, lastTelemetry } from "../store.js";
import { toast, banner, segmented, confirmDialog } from "../ui.js";
import { lastSeenText } from "../conn-fmt.js";
import { alarmStateText, armLeft, kindInfo } from "../alarm-fmt.js";
import { recLink } from "../rec-play.js";
import { eventThumb } from "../snap-view.js";
import { installEntry } from "../install.js";
import {
  EVENTS_N, ACT_COOL_MS, pollDelay, alarmLive, alarmActions, actCooldown, alarmWord, bannerWord, armedFor, armProgress, armHint, alarmChips,
  recentEvents, recentKey, eventExtras, eventCamera, eventAgo, fixInfo, fmtLatLon, motionInfo, limitText, camInfo,
  rearInfo, screenAction, exposureInfo, tempInfo, throttleInfo, diskInfo, netInfo, levelInfo, radioInfo, speakerInfo,
  nextVolume, VOL_STEP,
  newerAlarm, serverPos, videoAction, SCREEN_MODES, screenNowText, quickConfirm, protectedText,
} from "../dash-fmt.js";

const nowS = () => Date.now() / 1000;
const EXPECT_MS = 2500; // after an action, its answer beats telemetry packets already on the way
const EXTRAS_MS = 4000; // snapshot and kept recording are attached to an event a moment after it
const SERVER_POS_MS = 60000; // offline, remote: the server's last point of the van, at most this often
const NOTE_MS = 6000; // a quick action's note next to its button

/** Set text / class only when they change (telemetry comes 2…10 times a second). */
function put(el, text) {
  const s = text == null ? "" : String(text);
  if (el.textContent !== s) el.textContent = s;
}
function cls(el, c) {
  if (el.getAttribute("class") !== c) el.setAttribute("class", c);
}
const kindCls = (k) => (k === "bad" ? "red" : k === "warn" ? "amber" : k === "ok" ? "green" : "");

export function mount(root) {
  root.classList.add("db-page");
  let stopped = false;
  let st = null; // AlarmStatus (GET api/alarm)
  let stAt = 0;
  let stErr = null;
  let stLive = false; // st was fetched by this page (not the copy kept from an earlier visit)
  const stKept = kept("alarm");
  if (stKept) { st = stKept.v; stAt = stKept.at; }
  let busy = null; // alarm action in flight
  let doneAt = null; // Date.now() of the last action's answer: then the buttons are inert for ACT_COOL_MS
  let coolTimer = null;
  let expect = null; // {state, siren, hazard, until}: the answer of the last action
  let lastSeq = null;
  let lastState = null;
  let btnSig = "";
  let chipSig = "";
  let evSig = null;
  let pollTimer = null;
  let extrasTimer = null;
  let loading = null;

  const alarmCfg = () => (store.settings && store.settings.alarm) || null;
  const hzOn = () => !!(alarmCfg() && alarmCfg().hazard && alarmCfg().hazard.enabled);

  function live() {
    const t = store.online && store.telemetry ? store.telemetry.alarm : null;
    if (!t) {
      // no live telemetry: the newest known state — api/alarm or the last telemetry (this session's or kept)
      const lt = lastTelemetry();
      return newerAlarm(lt && lt.t.alarm, lt ? lt.at : 0, st, stAt);
    }
    const fromSt = alarmLive(null, st, 0, stAt);
    if (expect) {
      const same = t.state === expect.state && !!t.siren === expect.siren && !!t.hazard === expect.hazard;
      if (same || Date.now() > expect.until) expect = null;
      else if (fromSt) return { ...fromSt, seq: isNum(t.seq) ? t.seq : null };
    }
    return alarmLive(t, st, store.telemetryAt, stAt);
  }

  // ───── alarm card ─────
  const heroBanner = h(".db-banner", { hidden: true, role: "alert" }); // like the car screen: IMPACT / TILTED / …
  const heroIc = h("span.al-ic");
  const heroWord = h(".al-word.db-word", null, "—");
  const heroSub = h(".db-sub");
  const heroLive = h(".db-live");
  const btns = h(".btnrow.db-btns");
  const armBar = h(".progress.db-armbar", { hidden: true }, h("i"));
  const heroHint = h(".hint.db-hint");
  const chips = h(".db-chips");
  const hero = h(".card.db-hero", null,
    h(".card-head", null, h("h2", null, "Alarm"), h("a.db-more", { href: "#/alarm" }, "Settings & log ›")),
    heroBanner,
    h(".db-hero-top", null,
      heroIc,
      h(".db-hero-txt", null, heroWord, heroSub, heroLive),
      btns),
    armBar,
    heroHint,
    chips);

  async function act(what) {
    if (busy) return;
    busy = what;
    renderHero();
    try {
      const r = await api.post("/api/alarm/" + what);
      if (stopped) return;
      expect = { state: r.state, siren: !!r.siren, hazard: !!(r.hazard && r.hazard.active), until: Date.now() + EXPECT_MS };
      setStatus(r);
      if (what === "arm") toast("ok", r.state === "arming" ? `Arming — the alarm is on in ${r.arm_in_s ?? "a few"} s` : "Alarm armed");
      else if (what === "disarm") toast("ok", "Alarm off");
      else if (what === "silence") toast("ok", "Siren and hazard lights stopped — the alarm stays armed");
    } catch (e) {
      toast("error", errText(e));
    } finally {
      busy = null;
      doneAt = Date.now(); // a double tap must not hit the button that takes this one's place
      if (!stopped) {
        coolSoon();
        renderHero();
      }
    }
  }
  // the cool-down ends: the buttons come alive
  function coolSoon() {
    clearTimeout(coolTimer);
    coolTimer = setTimeout(() => { if (!stopped) renderHero(); }, ACT_COOL_MS + 20);
  }

  function renderHero() {
    const l = live();
    if (!l) {
      cls(heroWord, "al-word db-word");
      put(heroWord, "—");
      put(heroSub, stErr || (store.online ? "Loading…" : store.online === null ? "Connecting…" : "No connection to the device"));
      cls(hero, "card db-hero");
      clear(btns);
      btnSig = "";
      armBar.hidden = true;
      heroBanner.hidden = true;
      put(heroHint, "");
      return;
    }
    const s = alarmStateText(l.state);
    const k = s.kind || "off";
    // no telemetry, and api/alarm failed or was not asked yet (a kept copy): the last known state
    const stale = !store.online && (!!stErr || !stLive);
    cls(hero, `card db-hero s-${l.state}${stale ? " stale" : ""}`);
    cls(heroWord, `al-word db-word ${k}${l.state === "triggered" ? " blink" : ""}`);
    put(heroWord, alarmWord(l.state));
    const icName = l.state === "off" ? "unlock" : l.state === "triggered" ? "alarm" : "lock";
    if (heroIc.dataset.ic !== icName) {
      clear(heroIc).append(icon(icName, 40));
      heroIc.dataset.ic = icName;
    }
    cls(heroIc, "al-ic " + k);

    // sub line: countdown / armed for / the event that triggered it
    const now = nowS();
    const left = l.state === "arming" ? armLeft(l.armIn, l.at, Date.now()) : null;
    let sub = "";
    let last = null;
    if (l.state === "arming") sub = `${left ?? "…"} s until armed`;
    else if (l.state === "triggered") {
      const ev = st && Array.isArray(st.events) && st.events[0];
      const lt = store.online && store.telemetry && store.telemetry.alarm && store.telemetry.alarm.last;
      last = lt || ev || null;
      sub = last ? `${last.title || kindInfo(last.kind).label} · ${eventAgo(last.ts, now)}` : "Something happened";
    } else if (l.state === "armed") sub = (st && armedFor(st.armed_at, now)) || "Watching the van";
    else if (l.state === "off") sub = "Not watching the van";
    if (stale) sub = "No connection · last known state" + (l.at > 0 ? ", " + eventAgo(l.at / 1000, now) : "");
    put(heroSub, sub);
    cls(heroSub, "db-sub" + (l.state === "arming" ? " amber mono" : ""));
    heroBanner.hidden = l.state !== "triggered" || stale;
    put(heroBanner, bannerWord(last && last.kind));

    // siren / hazard sounding now
    const liveSig = `${l.siren}|${l.hazard}`;
    if (heroLive.dataset.sig !== liveSig) {
      heroLive.dataset.sig = liveSig;
      clear(heroLive);
      if (l.siren) heroLive.append(h("span.al-siren", null, icon("vol", 16), "Siren sounding"));
      if (l.hazard) heroLive.append(h("span.al-siren.al-hz", null, icon("hazard", 16), "Hazard lights flashing"));
      heroLive.hidden = !l.siren && !l.hazard;
    }

    armBar.hidden = l.state !== "arming" || stale; // an old countdown would run on
    if (l.state === "arming" && !stale) {
      const total = (alarmCfg() && alarmCfg().arm_delay_s) || l.armIn || 1;
      armBar.firstChild.style.width = armProgress(left ?? total, total) * 100 + "%";
    }

    // buttons: rebuilt only when the set changes (not with the countdown) — a button must not vanish under the finger
    const acts = alarmActions({ state: l.state, siren: l.siren, hazard: l.hazard, hazardEnabled: hzOn() });
    const sig = acts.map((a) => a.what + ":" + a.label).join("|") + "|" + busy;
    // right after an answer the new buttons are inert (dimmed) for a moment: Disarm takes Arm's place
    const nowMs = Date.now();
    const cool = !busy && actCooldown(doneAt, nowMs) > 0;
    const inert = cool || store.offline; // the server answers for the device: nothing to arm or disarm there
    // while an action is in flight the (disabled) buttons stay as they are until its answer
    if (sig !== btnSig && !(busy && btnSig.endsWith("|" + busy))) {
      btnSig = sig;
      // the set changed again during the cool-down (telemetry caught up): it counts from this swap
      if (cool) {
        doneAt = nowMs;
        coolSoon();
      }
      clear(btns);
      for (const a of acts) {
        btns.append(h(`button.btn.al-big.${a.cls}`, { type: "button", disabled: !!busy || inert, title: a.title, onclick: () => act(a.what) },
          busy === a.what ? h("span.spin") : icon(a.icon), a.label));
      }
    }
    // the cool-down ends: the same buttons come alive (not rebuilt under the finger)
    if (!busy) for (const b of btns.children) if (b.disabled !== inert) b.disabled = inert;
    put(heroHint, l.state === "off" ? armHint(alarmCfg() ? alarmCfg().arm_delay_s : null) : "");
    heroHint.hidden = l.state !== "off";

    // chips: camera watch, hazard lights, problems
    const list = alarmChips(st, { sensorOk: l.sensorOk, hazardActive: l.hazard });
    const csig = JSON.stringify(list);
    if (csig !== chipSig) {
      chipSig = csig;
      clear(chips);
      for (const c of list) {
        chips.append(h(`span.badge.db-chip${c.kind ? "." + c.kind : ""}${c.key === "hazard" && c.kind === "warn" ? ".blink" : ""}`,
          { title: c.title || "" }, icon(c.icon), c.text));
      }
      chips.hidden = !list.length;
    }
  }

  // ───── latest events ─────
  const evList = h(".db-evlist");
  const whens = []; // [el, ts]
  const evCard = h(".card.db-events", null,
    h(".card-head", null, h("h2", null, "Latest events"), h("a.db-more", { href: "#/alarm" }, "All events ›")),
    evList);

  function renderEvents() {
    evCard.classList.toggle("stale", !store.online && (!!stErr || !stLive));
    if (!st) {
      if (evSig !== "none") {
        evSig = "none";
        clear(evList).append(h(".db-empty.dim", null, stErr ? "Events are not available." : "Loading…"));
      }
      return;
    }
    const list = recentEvents(st.events, EVENTS_N);
    const key = recentKey(list);
    if (key === evSig) return;
    evSig = key;
    clear(evList);
    whens.length = 0;
    if (!list.length) {
      evList.append(h(".db-empty.dim", null, "No events yet. They show up here when the alarm notices something."));
      return;
    }
    const now = nowS();
    for (const ev of list) {
      const ki = kindInfo(ev.kind, ev.data && ev.data.camera);
      const x = eventExtras(ev);
      const when = h("span.when.mono", { title: isNum(ev.ts) ? new Date(ev.ts * 1000).toLocaleString() : "" }, eventAgo(ev.ts, now));
      whens.push([when, ev.ts]);
      const thumb = eventThumb(ev); // the camera that raised it; the modal shows both
      const recs = [
        x.segment ? h("a", { href: recLink(x.segment, ev.ts), title: "Watch from the moment of the event: " + x.segment },
          x.segmentRear ? "Front recording kept" : "Recording kept") : null,
        x.segmentRear ? h("a", { href: recLink(x.segmentRear, ev.ts), title: "Watch from the moment of the event: " + x.segmentRear }, "Rear recording kept") : null,
      ];
      if (eventCamera(ev) === "rear") recs.reverse(); // a rear-camera event: its own recording first
      evList.append(h(".db-ev." + (ki.kind || "plain"), null,
        h("span.ic", null, icon(ki.icon)),
        h(".txt", null,
          h(".top", null, h("span.tt", null, ev.title || ki.label), when),
          ev.body ? h(".bd", null, ev.body) : null,
          x.link || x.segment || x.segmentRear ? h(".ex", null,
            x.link ? h("a", { href: x.link.href }, x.link.text) : null,
            ...recs) : null),
        thumb));
    }
  }

  function renderWhens() {
    const now = nowS();
    for (const [el, ts] of whens) put(el, eventAgo(ts, now));
  }

  // ───── the van ─────
  const errBox = h("div");
  const seenEl = h(".db-seen", { hidden: true, role: "status" }); // offline: "Last seen 12 min ago"
  let srvLast = null; // api/tracking/last (remote, offline): where the server last heard of the van
  let srvAt = 0;
  const tile = (href, ic, label, ...body) => h("a.db-tile", { href }, h(".db-tl", null, icon(ic), h("span", null, label), h("span.db-go", null, "›")), ...body);

  const posTxt = h("span", null, "—");
  const posLim = h("span.db-limit", { hidden: true, title: "Speed limit here" });
  const posV = h(".db-tv", null, posTxt, posLim);
  const posFix = h(".db-ts");
  const posLL = h(".db-ts.mono.wrap");
  const posTile = tile("#/tracking", "pin", "Position", posV, posFix, posLL);

  // camera: the front camera; with the rear one on, its line and the car screen button under it
  const recDot = h("span.dot.rec-dot");
  const camV = h(".db-tv", null, recDot, h("span", null, "—"));
  const camExp = h(".db-ts");
  const camErr = h(".db-ts.red");
  const rearDot = h("span.dot.rec-dot");
  const rearTxt = h("span.db-reartxt");
  const rearOnScr = h("span.db-onscr", { hidden: true }, "on screen");
  const rearErr = h(".db-ts.red.db-rearerr", { hidden: true });
  const rearBtn = h("button.btn.sm.db-scrbtn", { type: "button", onclick: () => toggleScreen() }, icon("screen"), h("span", null, "Show on screen"));
  const rearRow = h(".db-rear", { hidden: true },
    h("a.db-rearst", { href: "#/camera?cam=rear", title: "Rear camera" }, h("span.db-rearlbl", null, "Rear"), rearDot, rearTxt, rearOnScr),
    rearErr,
    rearBtn);
  const camTile = h(".db-tile.db-cam", null,
    h("a.db-rtop", { href: "#/camera?cam=front" },
      h(".db-tl", null, icon("cam"), h("span", null, "Camera"), h("span.db-go", null, "›")),
      camV, camExp, camErr),
    rearRow);
  let scrBusy = false;
  let scrExpect = null; // {camera, until}: the answer of the last switch until telemetry shows it
  const rearOn = () => !!(store.settings && store.settings.rear_camera && store.settings.rear_camera.enabled);

  /** What the car screen shows: telemetry, or the answer of the switch just made until telemetry catches up. */
  function screenNow(d) {
    if (scrExpect && d) {
      if (d.camera === scrExpect.camera || Date.now() > scrExpect.until) scrExpect = null;
      else return { ...d, camera: scrExpect.camera };
    }
    return d;
  }

  async function toggleScreen() {
    if (scrBusy) return;
    const a = screenAction(screenNow(store.telemetry && store.telemetry.display));
    scrBusy = true;
    rearBtn.disabled = true;
    try {
      const r = await api.post("/api/camera/view", { camera: a.to });
      if (stopped) return;
      scrExpect = { camera: (r && r.camera) || a.to, until: Date.now() + EXPECT_MS };
      toast("ok", scrExpect.camera === "rear" ? "The car screen shows the rear camera" : "The car screen is back to the front camera");
    } catch (e) {
      toast("error", "Car screen: " + errText(e));
    } finally {
      scrBusy = false;
      rearBtn.disabled = false;
      if (!stopped) renderVan();
    }
  }

  function renderRear(t) {
    const on = rearOn();
    rearRow.hidden = !on;
    if (!on) return;
    const rc = t.camera_rear || null;
    const ri = rearInfo(rc);
    put(rearTxt, ri.text);
    cls(rearTxt, "db-reartxt " + (ri.rec ? "" : kindCls(ri.kind)));
    rearDot.hidden = !ri.rec;
    rearDot.classList.toggle("on", ri.rec);
    const sa = screenAction(screenNow(store.online ? t.display : null));
    rearOnScr.hidden = !sa.onScreen;
    if (rearBtn.dataset.to !== sa.to) {
      rearBtn.dataset.to = sa.to;
      put(rearBtn.lastChild, sa.label);
      rearBtn.title = sa.title;
      rearBtn.classList.toggle("on", sa.onScreen);
    }
    put(rearErr, (rc && rc.error) || "");
    rearErr.hidden = !(rc && rc.error);
  }

  const sysTemp = h("span", null, "—");
  const sysLoad = h("span.db-tvs");
  const sysV = h(".db-tv", null, sysTemp, sysLoad);
  const sysDisk = h(".db-ts");
  const sysNet = h(".db-ts.wrap");
  const sysPow = h(".db-ts");
  const sysTile = tile("#/system", "sys", "System", sysV, sysDisk, sysNet, sysPow);

  const lvV = h(".db-tv", null, "—");
  const lvSub = h(".db-ts.wrap");
  const lvNote = h(".db-ts.amber");
  const lvTile = tile("#/level", "level", "Level", lvV, lvSub, lvNote);
  lvTile.hidden = true;

  // radio: the top links to the Radio page, the buttons control it right here
  const eq = h(".eq.sm", { "aria-hidden": "true" }, h("i"), h("i"), h("i"), h("i"), h("i"));
  const rTitle = h("span.db-station");
  const rLine = h(".db-ts.db-track");
  const spk = h("span.db-spk");
  const vol = h("span.db-vol.mono");
  let rBusy = false;
  const rBtn = (ic, label, fn) => h("button.btn.db-rbtn", { type: "button", title: label, "aria-label": label, onclick: fn }, icon(ic));
  const prevBtn = rBtn("prev", "Previous station", () => radio("/api/radio/step", { delta: -1 }));
  const nextBtn = rBtn("next", "Next station", () => radio("/api/radio/step", { delta: 1 }));
  const playBtn = rBtn("play", "Play", () => radio(radioInfo(store.telemetry && store.telemetry.radio).active ? "/api/radio/stop" : "/api/radio/resume", {}));
  // volume −/+ (5 %, like the keypad knob): each tap is sent at once; quick taps step from the value just sent, not
  // from telemetry that hasn't caught up yet
  let volPending = null;
  let volPendingT = null;
  const volDn = rBtn("minus", "Volume down", () => stepVolume(-VOL_STEP));
  const volUp = rBtn("plus", "Volume up", () => stepVolume(VOL_STEP));
  volDn.classList.add("db-vbtn");
  volUp.classList.add("db-vbtn");
  async function stepVolume(delta) {
    const r = store.telemetry && store.telemetry.radio;
    const v = nextVolume(volPending, r ? r.volume : null, delta);
    if (v === null) return;
    volPending = v;
    clearTimeout(volPendingT);
    volPendingT = setTimeout(() => { volPending = null; if (!stopped) renderRadio(false); }, 1500);
    put(vol, `vol ${v}`);
    try {
      await api.post("/api/radio/volume", { volume: v });
    } catch (e) {
      toast("error", "Volume: " + errText(e));
      volPending = null;
      if (!stopped) renderRadio(false);
    }
  }
  const radioTile = h(".db-tile.db-radio.wide", null,
    h("a.db-rtop", { href: "#/radio" },
      h(".db-tl", null, icon("radio"), h("span", null, "Radio"), spk, h("span.db-go", null, "›")),
      h(".db-np", null, eq, rTitle),
      rLine),
    h(".db-rctl", null, prevBtn, playBtn, nextBtn, h("span.grow"), h(".db-volgrp", null, volDn, vol, volUp)));

  async function radio(url, body) {
    if (rBusy) return;
    rBusy = true;
    prevBtn.disabled = playBtn.disabled = nextBtn.disabled = true;
    try {
      await api.post(url, body);
    } catch (e) {
      toast("error", "Radio: " + errText(e));
    } finally {
      rBusy = false;
      if (!stopped) renderRadio(true);
    }
  }

  // ───── quick actions: protect the recording, the car monitor on / off, what the car screen shows ─────
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
  const modeSeg = segmented({ name: "db-scrmode", options: SCREEN_MODES, value: forceMode(), onchange: (v) => setMode(v) });
  modeSeg.classList.add("db-qseg");
  const modeNow = h("span.db-qnow");
  const quickTile = h(".db-tile.wide.db-quick", null,
    h(".db-tl", null, icon("keys"), h("span", null, "Quick actions")),
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
    qTimer = setTimeout(() => { if (!stopped) renderQuick(); }, ACT_COOL_MS + 20);
    renderQuick();
  }
  const movingNow = () => {
    const t = store.online && store.telemetry;
    return !!t && motionInfo(t.gps, t.heading).moving;
  };

  async function protect() {
    if (qBusy.protect || qCool("protect")) return;
    qBusy.protect = true;
    renderQuick();
    try {
      const r = await api.post("/api/recordings/protect-current");
      if (stopped) return;
      // the device also tells it over WS (toasts); the note stays next to the button for a moment
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
    renderQuick();
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
    renderQuick();
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
    renderQuick();
  }

  /** The spinner in place of the button's icon while its request runs. */
  function busyIcon(btn, busy, ic) {
    const spinning = !!btn.querySelector(".spin");
    if (busy !== spinning) btn.firstChild.replaceWith(busy ? h("span.spin") : icon(ic));
  }

  function renderQuick() {
    const off = store.offline; // the server answered for the device: nothing to switch
    const offTitle = "The device is offline";
    busyIcon(protBtn, qBusy.protect, "lock");
    protBtn.disabled = off || qBusy.protect || qCool("protect");
    vidRow.hidden = vidMissing;
    const a = videoAction(vidOn);
    put(vidState, a.state);
    cls(vidState, "db-qstate " + kindCls(a.kind));
    put(vidBtn.lastChild, a.label);
    vidBtn.title = off ? offTitle : vidErr ? "Car monitor: " + vidErr : a.title;
    busyIcon(vidBtn, qBusy.video, "screen");
    vidBtn.disabled = off || qBusy.video || qCool("video");
    if (!qBusy.screen) modeSeg.set(forceMode());
    modeSeg.classList.toggle("busy", off || qBusy.screen);
    const t = store.online && store.telemetry;
    put(modeNow, t ? screenNowText(t.display) : "");
  }

  const tiles = h(".db-tiles", null, posTile, camTile, quickTile, sysTile, lvTile, radioTile);
  const install = installEntry("button.db-install", "Install app"); // hides itself in the installed app
  const van = h(".db-van", null, seenEl, errBox, tiles, h(".db-foot", null, install));

  /** Offline: since when the shown values are (live this session, or kept from an earlier one). */
  function renderSeen() {
    const lt = lastTelemetry();
    const show = store.online === false || (store.online === null && !!lt);
    seenEl.hidden = !show;
    if (!show) return;
    put(seenEl, lt ? lastSeenText(lt.at, Date.now()) + (store.online === null ? " — connecting…" : " — no connection to the device")
      : "No connection to the device");
  }

  async function loadServerPos() {
    srvAt = Date.now();
    try {
      const l = await api.get("/api/tracking/last");
      if (!stopped) { srvLast = l; renderVan(); }
    } catch { /* the server is unreachable too: the kept telemetry stays */ }
  }

  function renderVan() {
    const lt = lastTelemetry();
    const t = lt && lt.t;
    van.classList.toggle("stale", !store.online);
    renderSeen();
    if (!t) return;
    const g = t.gps || {}, cam = t.camera || {}, sys = t.system || {};

    // offline: the server's last point when it is newer than the last telemetry seen here
    const sp = !store.online ? serverPos(srvLast, lt.at) : null;
    const pg = sp ? { fix: "3d", speed_mps: sp.speed_mps, lat: sp.lat, lon: sp.lon } : g;
    const m = motionInfo(pg, sp ? { deg: sp.course_deg } : t.heading);
    const lim = m.moving && !sp ? limitText(t.speed_limit) : null;
    put(posTxt, m.text);
    put(posLim, lim || "");
    posLim.hidden = !lim;
    cls(posV, "db-tv" + (m.moving ? " green" : ""));
    const fx = sp ? { text: "From the server · " + eventAgo(sp.ts, nowS()), kind: "" } : fixInfo(g);
    put(posFix, fx.text);
    cls(posFix, "db-ts " + kindCls(fx.kind));
    put(posLL, fmtLatLon(pg.lat, pg.lon) || "");
    posLL.hidden = !fmtLatLon(pg.lat, pg.lon);

    const c = camInfo(cam);
    put(camV.lastChild, c.text);
    cls(camV, "db-tv " + (c.rec ? "" : kindCls(c.kind)));
    recDot.hidden = !c.rec;
    recDot.classList.toggle("on", c.rec);
    const ex = exposureInfo(cam);
    put(camExp, ex ? ex.text : "");
    camExp.title = ex ? ex.title : "";
    camExp.hidden = !ex;
    cls(camExp, "db-ts" + (ex && ex.night ? " db-night" : ""));
    put(camErr, cam.error || "");
    camErr.hidden = !cam.error;
    renderRear(t);

    const tp = tempInfo(sys.cpu_temp_c);
    put(sysTemp, tp.text);
    cls(sysV, "db-tv " + kindCls(tp.kind));
    put(sysLoad, isNum(sys.load_1m) ? `load ${sys.load_1m.toFixed(2)}` : "");
    const dk = diskInfo(t.disk, cam);
    put(sysDisk, dk.text);
    cls(sysDisk, "db-ts " + kindCls(dk.kind));
    const nt = netInfo(sys);
    put(sysNet, nt.text);
    cls(sysNet, "db-ts wrap " + kindCls(nt.kind));
    const pw = throttleInfo(sys.throttled);
    const powBad = pw.kind === "bad" || pw.kind === "warn";
    put(sysPow, powBad ? pw.text : "");
    sysPow.hidden = !powBad;
    cls(sysPow, "db-ts " + kindCls(pw.kind));

    const lv = levelInfo(t.level);
    lvTile.hidden = !lv;
    sysTile.classList.toggle("wide", !lv); // keep the grid even without the level tile
    if (lv) {
      put(lvV, lv.text);
      cls(lvV, "db-tv " + kindCls(lv.kind));
      put(lvSub, lv.sub);
      lvSub.hidden = !lv.sub;
      put(lvNote, lv.note);
      lvNote.hidden = !lv.note;
    }

    renderRadio(false);

    const errs = Array.isArray(t.errors) ? t.errors : [];
    const esig = errs.join("\n");
    if (errBox.dataset.sig !== esig) {
      errBox.dataset.sig = esig;
      clear(errBox);
      if (errs.length) errBox.append(banner("warn", [h("b", null, "Problems: "), errs.join(" · ")]));
    }
  }

  function renderRadio(force) {
    const lt = lastTelemetry();
    const r = lt ? lt.t.radio : null;
    radioTile.hidden = !r && !!lt; // a device without the radio
    const ri = radioInfo(r);
    put(rTitle, ri.title);
    cls(rTitle, "db-station" + (ri.off ? " off" : ""));
    put(rLine, (ri.track ? "♪ " : "") + ri.line);
    cls(rLine, "db-ts db-track " + (ri.track ? "green" : kindCls(ri.lineKind)));
    eq.classList.toggle("on", ri.playing);
    eq.classList.toggle("wait", ri.active && !ri.playing);
    if (volPending === null) put(vol, ri.volume === null ? "" : `vol ${ri.volume}`);
    const sp = speakerInfo(r);
    const ssig = `${sp.text}|${sp.kind}`;
    if (spk.dataset.sig !== ssig) {
      spk.dataset.sig = ssig;
      clear(spk).append(icon("bt"), h("span", null, sp.text));
      spk.className = "db-spk " + kindCls(sp.kind);
      spk.hidden = !r;
      spk.title = sp.connected ? "Bluetooth speaker connected" : r && r.output ? "Bluetooth speaker not connected" : "No Bluetooth speaker selected";
    }
    // the play button stays in place: only its icon and label change
    const pk = ri.active ? "stop" : "play";
    if (force || playBtn.dataset.k !== pk) {
      playBtn.dataset.k = pk;
      clear(playBtn).append(icon(pk));
      const label = ri.active ? "Stop the radio" : ri.canPlay ? "Play " + ri.title : "Pick a station on the Radio page first";
      playBtn.title = label;
      playBtn.setAttribute("aria-label", label);
    }
    if (!rBusy) {
      playBtn.disabled = !ri.active && !ri.canPlay;
      prevBtn.disabled = nextBtn.disabled = !r;
    }
    volDn.disabled = volUp.disabled = !r || ri.volume === null;
  }

  // ───── data ─────
  function setStatus(s) {
    if (!s || typeof s !== "object") return;
    st = s;
    stAt = Date.now();
    stErr = null;
    stLive = true;
    keep("alarm", s, stAt); // the offline view of the next visit
    renderHero();
    renderEvents();
  }

  async function load() {
    if (loading) return loading;
    loading = (async () => {
      try {
        const s = await api.get("/api/alarm?events=5"); // the dashboard shows 5: not the whole log on every check
        if (!stopped) setStatus(s);
      } catch (e) {
        if (stopped) return;
        stErr = e.status === 404 ? "The alarm is not available on this device yet — update the device software." : "Alarm: " + errText(e);
        renderHero();
        renderEvents();
      } finally {
        loading = null;
      }
    })();
    return loading;
  }

  function schedule() {
    clearTimeout(pollTimer);
    if (stopped) return;
    const l = live();
    pollTimer = setTimeout(async () => {
      if (!document.hidden) {
        if (!store.online && REMOTE && Date.now() - srvAt > SERVER_POS_MS) loadServerPos();
        await load();
      }
      schedule();
    }, pollDelay(store.online, l && l.state));
  }

  function fetchNow() {
    load();
    schedule();
  }

  // ───── layout ─────
  root.append(
    h(".page-head", null, h("h1", null, "Dashboard")),
    h(".db-grid", null, hero, evCard, van),
  );

  renderHero();
  renderEvents();
  renderVan();
  renderQuick();
  fetchNow();
  loadVideo();

  const offTel = on("telemetry", () => {
    renderVan();
    renderHero();
    const l = live();
    if (!l) return;
    let fresh = false;
    if (l.seq !== null) {
      if (lastSeq !== null && l.seq !== lastSeq) {
        fresh = true;
        clearTimeout(extrasTimer);
        extrasTimer = setTimeout(() => !stopped && load(), EXTRAS_MS); // snapshot / recording come a moment later
      }
      lastSeq = l.seq;
    }
    if (lastState !== null && l.state !== lastState) fresh = true;
    lastState = l.state;
    if (fresh) fetchNow();
  });
  // the device dropped off or came back: ask now (offline the poll gets faster and tells why)
  let wasOnline = store.online;
  const offConn = on("conn", (o) => {
    renderVan();
    renderHero();
    renderEvents();
    renderQuick();
    fetchNow();
    if (o && !wasOnline) loadVideo(); // the monitor may have been switched by a key meanwhile
    if (!o && REMOTE && Date.now() - srvAt > SERVER_POS_MS) loadServerPos();
    wasOnline = o;
  });
  const offSet = on("settings", () => { btnSig = ""; renderHero(); renderVan(); renderQuick(); });
  // the car monitor switched elsewhere (a key, another phone): fnav-core tells it over WS
  const offEv = on("event", (m) => {
    if (m.event === "video_output" && typeof m.on === "boolean") {
      vidOn = m.on;
      renderQuick();
    }
  });
  // countdown, "armed for", "x min ago" and "Last seen" between packets
  const tick = setInterval(() => { renderHero(); renderWhens(); renderSeen(); renderQuick(); }, 1000);
  const onVis = () => { if (!document.hidden) fetchNow(); };
  document.addEventListener("visibilitychange", onVis);

  return () => {
    stopped = true;
    clearTimeout(pollTimer);
    clearTimeout(extrasTimer);
    clearTimeout(coolTimer);
    clearTimeout(qTimer);
    clearTimeout(noteTimer);
    clearTimeout(volPendingT);
    clearInterval(tick);
    document.removeEventListener("visibilitychange", onVis);
    offTel();
    offConn();
    offSet();
    offEv();
    install.dispose();
  };
}
