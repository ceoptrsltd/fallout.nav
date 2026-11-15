// Tuner: a six-string guitar (acoustic or electric) tuned by the microphone of this phone / computer. Everything runs
// in the browser (Web Audio): nothing goes to the device. Pitch and the maths — tuner-fmt.js.
//
// The microphone needs a secure page (https: the remote admin or the installed app); on the device's plain http
// address the browser has no microphone — then only the reference tones work.
import { h, icon, clear } from "../dom.js";
import { errText } from "../api.js";
import { toast, banner } from "../ui.js";
import {
  TUNINGS, tuningById, noteHz, centsOff, nearestNote, noteLabel, nearestString, stringNumber, tuneLevel, tuneHint,
  fmtCents, needleDeg, detectPitch, decimate2, rms, PitchTracker, NeedleSpring, clampA4, A4_DEFAULT, A4_MIN, A4_MAX,
  IN_TUNE_CENTS, CLOSE_CENTS, NEEDLE_CENTS,
} from "../tuner-fmt.js";

const KEY = "fnav.tuner"; // {tuning, a4} — this browser's choice
const ANALYSE_MS = 45; // a reading ~22 times a second
const TUNED_MS = 600; // in tune this long — the string gets its tick
const TONE_S = 2.6;

function loadPrefs() {
  try {
    const p = JSON.parse(localStorage.getItem(KEY) || "{}");
    return { tuning: tuningById(p.tuning).id, a4: clampA4(p.a4 ?? A4_DEFAULT) };
  } catch {
    return { tuning: "standard", a4: A4_DEFAULT };
  }
}

// ───── the gauge: ±50 ¢ on an arc, the green and amber zones, the needle ─────
const CX = 160, CY = 168, R = 138, SPAN = 60;
function pt(r, deg) {
  const a = (deg * Math.PI) / 180;
  return [CX + r * Math.sin(a), CY - r * Math.cos(a)];
}
function arc(r, d0, d1) {
  const [x0, y0] = pt(r, d0), [x1, y1] = pt(r, d1);
  return `M${x0.toFixed(1)} ${y0.toFixed(1)}A${r} ${r} 0 0 1 ${x1.toFixed(1)} ${y1.toFixed(1)}`;
}
function gauge() {
  const svg = h("svg.tn-gauge", { viewBox: "0 0 320 182", "aria-hidden": "true" });
  const deg = (c) => needleDeg(c, SPAN);
  svg.append(
    h("path.tn-arc", { d: arc(R, -SPAN, SPAN) }),
    h("path.tn-zone.close", { d: arc(R, deg(-CLOSE_CENTS), deg(CLOSE_CENTS)) }),
    h("path.tn-zone.good", { d: arc(R, deg(-IN_TUNE_CENTS), deg(IN_TUNE_CENTS)) }),
  );
  for (let c = -NEEDLE_CENTS; c <= NEEDLE_CENTS; c += 5) {
    const major = c % 25 === 0;
    const [x0, y0] = pt(R - (major ? 16 : 9), deg(c)), [x1, y1] = pt(R - 3, deg(c));
    svg.append(h("line.tn-tick" + (major ? ".major" : ""), { x1: x0, y1: y0, x2: x1, y2: y1 }));
    if (major) {
      const [tx, ty] = pt(R + 13, deg(c));
      svg.append(h("text.tn-tlbl", { x: tx, y: ty + 4, "text-anchor": "middle" }, c === 0 ? "0" : (c > 0 ? "+" : "−") + Math.abs(c)));
    }
  }
  svg.append(h("text.tn-flat", { x: 26, y: 176, "text-anchor": "middle" }, "♭"), h("text.tn-sharp", { x: 294, y: 176, "text-anchor": "middle" }, "♯"));
  const needle = h("g.tn-needle", null,
    h("line", { x1: CX, y1: CY, x2: CX, y2: CY - R + 8 }),
    h("circle", { cx: CX, cy: CY, r: 7 }));
  svg.append(needle);
  svg.setNeedle = (d, cls) => {
    needle.setAttribute("transform", `rotate(${d.toFixed(2)} ${CX} ${CY})`);
    needle.setAttribute("class", "tn-needle " + cls);
  };
  return svg;
}

