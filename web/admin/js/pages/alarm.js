// Alarm (docs/contracts/alarm.md): state and Arm / Disarm / Silence, the live vibration level from telemetry
// (alarm.level_g) with the impact threshold and a peak hold — knock on the body to pick impact_g —, the tilt angle
// (GET api/alarm, polled while the page is open), settings.alarm, push notifications on this device (sw.js +
// api/push/* answered by the server behind https …/fnadmin/) and the event log.
import { h, icon, clear, isNum } from "../dom.js";
import { api, errText } from "../api.js";
import { SettingsForm } from "../form.js";
import { store, on } from "../store.js";
import { toast, banner, badge, confirmDialog, poll } from "../ui.js";
import {
  IMPACT_MIN, IMPACT_MAX, SLIDER_STEPS, METER_MIN, METER_MAX, logPos, logTicks, impactToSlider, sliderToImpact,
  fmtG, peakHold, alarmStateText, armLeft, relayText, kindInfo, eventsKey, eventLink, pushErrorText, toUnix,
} from "../alarm-fmt.js";
import { fmtWhen, agoText } from "../tracking-fmt.js";
import * as push from "../push.js";

const POLL_MS = 1500;
const PEAK_HOLD_S = 30;
const nowS = () => Date.now() / 1000;

export function mount(root) {
  root.classList.add("al-page");
  let stopped = false;
  let st = null; // AlarmStatus (GET api/alarm)
  let stAt = 0; // Date.now() when st arrived
  let busy = null; // action in flight
  let peak = null; // {g, at}
  let lastSeq = null;
  let evKey = null;
  let stateSig = "";

  const settings = () => (store.settings && store.settings.alarm) || null;

  // ───── state ─────
  const headBadge = h("span");
  const unavail = h("div", { hidden: true });
  const stIcon = h("span.al-ic");
  const stWord = h(".al-word");
  const stSub = h(".al-sub");
  const armBar = h(".progress.al-armbar", { hidden: true }, h("i"));
  const btns = h(".btnrow.al-btns");
  const stHint = h(".hint");
  const stKv = h(".kv.al-kv");
  const sensorWarn = h("div");

  /** Live part: telemetry while it flows, otherwise the last GET. */
  function live() {
    const t = store.online && store.telemetry ? store.telemetry.alarm : null;
    if (t) return { state: t.state, armIn: t.arm_in_s, siren: !!t.siren, sensorOk: !!t.sensor_ok, at: store.telemetryAt, level: t.level_g, seq: t.seq };
    if (st) return { state: st.state, armIn: st.arm_in_s, siren: !!st.siren, sensorOk: !!(st.sensor && st.sensor.ok), at: stAt, level: st.level_g, seq: null };
    return null;
  }

  async function act(what) {
    if (busy) return;
    busy = what;
    renderState(true);
    try {
      const r = await api.post("/api/alarm/" + what);
      if (stopped) return;
      setStatus(r);
      if (what === "arm") toast("ok", r.state === "arming" ? `Arming — the alarm is on in ${r.arm_in_s ?? "a few"} s` : "Alarm armed");
      else if (what === "disarm") toast("ok", "Alarm off");
      else if (what === "silence") toast("ok", "Siren silenced — the alarm stays armed");
    } catch (e) {
      toast("error", errText(e));
    } finally {
      busy = null;
      if (!stopped) renderState(true);
    }
  }

  function renderState(force) {
    const l = live();
    const left = l && l.state === "arming" ? armLeft(l.armIn, l.at, Date.now()) : null;
    const sen0 = st && st.sensor;
    const sig = l ? [l.state, left, l.siren, l.sensorOk, busy, st && st.armed_at, st && st.relay && st.relay.pending,
      st && st.relay && st.relay.configured, sen0 && sen0.ok, sen0 && sen0.model, sen0 && sen0.error,
      settings() && settings().arm_delay_s].join("|") : "none";
    if (!force && sig === stateSig) return;
    stateSig = sig;
    if (!l) {
      stWord.textContent = "—";
      return;
    }
    const s = alarmStateText(l.state);
    clear(headBadge).append(badge(s.text, s.kind));
    stWord.className = "al-word " + (s.kind || "off") + (l.state === "triggered" ? " blink" : "");
    stWord.textContent = l.state === "triggered" ? "TRIGGERED" : l.state === "arming" ? "ARMING" : l.state === "armed" ? "ARMED" : l.state === "off" ? "OFF" : s.text.toUpperCase();
    clear(stIcon).append(icon(l.state === "off" ? "unlock" : l.state === "triggered" ? "alarm" : "lock", 40));
    stIcon.className = "al-ic " + (s.kind || "off");

    // sub line: countdown / since / what happened
    clear(stSub);
    armBar.hidden = l.state !== "arming";
    if (l.state === "arming") {
      stSub.append(h("span.mono", null, `${left ?? "…"} s`), " until armed");
      const total = Math.max(1, (settings() && settings().arm_delay_s) || l.armIn || 1);
      armBar.firstChild.style.width = Math.max(0, Math.min(100, (1 - (left ?? total) / total) * 100)) + "%";
    } else if ((l.state === "armed" || l.state === "triggered") && st && isNum(st.armed_at)) {
      stSub.append(`Armed ${fmtWhen(st.armed_at, nowS())}`);
    } else if (l.state === "off") {
      stSub.append("Not watching");
    }
    if (l.siren) stSub.append(h("span.al-siren", null, icon("vol", 16), "Siren is sounding"));

    // buttons
    clear(btns);
    const b = (what, cls, ic, text, title) => h("button.btn" + cls, { type: "button", disabled: !!busy, title, onclick: () => act(what) },
      busy === what ? h("span.spin") : icon(ic), text);
    if (l.state === "off") btns.append(b("arm", ".primary.al-big", "lock", "Arm", "Turn the alarm on"));
    else {
      if (l.siren) btns.append(b("silence", ".primary.al-big", "vol", "Silence", "Stop the siren, the alarm stays armed"));
      btns.append(b("disarm", ".danger.al-big", "unlock", "Disarm", "Turn the alarm off"));
    }
    const delay = settings() ? settings().arm_delay_s : null;
    stHint.textContent = l.state === "off"
      ? (delay === 0 ? "The alarm arms at once." : isNum(delay) ? `The alarm arms ${delay} s after you press Arm — time to close the doors and walk away.` : "")
      : l.state === "triggered" ? "Something happened — see the events below. Disarm turns the alarm off and stops the siren." : "";

    // sensor and server
    clear(stKv);
    const sen = st && st.sensor;
    const senTxt = !sen ? (l.sensorOk ? h("span.green", null, "ok") : "—")
      : sen.ok ? h("span.green", null, `${sen.model || "MPU-6050"} · 0x${(sen.address ?? 104).toString(16)}`)
      : h("span.red", null, "not responding");
    const rel = relayText(st && st.relay);
    stKv.append(
      h("span.k", null, "Sensor"), h("span.v", null, senTxt),
      h("span.k", null, "Server"), h("span.v", null, st ? h("span." + (rel.kind === "ok" ? "green" : "amber"), null, rel.text) : "—"),
    );
    clear(sensorWarn);
    if (sen && !sen.ok) {
      sensorWarn.append(banner("warn", h("div", null, h("b", null, "Shock sensor not responding. "),
        "Impact and tilt are not detected; GPS movement still is. ", sen.error ? h("span.mono", null, sen.error) : null)));
    }
  }

  // ───── vibration and tilt ─────
  const fill = h("i.al-fill");
  const thrMark = h("b.al-thr", { title: "Impact threshold" });
  const peakMark = h("b.al-peak", { title: "Peak" });
  const meterMsg = h(".al-meter-msg", { hidden: true }, "No sensor");
  const meter = h(".al-meter", null, h(".al-track", null, fill, thrMark, peakMark, meterMsg),
    h(".al-ticks", null, logTicks([0.01, 0.03, 0.1, 0.3, 1], METER_MIN, METER_MAX)
      .map((t) => h("span", { style: { left: t.p * 100 + "%" } }, t.label))));
  const nowV = h("b.mono");
  const peakV = h("b.mono");
  const peakAgo = h("span.dim");
  const thrV = h("b.mono.amber");
  const tiltV = h("b.mono");
  const tiltOf = h("span.dim");
  const tiltBar = h(".progress.al-tiltbar", null, h("i"));

  function threshold() {
    const v = form && form.fields.has("alarm.impact_g") ? form.get("alarm.impact_g") : settings() && settings().impact_g;
    return isNum(v) ? v : null;
  }

  function renderMeter() {
    const l = live();
    const g = l ? l.level : null;
    const thr = threshold();
    const ok = l ? l.sensorOk && isNum(g) : false;
    if (ok) peak = peakHold(peak, g, nowS(), PEAK_HOLD_S);
    meterMsg.hidden = ok || !l;
    fill.style.width = (ok ? logPos(g, METER_MIN, METER_MAX) * 100 : 0) + "%";
    fill.className = "al-fill" + (ok && thr && g >= thr ? " hit" : ok && thr && g >= thr / 2 ? " near" : "");
    thrMark.hidden = !thr;
    if (thr) thrMark.style.left = logPos(thr, METER_MIN, METER_MAX) * 100 + "%";
    peakMark.hidden = !peak;
    if (peak) {
      peakMark.style.left = logPos(peak.g, METER_MIN, METER_MAX) * 100 + "%";
      peakMark.classList.toggle("hit", !!thr && peak.g >= thr);
    }
    nowV.textContent = ok ? fmtG(g) : "—";
    peakV.textContent = peak ? fmtG(peak.g) : "—";
    peakAgo.textContent = peak && nowS() - peak.at >= 1 ? ` (${Math.round(nowS() - peak.at)} s ago)` : "";
    thrV.textContent = thr ? fmtG(thr) : "—";
  }

  function renderTilt() {
    const s = settings();
    const t = st ? st.tilt_deg : null;
    const lim = s && isNum(s.tilt_deg) ? s.tilt_deg : null;
    tiltV.textContent = isNum(t) ? t.toFixed(1) + "°" : "—";
    if (s && s.tilt_enabled === false) tiltOf.textContent = " (tilt watch is off)";
    else if (!isNum(t)) tiltOf.textContent = " (measured from the moment the alarm is armed)";
    else tiltOf.textContent = lim ? ` of ${lim}°` : "";
    tiltBar.hidden = !isNum(t) || !lim;
    if (isNum(t) && lim) {
      tiltBar.firstChild.style.width = Math.min(100, (t / lim) * 100) + "%";
      tiltBar.classList.toggle("hit", t >= lim);
    }
  }

  // ───── events ─────
  const evCount = h("span.dim.mono");
  const evList = h(".al-events");
  const testBtn = h("button.btn.sm", { type: "button", onclick: () => sendTestAlarm() }, icon("bell"), "Send test alarm");
  const clearBtn = h("button.btn.sm.danger", { type: "button", onclick: () => clearEvents() }, icon("trash"), "Clear");

  function renderEvents(force) {
    const list = (st && st.events) || [];
    const k = eventsKey(list);
    if (!force && k === evKey) return;
    evKey = k;
    evCount.textContent = list.length ? String(list.length) : "";
    clearBtn.disabled = !list.length;
    clear(evList);
    if (!list.length) {
      evList.append(h(".al-empty.dim", null, "No events yet."));
      return;
    }
    const now = nowS();
    for (const ev of list) {
      const ki = kindInfo(ev.kind);
      const link = eventLink(ev);
      const seg = ev.data && typeof ev.data.segment === "string" && ev.data.segment ? ev.data.segment : null;
      evList.append(h(".al-ev." + (ki.kind || "plain"), null,
        h("span.ic", null, icon(ki.icon)),
        h(".txt", null,
          h(".tt", null, ev.title || ki.label),
          ev.body ? h(".bd", null, ev.body) : null,
          link || seg ? h(".ex", null,
            link ? h("a", { href: link.href }, link.text) : null,
            seg ? h("span", null, "Recording kept: ", h("a.mono", { href: "#/recordings" }, seg)) : null) : null),
        h(".when.mono", { title: isNum(ev.ts) ? new Date(ev.ts * 1000).toLocaleString() : "" }, isNum(ev.ts) ? fmtWhen(ev.ts, now) : "—"),
      ));
    }
  }

  async function sendTestAlarm() {
    testBtn.disabled = true;
    try {
      setStatus(await api.post("/api/alarm/test"));
      const rel = st && st.relay;
      if (rel && !rel.configured) toast("warn", "Test event logged, but the device has no server link — no notification goes out.");
      else toast("ok", "Test alarm sent — subscribed devices get a notification in a few seconds.");
    } catch (e) {
      toast("error", errText(e));
    } finally {
      testBtn.disabled = false;
    }
  }

  async function clearEvents() {
    if (!(await confirmDialog({ title: "Clear the event log?", text: "All alarm events on the device are deleted. Protected recordings stay protected.", okText: "Clear", danger: true }))) return;
    try {
      setStatus(await api.del("/api/alarm/events"));
      toast("ok", "Event log cleared");
    } catch (e) {
      toast("error", errText(e));
    }
  }

  // ───── data ─────
  function setStatus(s) {
    if (!s || typeof s !== "object") return;
    st = s;
    stAt = Date.now();
    unavail.hidden = true;
    renderState(false); // rebuilt only when something shown changed: a button must not vanish under the finger
    renderTilt();
    renderEvents(false);
  }

  async function load() {
    try {
      const s = await api.get("/api/alarm");
      if (!stopped) setStatus(s);
    } catch (e) {
      if (stopped) return;
      clear(unavail).append(banner("bad", e.status === 404
        ? "The alarm is not available on this device yet — update the device software."
        : "Alarm: " + errText(e)));
      unavail.hidden = false;
    }
  }

  // ───── notifications ─────
  const nBox = h("div");
  const devBox = h(".al-devs");
  let pmode = push.currentMode();
  let reg = null;
  let regErr = null;
  let key = null;
  let keyErr = null;
  let subs = null;
  let subsErr = null;
  let sub = null;
  let nBusy = null;
  let nErr = null;

  async function loadPush() {
    if (pmode === "insecure") return renderPush();
    if (pmode === "ok") {
      try { reg = await push.swReady(); regErr = null; } catch (e) { regErr = e; }
      sub = await push.currentSub(reg);
    }
    try { key = await push.fetchKey(); keyErr = null; } catch (e) { key = null; keyErr = e; }
    try { subs = await push.listSubs(); subsErr = null; } catch (e) { subs = null; subsErr = e; }
    if (!stopped) renderPush();
  }

  function enable() {
    // the permission request is the very first thing in the click handler: Safari asks only inside a user gesture
    const permP = Notification.requestPermission();
    nErr = null;
    nBusy = "enable";
    renderPush();
    (async () => {
      try {
        if (!key) key = await push.fetchKey();
        if (!reg) reg = await push.swReady();
        await push.subscribe(permP, key, reg);
        toast("ok", "Notifications are on for this device");
      } catch (e) {
        nErr = pushErrorText(e);
      }
      nBusy = null;
      await loadPush();
    })();
  }

  async function disable() {
    nErr = null;
    nBusy = "disable";
    renderPush();
    try {
      await push.unsubscribe(reg);
      toast("ok", "Notifications are off for this device");
    } catch (e) {
      nErr = pushErrorText(e);
    }
    nBusy = null;
    await loadPush();
  }

  async function testPush() {
    nErr = null;
    nBusy = "test";
    renderPush();
    try {
      const r = await push.sendTest(sub && sub.endpoint);
      if (r && r.sent > 0 && !r.failed) toast("ok", "Test notification sent — it should appear in a few seconds");
      else nErr = "Not delivered" + (r && r.errors && r.errors.length ? ": " + r.errors.join("; ") : "");
    } catch (e) {
      nErr = pushErrorText(e);
    }
    nBusy = null;
    await loadPush();
  }

  async function removeSub(s) {
    const mine = s.id === push.storedId();
    const ok = await confirmDialog({
      title: "Remove this device?",
      text: h("div", null, "“", h("b", null, s.name || "Unnamed device"), "” will no longer get alarm notifications.", mine ? " This is the device you are using now." : ""),
      okText: "Remove", danger: true,
    });
    if (!ok) return;
    try {
      await push.deleteSub(s.id);
      if (mine) {
        push.forgetId();
        if (sub) { try { await sub.unsubscribe(); } catch { /* */ } }
      }
      toast("ok", "Device removed");
    } catch (e) {
      toast("error", pushErrorText(e));
    }
    await loadPush();
  }

  function renderPush() {
    clear(nBox);
    clear(devBox);
    const btn = (what, cls, ic, text, fn, disabled) => h("button.btn" + cls, { type: "button", disabled: !!nBusy || disabled, onclick: fn },
      nBusy === what ? h("span.spin") : icon(ic), text);

    if (pmode === "insecure") {
      nBox.append(
        h("p.al-p", null, "Phones show notifications only for sites opened over a secure (https) connection, and the device's own address is plain http."),
        h("p.al-p", null, "Open the remote admin ", h("code", null, "https://<your server>/fnadmin/"),
          " on the phone, sign in and enable notifications on this page there. On iPhone add it to the Home Screen first and open it from there."),
        h(".hint", null, "Send test alarm (in Events) works from here too: it goes through the device to the server."),
      );
      return;
    }
    if (pmode === "ios-install") {
      nBox.append(
        h("p.al-p", null, "On iPhone and iPad, notifications work only in the app added to the Home Screen (iOS 16.4 or newer):"),
        h("ol.al-steps", null,
          h("li", null, "Tap ", h("b", null, "Share"), " (the square with an arrow) in Safari."),
          h("li", null, "Choose ", h("b", null, "Add to Home Screen"), ", then ", h("b", null, "Add"), "."),
          h("li", null, "Open ", h("b", null, "fallout.nav"), " from the Home Screen and sign in — the app keeps its own login."),
          h("li", null, "Open ", h("b", null, "Alarm"), " and tap ", h("b", null, "Enable notifications on this device"), ".")),
      );
    } else if (pmode === "unsupported") {
      nBox.append(banner("warn", push.pushEnv().ios
        ? "This iPhone cannot receive web notifications: update to iOS 16.4 or newer and open fallout.nav from the Home Screen."
        : "This browser cannot receive push notifications. Use Chrome, Edge, Firefox or Safari (on iPhone — the Home Screen app)."));
    } else {
      // ok
      const perm = typeof Notification !== "undefined" ? Notification.permission : "default";
      const myId = push.storedId();
      const known = !!(sub && subs && myId && subs.some((s) => s.id === myId));
      const serverErr = keyErr ? pushErrorText(keyErr) : null;
      if (serverErr) nBox.append(banner("bad", serverErr));
      else if (regErr) nBox.append(banner("bad", "Service worker: " + errText(regErr)));
      if (perm === "denied") {
        nBox.append(banner("warn", "Notifications are blocked for this site. Allow them in the browser's site settings, then press Enable again."));
      }
      if (known) {
        nBox.append(h(".al-pstate", null, h("span.al-ic.ok", null, icon("bell", 26)),
          h("div", null, h("b", null, "On for this device"), h(".dim", null, push.thisDeviceName()))));
        nBox.append(h(".btnrow", null,
          btn("test", ".primary", "bell", "Send test notification", testPush),
          btn("disable", "", "x", "Disable", disable)));
      } else {
        nBox.append(h(".al-pstate", null, h("span.al-ic.off", null, icon("bell", 26)),
          h("div", null, h("b", null, "Off for this device"), h(".dim", null, "Get a notification on this " + (push.pushEnv().ios ? "iPhone" : "device") + " when the alarm goes off."))));
        nBox.append(h(".btnrow", null, btn("enable", ".primary", "bell", "Enable notifications on this device", enable, !!serverErr || !!regErr)));
      }
    }
    if (nErr) nBox.append(h(".al-perr", null, nErr));

    // subscribed devices (any secure page can manage them); the same server error is already shown above
    if (subsErr && keyErr && (pmode === "ok" || pushErrorText(subsErr) === pushErrorText(keyErr))) {
      if (pmode !== "ok") devBox.append(h(".dim", null, pushErrorText(subsErr)));
      return;
    }
    devBox.append(h("h3", null, "Devices that get notifications"));
    if (subsErr) {
      devBox.append(h(".dim", null, pushErrorText(subsErr)));
      return;
    }
    if (!subs) {
      devBox.append(h(".dim", null, "Loading…"));
      return;
    }
    if (!subs.length) {
      devBox.append(h(".dim", null, "None yet."));
      return;
    }
    const now = nowS();
    const myId = push.storedId();
    for (const s of subs) {
      const created = toUnix(s.created);
      const okAt = toUnix(s.last_ok);
      devBox.append(h(".al-dev", null,
        h(".who", null,
          h(".nm", null, s.name || "Unnamed device", s.id === myId && sub ? badge("this device", "ok") : null),
          h(".dim.sm", null, [s.host, created ? "added " + fmtWhen(created, now) : null, okAt ? "last delivered " + agoText(okAt, now) : "nothing delivered yet"].filter(Boolean).join(" · ")),
          s.last_error ? h(".red.sm", null, s.last_error) : null),
        h("button.btn.sm.icon.danger", { type: "button", title: "Remove", onclick: () => removeSub(s) }, icon("trash")),
      ));
    }
  }

  // ───── settings ─────
  let form = null;
  let setCard;
  if (store.settings && !store.settings.alarm) {
    setCard = h(".card", null, h("h2", null, "Settings"), banner("warn", "This device has no alarm settings yet — update the device software."));
  } else {
    form = new SettingsForm();
    const f = (s) => form.add(s);
    const impactWidget = (fd) => {
      const rng = h("input", { type: "range", min: 0, max: SLIDER_STEPS, step: 1, "aria-label": "Impact threshold" });
      const num = h("input", { type: "number", min: IMPACT_MIN, max: IMPACT_MAX, step: "any", inputmode: "decimal" });
      rng.addEventListener("input", () => {
        const g = sliderToImpact(rng.value);
        num.value = String(g);
        num.classList.remove("invalid");
        fd.change(g);
        renderMeter();
      });
      num.addEventListener("input", () => {
        const raw = num.value.trim();
        const v = raw === "" ? NaN : Number(raw);
        if (Number.isFinite(v)) rng.value = String(impactToSlider(v));
        num.classList.toggle("invalid", !Number.isFinite(v));
        fd.change(v);
        renderMeter();
      });
      const ticks = h(".al-ticks", null, logTicks([0.02, 0.05, 0.1, 0.2, 0.5, 1, 2], IMPACT_MIN, IMPACT_MAX)
        .map((t) => h("span", { style: { left: `calc(8px + ${t.p} * (100% - 16px))` } }, t.label)));
      return {
        el: h(".al-impact", null, h(".al-rng", null, rng, ticks), h("span.al-num", null, num, h("span.unit", null, "g"))),
        update: (v) => { rng.value = String(impactToSlider(isNum(v) ? v : 0.15)); num.value = isNum(v) ? String(v) : ""; num.classList.remove("invalid"); },
      };
    };
    const int = (v) => (Number.isInteger(v) ? null : "Whole number");
    setCard = h(".card", null, h("h2", null, "Settings"),
      f({ path: "alarm.impact_g", label: "Impact threshold", hint: "a knock or a hit this strong is an alarm", type: "custom", build: impactWidget,
        validate: (v) => (!isNum(v) ? "Enter a number" : v < IMPACT_MIN || v > IMPACT_MAX ? `Between ${IMPACT_MIN} and ${IMPACT_MAX} g` : null),
        help: "Knock on the body with this page open and watch the peak on the vibration meter: set the threshold above what wind and passing trucks give, below a real hit." }),
      f({ path: "alarm.tilt_enabled", label: "Watch tilt", type: "switch", switchLabel: "jacking up, towing" }),
      f({ path: "alarm.tilt_deg", label: "Tilt threshold", type: "number", min: 0.5, max: 45, step: 0.1, unit: "°", help: "Held for 5 s, measured from the position when the alarm was armed." }),
      f({ path: "alarm.move_enabled", label: "Watch GPS position", type: "switch", switchLabel: "the van leaves the parking spot" }),
      f({ path: "alarm.move_m", label: "Movement distance", type: "number", min: 20, max: 5000, step: 10, unit: "m", validate: int }),
      f({ path: "alarm.siren", label: "Siren", type: "switch", switchLabel: "through the Bluetooth speaker" }),
      f({ path: "alarm.siren_s", label: "Siren length", type: "number", min: 5, max: 300, step: 1, unit: "s", validate: int }),
      f({ path: "alarm.arm_delay_s", label: "Arm delay", hint: "after pressing Arm", type: "number", min: 0, max: 600, step: 1, unit: "s", validate: int }),
      f({ path: "alarm.push_cooldown_s", label: "Repeat notifications", hint: "of the same kind, at most every", type: "number", min: 0, max: 3600, step: 1, unit: "s", validate: int,
        help: "Every event goes into the log; only the notifications are limited." }),
      f({ path: "alarm.notify_offline", label: "Van went offline", type: "switch", switchLabel: "notify if the van drops off the network while armed",
        help: "Sent by the server after 3 minutes without contact (power cut, jammer, no mobile network)." }),
      h("details.al-adv", null, h("summary", null, "Sensor connection"),
        f({ path: "alarm.sensor_bus", label: "I²C bus", type: "number", min: 0, max: 30, step: 1, validate: int, help: "The same bus as the compass (1 on the Raspberry Pi header)." }),
        f({ path: "alarm.sensor_address", label: "Sensor address", type: "select",
          options: [{ value: 104, label: "0x68 — AD0 to GND (default)" }, { value: 105, label: "0x69 — AD0 to 3.3 V" }] })),
      form.bar,
    );
  }

  // ───── layout ─────
  root.append(
    h(".page-head", null, h("h1", null, "Alarm"), headBadge),
    unavail,
    h(".grid.g2.al-top", null,
      h(".card.al-state", null,
        h("h2", null, "State"),
        h(".al-main", null, stIcon, h("div", null, stWord, stSub)),
        armBar,
        btns,
        stHint,
        sensorWarn,
        stKv),
      h(".card", null,
        h(".card-head", null, h("h2", null, "Vibration"),
          h("button.btn.sm", { type: "button", title: "Forget the peak", onclick: () => { peak = null; renderMeter(); } }, "Reset peak")),
        meter,
        h(".al-nums", null,
          h("span", null, "Now ", nowV),
          h("span", null, "Peak ", peakV, peakAgo),
          h("span", null, "Threshold ", thrV)),
        h(".sep"),
        h(".al-tilt", null, h("span", null, "Tilt ", tiltV, tiltOf), tiltBar),
        h(".hint", null, "Live from the shock sensor: the strongest jolt of every moment, in g on a log scale. The amber mark is the impact threshold."))),
    h(".grid.g2", null,
      h(".card", null,
        h(".card-head.al-evhead", null, h("h2", null, "Events ", evCount), testBtn, clearBtn),
        evList),
      h(".card", null,
        h("h2", null, "Notifications"),
        nBox,
        devBox)),
    setCard,
  );

  renderState(true);
  renderMeter();
  renderTilt();
  renderEvents(true);
  renderPush();

  const offTel = on("telemetry", () => {
    renderState(false);
    renderMeter();
    const l = live();
    if (l && l.seq !== null && l.seq !== undefined) {
      if (lastSeq !== null && l.seq !== lastSeq) load(); // a new event: fetch the log now
      lastSeq = l.seq;
    }
  });
  const offConn = on("conn", () => renderState(true));
  const offSet = on("settings", () => { renderState(true); renderMeter(); renderTilt(); });
  // countdown and "x s ago" between telemetry packets (remote telemetry is 2 Hz, the poll 1.5 s)
  const tick = setInterval(() => { renderState(false); if (!store.online) renderMeter(); }, 500);
  const stopPoll = poll(load, POLL_MS);
  loadPush();

  return () => {
    stopped = true;
    stopPoll();
    clearInterval(tick);
    offTel();
    offConn();
    offSet();
    if (form) form.destroy();
  };
}
