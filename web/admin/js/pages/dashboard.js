// Dashboard: the alarm button first (it says the state, the camera watch, problems — and arms / disarms), the radio,
// the air, the power stations, then the latest alarm events and the van at a glance (position, camera, system, level).
// Quick actions live on the Camera page (quick-actions.js). Live data from telemetry;
// api/alarm (camera watch, events) is polled slowly and fetched at once on a new event (telemetry alarm.seq) or state
// change. Device unreachable: the last known data (this session's, or kept in localStorage — store.js keep) greyed out
// with "Last seen …"; remotely the position may come from the server's journal (api/tracking/last).
import { h, icon, clear, isNum } from "../dom.js";
import { api, errText, REMOTE } from "../api.js";
import { store, on, keep, kept, lastTelemetry, applySettings } from "../store.js";
import { toast, banner } from "../ui.js";
import { lastSeenText } from "../conn-fmt.js";
import { alarmStateText, armLeft, kindInfo } from "../alarm-fmt.js";
import { recLink } from "../rec-play.js";
import { eventThumb } from "../snap-view.js";
import { installEntry } from "../install.js";
import { METRICS as AIR_METRICS, fmtValue, levelOf } from "../air-fmt.js";
import { sparkline } from "../air-chart.js";
import { powerControls } from "../power-controls.js";
import {
  EVENTS_N, ACT_COOL_MS, pollDelay, alarmLive, alarmActions, actCooldown, alarmWord, bannerWord, armedFor, armProgress, alarmChips,
  recentEvents, recentKey, eventExtras, eventCamera, eventAgo, fixInfo, fmtLatLon, motionInfo, limitText, camInfo,
  rearInfo, screenAction, exposureInfo, tempInfo, throttleInfo, diskInfo, netInfo, levelInfo, radioInfo, speakerInfo,
  nextVolume, VOL_STEP, powerInfo,
  newerAlarm, serverPos, lastTriggerText,
} from "../dash-fmt.js";