export function mount(root) {
  root.classList.add("tn-page");
  let prefs = loadPrefs();
  let tuning = tuningById(prefs.tuning);
  let pick = null; // a string tapped by hand (index); null — Auto: the string nearest to what sounds
  let target = 0; // the string tuned now
  const tuned = new Set(); // strings that were in tune this visit
  let inTuneSince = null;
  const spring = new NeedleSpring(); // the needle's cents, on a damped spring
  let lastFrame = null;
  const tracker = new PitchTracker();

  // audio
  const secure = window.isSecureContext === true && !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  let ac = null;
  let stream = null;
  let nodes = [];
  let analyser = null;
  let raw = null;
  let raf = 0;
  let timer = 0;
  let level = 0;
  let listening = false;
  let starting = false;
  let wake = null;
  let stopped = false;

  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(prefs)); } catch { /* private mode */ } };

  // ───── the display ─────
  const g = gauge();
  const noteEl = h("span.tn-note", null, "—");
  const octEl = h("span.tn-oct");
  const centsEl = h("span.tn-cents", null, "— ¢");
  const hzEl = h("span.tn-hz");
  const hintEl = h(".tn-hint");
  const targetEl = h(".tn-target");
  const display = h(".tn-display", null,
    g,
    h(".tn-read", null, h(".tn-notebox", null, noteEl, octEl), h(".tn-nums", null, centsEl, hzEl)),
    hintEl,
    targetEl);

  // strings: as on the headstock, the 6th (lowest) on the left; Auto picks the one that sounds
  const autoBtn = h("button.btn.tn-auto", { type: "button", title: "Tune whichever string sounds", onclick: () => { pick = null; render(); } }, "Auto");
  const strBox = h(".tn-strings");
  function buildStrings() {
    clear(strBox);
    tuning.notes.forEach((m, i) => {
      strBox.append(h("button.tn-str", { type: "button", dataset: { i }, title: `String ${stringNumber(i)}: ${noteLabel(m)}`,
        onclick: () => { pick = pick === i ? null : i; target = i; inTuneSince = null; render(); } },
      h("span.tn-str-n", null, noteLabel(m).replace(/\d+$/, "")), h("span.tn-str-k", null, String(stringNumber(i))), h("span.tn-str-ok", null, icon("check"))));
    });
  }

  // ───── controls ─────
  const micBtn = h("button.btn.primary.tn-mic", { type: "button", onclick: () => (listening ? stopMic() : startMic()) });
  const toneBtn = h("button.btn.tn-tone", { type: "button", title: "Play the string's note to tune by ear", onclick: () => playTone(tuning.notes[target]) }, icon("vol"), h("span", null, "Tone"));
  const meter = h(".tn-meter", { title: "Microphone level" }, h("i"));
  const tunSel = h("select.tn-sel", { "aria-label": "Tuning", onchange: () => {
    prefs = { ...prefs, tuning: tunSel.value };
    tuning = tuningById(prefs.tuning);
    save();
    tuned.clear();
    pick = null;
    buildStrings();
    render();
  } }, TUNINGS.map((t) => h("option", { value: t.id, selected: t.id === tuning.id }, `${t.label} — ${t.notes.map((m) => noteLabel(m).replace(/\d+$/, "")).join(" ")}`)));
  const a4Val = h("span.tn-a4v.mono");
  const setA4 = (v) => {
    prefs = { ...prefs, a4: clampA4(v) };
    save();
    tuned.clear();
    render();
  };
  const a4Box = h(".tn-a4", { title: `The pitch of A4 everything is tuned from (${A4_MIN}–${A4_MAX} Hz; usually 440)` },
    h("span.tn-lbl", null, "A4"),
    h("button.btn.sm.icon", { type: "button", "aria-label": "Lower A4", onclick: () => setA4(prefs.a4 - 1) }, icon("minus")),
    a4Val,
    h("button.btn.sm.icon", { type: "button", "aria-label": "Raise A4", onclick: () => setA4(prefs.a4 + 1) }, icon("plus")));

  const msg = h("div");
  const card = h(".card.tn-card", null,
    h(".tn-bar", null, h("label.tn-tun", null, h("span.tn-lbl", null, "Tuning"), tunSel), a4Box),
    display,
    h(".tn-strrow", null, strBox, autoBtn),
    h(".tn-ctl", null, micBtn, meter, toneBtn));
  const help = h(".card.tn-help", null,
    h("h2", null, "How to tune"),
    h("ul", null,
      h("li", null, "Tap ", h("b", null, "Start"), " and allow the microphone. Hold the phone 20–40 cm from the guitar: an acoustic near the "
        + "sound hole, an electric near its amp — or unplugged, near the pickups."),
      h("li", null, "Pluck one string at a time and let it ring. ", h("b", null, "Auto"), " takes the nearest string; tap a string to tune exactly "
        + "that one (a string far out of tune, or a new set)."),
      h("li", null, "Needle left (♭) — the string is low: tighten the peg. Right (♯) — loosen. Green — in tune (±" + IN_TUNE_CENTS
        + " cents); the string gets a tick."),
      h("li", null, h("b", null, "Tone"), " plays the string's note, to tune by ear. The other tunings and A4 are kept in this browser.")));

  root.append(h(".page-head", null, h("h1", null, "Tuner")), msg, card, help);
  if (!secure) {
    msg.append(banner("warn", [h("b", null, "No microphone on this page. "),
      "Browsers give the microphone only to secure (https) pages: open the tuner through the remote address "
      + "(https://<your server>/fnadmin/) or the installed app. The reference tones work here too."]));
  }

  // ───── audio ─────
  async function startMic() {
    if (!secure || listening || starting) return;
    starting = true;
    render();
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      ac = ac || new AC(); // made in the tap itself: iOS lets audio start only from a gesture
      const resumed = ac.resume();
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false }, // they bend the pitch
      });
      await resumed;
      if (stopped) { stopTracks(); return; }
      const sr = ac.sampleRate;
      const src = ac.createMediaStreamSource(stream);
      const hp = ac.createBiquadFilter(); // rumble: the van, the wind, the hand on the phone
      hp.type = "highpass";
      hp.frequency.value = 50;
      const lp = ac.createBiquadFilter(); // above the strings: less hiss, and no aliasing when every 2nd sample is taken
      lp.type = "lowpass";
      lp.frequency.value = Math.min(1500, sr / 8);
      analyser = ac.createAnalyser();
      analyser.fftSize = sr > 60000 ? 8192 : sr >= 32000 ? 4096 : 2048;
      analyser.smoothingTimeConstant = 0;
      src.connect(hp);
      hp.connect(lp);
      lp.connect(analyser);
      nodes = [src, hp, lp, analyser];
      raw = new Float32Array(analyser.fftSize);
      listening = true;
      tracker.reset();
      try { wake = navigator.wakeLock ? await navigator.wakeLock.request("screen") : null; } catch { wake = null; } // the screen stays on
      timer = setInterval(tick, ANALYSE_MS);
      loop();
    } catch (e) {
      stopTracks();
      const n = e && e.name;
      toast("error", n === "NotAllowedError" || n === "SecurityError"
        ? "The microphone is not allowed — allow it for this site in the browser's settings"
        : n === "NotFoundError" ? "No microphone found" : "Microphone: " + errText(e));
    } finally {
      starting = false;
      if (!stopped) render();
    }
  }

  function stopTracks() {
    if (stream) for (const t of stream.getTracks()) t.stop();
    stream = null;
  }

  function stopMic() {
    cancelAnimationFrame(raf);
    raf = 0;
    clearInterval(timer);
    timer = 0;
    for (const n of nodes) { try { n.disconnect(); } catch { /* already */ } }
    nodes = [];
    analyser = null;
    stopTracks();
    listening = false;
    reading = { hz: null, live: false, held: false };
    level = 0;
    tracker.reset();
    spring.reset(0);
    lastFrame = null;
    inTuneSince = null;
    if (wake) { wake.release().catch(() => {}); wake = null; }
    if (!stopped) render();
  }

  function analyse(now) {
    analyser.getFloatTimeDomainData(raw);
    level = Math.min(1, rms(raw) * 6);
    let buf = raw;
    let sr = ac.sampleRate;
    while (sr >= 32000) { // ~24 kHz is plenty for the strings: a quarter of the work
      buf = decimate2(buf);
      sr /= 2;
    }
    const p = detectPitch(buf, sr);
    return tracker.feed(p ? p.hz : null, now);
  }

  // the sound is read on its own clock; the needle is drawn every frame (and by the reading itself when the
  // browser holds the frames back)
  let reading = { hz: null, live: false, held: false };
  function tick() {
    if (!listening) return;
    const now = performance.now();
    reading = analyse(now);
    if (lastFrame === null || now - lastFrame > 120) render(now);
  }
  function loop(now = performance.now()) {
    if (!listening) return;
    render(now);
    raf = requestAnimationFrame(loop);
  }

  function playTone(midi) {
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      ac = ac || new AC();
      ac.resume();
      const t = ac.currentTime;
      const osc = ac.createOscillator();
      const osc2 = ac.createOscillator(); // an octave up, quieter: a low E is heard on a phone speaker too
      const gn = ac.createGain();
      const gn2 = ac.createGain();
      osc.type = "triangle";
      osc.frequency.value = noteHz(midi, prefs.a4);
      osc2.type = "sine";
      osc2.frequency.value = noteHz(midi, prefs.a4) * 2;
      gn.gain.setValueAtTime(0.0001, t);
      gn.gain.exponentialRampToValueAtTime(0.35, t + 0.02);
      gn.gain.exponentialRampToValueAtTime(0.0001, t + TONE_S);
      gn2.gain.value = 0.35;
      osc.connect(gn);
      osc2.connect(gn2);
      gn2.connect(gn);
      gn.connect(ac.destination);
      osc.start(t);
      osc2.start(t);
      osc.stop(t + TONE_S + 0.05);
      osc2.stop(t + TONE_S + 0.05);
    } catch (e) {
      toast("error", "Tone: " + errText(e));
    }
  }

  // ───── render ─────
  function put(el, text) {
    if (el.textContent !== text) el.textContent = text;
  }

  function render(now = performance.now()) {
    const a4 = prefs.a4;
    put(a4Val, `${a4} Hz`);
    const hz = listening ? reading.hz : null;
    if (hz !== null && pick === null) target = nearestString(hz, tuning.notes, a4);
    if (pick !== null) target = pick;
    const tMidi = tuning.notes[target];
    const tHz = noteHz(tMidi, a4);
    const raw = hz !== null ? centsOff(hz, tHz) : null;
    // the needle swings to the reading; the string died away — back to the middle, slower
    const dt = lastFrame === null ? 0 : (now - lastFrame) / 1000;
    lastFrame = now;
    spring.omega = hz !== null ? 7.5 : 4;
    const pos = spring.step(hz !== null ? Math.max(-1.2 * NEEDLE_CENTS, Math.min(1.2 * NEEDLE_CENTS, raw)) : 0, dt);
    // the numbers and colours follow the needle (no flicker); a string far out — the reading itself
    const cents = hz === null ? null : Math.abs(raw) <= NEEDLE_CENTS ? pos : raw;
    const lvl = tuneLevel(cents);
    const live = listening && reading.live;

    // in tune for a moment — the string gets its tick
    if (live && lvl === "good") {
      if (inTuneSince === null) inTuneSince = now;
      else if (now - inTuneSince >= TUNED_MS) tuned.add(target);
    } else if (!reading.held || lvl !== "good") inTuneSince = null;

    g.setNeedle(needleDeg(pos, SPAN), (hz === null ? "idle" : lvl) + (reading.held && !reading.live ? " held" : ""));
    const n = nearestNote(hz, a4);
    put(noteEl, n ? n.name : listening ? "…" : "—");
    put(octEl, n ? String(n.octave) : "");
    noteEl.className = "tn-note " + (hz !== null ? lvl : "");
    put(centsEl, hz !== null ? fmtCents(cents) : "— ¢");
    centsEl.className = "tn-cents " + (hz !== null ? lvl : "");
    put(hzEl, hz !== null ? `${hz.toFixed(1)} Hz` : "");
    const hint = hz !== null ? tuneHint(cents) : { text: listening ? "Pluck a string" : secure ? "Tap Start and pluck a string" : "", dir: "" };
    put(hintEl, hint.text + (hint.dir === "up" ? " ▲" : hint.dir === "down" ? " ▼" : hint.text === "In tune" ? " ✓" : ""));
    hintEl.className = "tn-hint " + (hz !== null ? lvl : "dim");
    put(targetEl, `String ${stringNumber(target)} · ${noteLabel(tMidi)} · ${tHz.toFixed(2)} Hz` + (pick === null ? " · auto" : ""));
    display.classList.toggle("live", live);

    for (const b of strBox.children) {
      const i = Number(b.dataset.i);
      b.classList.toggle("target", i === target && (listening || pick !== null));
      b.classList.toggle("picked", i === pick);
      b.classList.toggle("ok", tuned.has(i));
    }
    autoBtn.classList.toggle("on", pick === null);
    autoBtn.setAttribute("aria-pressed", String(pick === null));

    const micSig = `${listening}|${starting}|${secure}`;
    if (micBtn.dataset.sig !== micSig) {
      micBtn.dataset.sig = micSig;
      clear(micBtn).append(starting ? h("span.spin") : icon(listening ? "stop" : "mic"), h("span", null, listening ? "Stop" : "Start"));
      micBtn.disabled = !secure || starting;
      micBtn.title = secure ? (listening ? "Stop listening (the microphone is released)" : "Listen to the guitar") : "Needs a secure (https) page";
    }
    meter.firstChild.style.width = `${Math.round(level * 100)}%`;
    meter.hidden = !listening;
  }

  // a hidden page lets the microphone go (the browser's red dot goes away); Start again when back
  const onVis = () => { if (document.hidden && listening) stopMic(); };
  document.addEventListener("visibilitychange", onVis);

  buildStrings();
  render();

  return () => {
    stopped = true;
    document.removeEventListener("visibilitychange", onVis);
    stopMic();
    if (ac) ac.close().catch(() => {});
  };
}