const nowS = () => Date.now() / 1000;
const EXPECT_MS = 2500; // after an action, its answer beats telemetry packets already on the way
const EXTRAS_MS = 4000; // snapshot and kept recording are attached to an event a moment after it
const SERVER_POS_MS = 60000; // offline, remote: the server's last point of the van, at most this often

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

  // ───── alarm card: one big button that says the state and does the next step ─────
  // [icon] ARMED                          [🔓 Disarm]
  //        Armed for 13 min · Camera watching · Hazard lights ready
  // Triggered: the word is what happened (IMPACT …); with the siren on, Stop is the big button and Disarm sits beside it.
  const abIc = h("span.db-ab-ic");
  const abWord = h("span.db-ab-word", null, "—");
  const abSub = h("span.db-ab-sub");
  const abInfo = h("span.db-ab-info");
  const abAct = h("span.db-ab-act");
  const abFill = h("span.db-ab-fill", { "aria-hidden": "true" }); // arming: the countdown fills the plate
  const mainBtn = h("button.btn.al-big.db-ab", { type: "button", onclick: () => mainWhat && act(mainWhat) },
    abFill, abIc, h("span.db-ab-txt", null, abWord, abSub, abInfo), abAct);
  let mainWhat = null;
  const extraBtns = h(".db-ab-more");
  const hero = h(".card.db-hero", null,
    h(".card-head", null, h("h2", null, "Alarm"), h("a.db-more", { href: "#/alarm" }, "Settings & log ›")),
    h(".db-ab-row", null, mainBtn, extraBtns));

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

  function setIcon(el, name, size) {
    if (el.dataset.ic === name) return;
    clear(el).append(icon(name, size));
    el.dataset.ic = name;
  }

  function renderHero() {
    const l = live();
    if (!l) {
      cls(mainBtn, "btn al-big db-ab");
      setIcon(abIc, "lock", 34);
      put(abWord, "—");
      put(abSub, stErr || (store.online ? "Loading…" : store.online === null ? "Connecting…" : "No connection to the device"));
      put(abInfo, "");
      abInfo.hidden = true;
      clear(abAct);
      abAct.dataset.sig = "";
      mainWhat = null;
      mainBtn.disabled = true;
      abFill.style.width = "0";
      clear(extraBtns);
      btnSig = "";
      return;
    }
    // no telemetry, and api/alarm failed or was not asked yet (a kept copy): the last known state
    const stale = !store.online && (!!stErr || !stLive);
    const now = nowS();
    const left = l.state === "arming" ? armLeft(l.armIn, l.at, Date.now()) : null;

    // the word: the state; triggered — what happened (IMPACT, TILTED …)
    let last = null;
    if (l.state === "triggered") {
      const ev = st && Array.isArray(st.events) && st.events[0];
      const lt = store.online && store.telemetry && store.telemetry.alarm && store.telemetry.alarm.last;
      last = lt || ev || null;
    }
    put(abWord, l.state === "triggered" && !stale ? bannerWord(last && last.kind) : alarmWord(l.state));
    setIcon(abIc, l.state === "off" ? "unlock" : l.state === "triggered" ? "alarm" : "lock", 34);

    // the line under it: countdown / armed for / what happened and when; siren and lights sounding now
    let sub = "";
    if (l.state === "arming") sub = `${left ?? "…"} s until armed`;
    else if (l.state === "triggered") sub = last ? `${last.title || kindInfo(last.kind).label} · ${eventAgo(last.ts, now)}` : "Something happened";
    else if (l.state === "armed") sub = (st && armedFor(st.armed_at, now)) || "Watching the van";
    else if (l.state === "off") {
      const d = alarmCfg() ? alarmCfg().arm_delay_s : null;
      sub = "Not watching the van" + (isNum(d) ? (d ? ` · arms ${d} s after the tap` : " · arms at once") : "");
    }
    const sounding = [l.siren ? "Siren sounding" : "", l.hazard ? "Hazard lights flashing" : ""].filter(Boolean);
    if (sounding.length) sub += " · " + sounding.join(" · ");
    if (stale) sub = "No connection · last known state" + (l.at > 0 ? ", " + eventAgo(l.at / 1000, now) : "");
    put(abSub, sub);

    // the last line: when the alarm last went off; problems (shock sensor, camera watch, notifications) before it
    const chips = stale ? [] : alarmChips(st, { sensorOk: l.sensorOk, hazardActive: l.hazard })
      .filter((c) => c.kind === "bad" || (c.kind === "warn" && c.key !== "hazard" && c.key !== "vision"));
    const lastTxt = l.state === "triggered" ? "" : lastTriggerText(st, now);
    const csig = JSON.stringify(chips) + lastTxt;
    if (csig !== chipSig) {
      chipSig = csig;
      clear(abInfo);
      chips.forEach((c) => abInfo.append(h(`span.${kindCls(c.kind)}`, { title: c.title || "" }, c.text), " · "));
      if (lastTxt) abInfo.append(h("span", null, lastTxt));
      else if (abInfo.lastChild) abInfo.lastChild.remove(); // the trailing « · »
      abInfo.hidden = !chips.length && !lastTxt;
    }
    const bad = chips.some((c) => c.kind === "bad");

    const k = l.state === "armed" ? "armed" : l.state;
    cls(mainBtn, `btn al-big db-ab s-${k}${stale ? " stale" : ""}${bad ? " problem" : ""}`);
    abFill.style.width = l.state === "arming" && !stale
      ? armProgress(left ?? ((alarmCfg() && alarmCfg().arm_delay_s) || l.armIn || 1), (alarmCfg() && alarmCfg().arm_delay_s) || l.armIn || 1) * 100 + "%"
      : "0";

    // the actions: the first one is the big button itself, the rest sit beside it (Disarm while the siren sounds)
    const acts = alarmActions({ state: l.state, siren: l.siren, hazard: l.hazard, hazardEnabled: hzOn() });
    const sig = acts.map((a) => a.what + ":" + a.label).join("|") + "|" + busy;
    const nowMs = Date.now();
    const cool = !busy && actCooldown(doneAt, nowMs) > 0;
    const inert = cool || store.offline || stale; // the server answers for the device: nothing to arm or disarm there
    // while an action is in flight the buttons stay as they are until its answer — nothing changes under the finger
    if (sig !== btnSig && !(busy && btnSig.endsWith("|" + busy))) {
      btnSig = sig;
      if (cool) { // the set changed again during the cool-down (telemetry caught up): it counts from this swap
        doneAt = nowMs;
        coolSoon();
      }
      const [first, ...rest] = acts;
      mainWhat = first ? first.what : null;
      mainBtn.title = first ? first.title : "";
      clear(abAct);
      if (first) abAct.append(busy === first.what ? h("span.spin") : icon(first.icon), h("span", null, first.label));
      cls(abAct, "db-ab-act" + (first ? " a-" + first.what : ""));
      clear(extraBtns);
      for (const a of rest) {
        extraBtns.append(h(`button.btn.al-big.${a.cls}`, { type: "button", title: a.title, onclick: () => act(a.what) },
          busy === a.what ? h("span.spin") : icon(a.icon), a.label));
      }
      extraBtns.hidden = !rest.length;
    }
    // the cool-down ends: the same buttons come alive (not rebuilt under the finger)
    const dis = !!busy || inert;
    if (mainBtn.disabled !== (dis || !mainWhat)) mainBtn.disabled = dis || !mainWhat;
    for (const b of extraBtns.children) if (b.disabled !== dis) b.disabled = dis;
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

  // power stations (docs/contracts/power.md): the main one's charge with a gauge, the other two in volts
  const pwCol = (label, withBar) => {
    const big = h("span.db-pw-big", null, "—");
    const unit = h("span.db-pw-unit");
    const bar = withBar ? h(".db-batt", null, h("i")) : null;
    const sub = h("span");
    const cells = h("span", { hidden: true }); // Daly: «· Δ 60 mV» в той же строке, что и напряжение
    const subLine = h(".db-ts", null, sub, cells);
    const bal = h(".db-ts", { hidden: true });
    const note = h(".db-ts");
    const volts = h("span.db-pw-cells", { hidden: true, title: "cell voltages, V" });
    const bolt = h("span.db-pw-bolt", { hidden: true, title: "charging" }, icon("bolt")); // charging: a blinking bolt, no word
    return { el: h(".db-pw-col", null, h(".db-pw-lbl", null, label), h(".db-pw-v", null, big, unit, bolt, volts), bar, subLine, bal, note), big, unit, bar, sub, cells, bal, note, volts, bolt };
  };
  const pwMain = pwCol("Fossibot", true);
  const pwSecond = pwCol("Powerbank", false);
  const pwThird = pwCol("Lead battery", false);
  // the top links to the Bluetooth page; the switches below work right here (slide-to-switch, power-controls.js)
  const pwCtl = powerControls();
  const pwTile = h(".db-tile.wide", null,
    h("a.db-rtop", { href: "#/bluetooth" },
      h(".db-tl", null, icon("bolt"), h("span", null, "Power"), h("span.db-go", null, "›")),
      h(".db-power", null, pwMain.el, pwSecond.el, pwThird.el)),
    pwCtl);
  pwTile.hidden = true;

  // air quality (docs/contracts/air.md): the values now, each with a tiny line of the last hour
  const airCells = new Map();
  const airGrid = h(".db-air");
  for (const m of AIR_METRICS) {
    const val = h("span.db-air-v", null, "—");
    const spark = h("span.db-air-s");
    airCells.set(m.key, { val, spark });
    airGrid.append(h(".db-air-c", null, h(".db-air-l", null, m.short || m.label), h(".db-air-n", null, val, h("span.db-air-u", null, m.unit)), spark));
  }
  const airAge = h("span.db-air-age");
  const airTile = h("a.db-tile.wide", { href: "#/air" },
    h(".db-tl", null, icon("air"), h("span", null, "Air"), airAge, h("span.db-go", null, "›")), airGrid);
  airTile.hidden = true;
  let airHist = null;
  let airTimer = null;
  function renderAir(t) {
    const a = t.air;
    airTile.hidden = !a;
    if (!a) return;
    put(airAge, a.age_s == null ? "no data" : "");
    for (const m of AIR_METRICS) {
      const c = airCells.get(m.key);
      put(c.val, fmtValue(m, a[m.key]));
      cls(c.val, "db-air-v " + levelOf(m, a[m.key]));
    }
    if (!airTimer) loadAir();
  }
  async function loadAir() {
    clearTimeout(airTimer);
    airTimer = setTimeout(loadAir, 60000);
    if (document.hidden || !store.online) return;
    try { airHist = await api.get("/api/air/history?hours=1"); } catch { return; }
    if (stopped) return;
    for (const m of AIR_METRICS) airCells.get(m.key).spark.replaceChildren(sparkline(m, airHist[m.key]?.avg || []));
  }

  function renderPower(t) {
    const pi = powerInfo(t.power);
    pwTile.hidden = !pi;
    if (!pi) return;
    pwCtl.render(store.online ? t.power : null); // offline: switches greyed out
    for (const [c, x] of [[pwMain, pi.main], [pwSecond, pi.second], [pwThird, pi.third]]) {
      put(c.big, x.big);
      put(c.unit, x.unit);
      cls(c.big, "db-pw-big " + kindCls(x.kind));
      put(c.sub, x.sub);
      put(c.note, x.note || "");
      cls(c.note, "db-ts " + kindCls(x.noteKind));
      c.note.hidden = !x.note;
      c.bolt.hidden = !x.charging;
      put(c.cells, x.cells || "");
      c.cells.hidden = !x.cells;
      cls(c.cells, "db-cells" + (x.cellsBad ? " red db-blink" : ""));
      put(c.bal, x.balance || "");
      c.bal.hidden = !x.balance;
      cls(c.bal, "db-ts" + (x.balancing ? " green db-balancing" : ""));
      const cv = x.cellVolts || [];
      if (c.volts.dataset.v !== cv.join(" ")) {
        c.volts.dataset.v = cv.join(" ");
        c.volts.replaceChildren(...cv.map((v) => h("span", null, v)));
      }
      c.volts.hidden = !cv.length;
      if (c.bar) {
        c.bar.className = "db-batt " + kindCls(x.kind);
        c.bar.firstChild.style.width = x.frac === null ? "0%" : `${Math.round(x.frac * 100)}%`;
      }
    }
  }

  // radio: the head links to the Radio page; the station with its star, the buttons work right here.
  // Shuffle and Favorites are the radio's mode (settings.radio.mode, like the keypad's mode key): shuffle — the next
  // station at random; favorites — through the favorites; neither — in order.
  const eq = h(".eq.sm", { "aria-hidden": "true" }, h("i"), h("i"), h("i"), h("i"), h("i"));
  const rTitle = h("span.db-station");
  const rLine = h(".db-ts.db-track");
  const spk = h("span.db-spk");
  const vol = h("span.db-vol.mono");
  let rBusy = false;
  let favBusy = false;
  let modeExpect = null; // {mode, until}: the mode just set, until settings over WS show it
  const rBtn = (ic, label, fn) => h("button.btn.db-rbtn", { type: "button", title: label, "aria-label": label, onclick: fn }, icon(ic));
  const prevBtn = rBtn("prev", "Previous station", () => radio("/api/radio/step", { delta: -1 }));
  const nextBtn = rBtn("next", "Next station", () => radio("/api/radio/step", { delta: 1 }));
  const playBtn = rBtn("play", "Play", () => radio(radioInfo(store.telemetry && store.telemetry.radio).active ? "/api/radio/stop" : "/api/radio/resume", {}));
  const shufBtn = rBtn("shuffle", "Shuffle", () => setRadioMode(radioMode() === "random" ? "order" : "random"));
  const favModeBtn = rBtn("favlist", "Play the favorites", () => playFavorites());
  shufBtn.classList.add("db-rmode");
  favModeBtn.classList.add("db-rmode");
  const starBtn = h("button.db-star", { type: "button", onclick: () => toggleFavorite() }, icon("starO"));
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
  const radioTile = h(".db-tile.db-radio", null,
    h("a.db-rtop", { href: "#/radio" },
      h(".db-tl", null, icon("radio"), h("span", null, "Radio"), spk, h("span.db-go", null, "›"))),
    h(".db-rnow", null,
      h(".db-rnow-txt", null, h(".db-np", null, eq, rTitle), rLine),
      starBtn),
    h(".db-rctl", null, prevBtn, playBtn, nextBtn, h("span.db-rsep"), shufBtn, favModeBtn, h("span.grow"),
      h(".db-volgrp", null, volDn, vol, volUp)));

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

  function radioMode() {
    const s = (store.settings && store.settings.radio && store.settings.radio.mode)
      || (store.telemetry && store.telemetry.radio && store.telemetry.radio.mode) || "order";
    if (modeExpect && (modeExpect.mode === s || Date.now() > modeExpect.until)) modeExpect = null;
    return modeExpect ? modeExpect.mode : s;
  }
  async function setRadioMode(mode) {
    modeExpect = { mode, until: Date.now() + 4 * EXPECT_MS };
    renderRadio(false);
    try {
      applySettings(await api.patch("/api/settings", { radio: { mode } }));
      return true;
    } catch (e) {
      modeExpect = null;
      toast("error", "Radio: " + errText(e));
      return false;
    } finally {
      if (!stopped) renderRadio(false);
    }
  }
  // Favorites on: through the favorites from now on; the station playing is not one of them — the first favorite
  // starts at once. Off again: in order.
  async function playFavorites() {
    if (radioMode() === "favorites") { setRadioMode("order"); return; }
    let full;
    try { full = await api.get("/api/radio/state"); } catch (e) { toast("error", "Radio: " + errText(e)); return; }
    if (stopped) return;
    if (!full || !Array.isArray(full.favorites) || !full.favorites.length) {
      toast("warn", "No favorites yet — tap the star next to a station to add it");
      return;
    }
    if (!(await setRadioMode("favorites")) || stopped) return;
    const r = store.telemetry && store.telemetry.radio;
    if (!r || !r.favorite || !radioInfo(r).active) radio("/api/radio/step", { delta: 1 });
  }
  // the star: the station playing into / out of the favorites (the API wants the whole station: the state has it)
  async function toggleFavorite() {
    if (favBusy) return;
    favBusy = true;
    starBtn.disabled = true;
    try {
      const full = await api.get("/api/radio/state");
      const s = full && full.station;
      if (!s) return;
      const fav = (full.favorites || []).some((f) => (f.uuid && f.uuid === s.uuid) || f.url === s.url);
      await api.post(fav ? "/api/radio/favorites/remove" : "/api/radio/favorites/add", { station: s });
      if (!stopped) toast("ok", fav ? `Removed “${s.name}” from favorites` : `“${s.name}” added to favorites`);
    } catch (e) {
      toast("error", "Favorites: " + errText(e));
    } finally {
      favBusy = false;
      starBtn.disabled = false;
    }
  }

  const tiles = h(".db-tiles", null, posTile, camTile, sysTile, lvTile);
  const install = installEntry("button.db-install", "Install app"); // hides itself in the installed app
  // two columns on a wide screen: alarm, radio, air, power on the left; events and the van's tiles on the right.
  // A phone has one column in the same order (the columns dissolve: .db-col display: contents).
  const grid = h(".db-grid", null,
    h(".db-col", null, hero, radioTile, airTile, pwTile),
    h(".db-col", null, evCard, tiles, h(".db-foot", null, install)));

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
    grid.classList.toggle("stale", !store.online);
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

    renderPower(t);
    renderAir(t);
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

    // the star of the station playing; the mode buttons lit as the mode is
    const fav = !!(r && r.favorite);
    if (starBtn.dataset.on !== String(fav)) {
      starBtn.dataset.on = String(fav);
      clear(starBtn).append(icon(fav ? "star" : "starO"));
      starBtn.classList.toggle("on", fav);
      starBtn.setAttribute("aria-pressed", String(fav));
    }
    starBtn.hidden = !(r && r.station);
    starBtn.title = fav ? "Remove the station from favorites" : "Add the station to favorites";
    const mode = radioMode();
    shufBtn.classList.toggle("on", mode === "random");
    shufBtn.setAttribute("aria-pressed", String(mode === "random"));
    shufBtn.title = mode === "random" ? "Shuffle on: the next station is a random one — tap for in order" : "Shuffle off: stations in order — tap for random";
    favModeBtn.classList.toggle("on", mode === "favorites");
    favModeBtn.setAttribute("aria-pressed", String(mode === "favorites"));
    favModeBtn.title = mode === "favorites" ? "Playing the favorites — tap to go back to all stations in order" : "Play the favorites";
    shufBtn.disabled = favModeBtn.disabled = !r;
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
    seenEl,
    errBox,
    grid,
  );

  renderHero();
  renderEvents();
  renderVan();
  fetchNow();

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
  const offConn = on("conn", (o) => {
    renderVan();
    renderHero();
    renderEvents();
    fetchNow();
    if (!o && REMOTE && Date.now() - srvAt > SERVER_POS_MS) loadServerPos();
  });
  const offSet = on("settings", () => { btnSig = ""; renderHero(); renderVan(); });
  // countdown, "armed for", "x min ago" and "Last seen" between packets
  const tick = setInterval(() => { renderHero(); renderWhens(); renderSeen(); }, 1000);
  const onVis = () => { if (!document.hidden) fetchNow(); };
  document.addEventListener("visibilitychange", onVis);

  return () => {
    stopped = true;
    clearTimeout(pollTimer);
    clearTimeout(extrasTimer);
    clearTimeout(coolTimer);
    clearTimeout(volPendingT);
    clearInterval(tick);
    clearTimeout(airTimer);
    document.removeEventListener("visibilitychange", onVis);
    offTel();
    offConn();
    offSet();
    install.dispose();
  };
}
