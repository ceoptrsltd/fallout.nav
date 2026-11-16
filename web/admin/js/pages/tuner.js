// Tuner: a six-string guitar (acoustic or electric) tuned by the microphone of this phone / computer. Everything runs
// in the browser (Web Audio): nothing goes to the device. Pitch and the maths — tuner-fmt.js.
//
// The microphone needs a secure page (https: the remote admin or the installed app); on the device's plain http
// address the browser has no microphone — then only the reference tones work.
import { h, icon, clear } from "../dom.js";
import { errText, appUrl } from "../api.js";
import { toast, banner } from "../ui.js";
import {
  TUNINGS, tuningById, noteHz, centsOff, nearestNote, noteLabel, nearestString, stringNumber, tuneLevel, tuneHint,
  fmtCents, needleDeg, decimate2, rms, PitchTracker, NeedleSpring, clampA4, A4_DEFAULT, A4_MIN, A4_MAX,
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

// ───── the trace: the last seconds of the string against the ideal (the middle line), in the string's colour ─────
const STRING_COLORS = ["#ff7a59", "#ffb62e", "#e6dc4a", "#5dff8a", "#4fd8e8", "#b79bff"]; // 6th … 1st
const TRACE_S = 8; // seconds across
const TRACE_CENTS = 40; // top and bottom of the chart
function trace() {
  const cv = h("canvas.tn-trace", { "aria-hidden": "true" });
  const ctx = cv.getContext("2d");
  let pts = []; // {t, c, i, held}
  const push = (t, c, i, held) => {
    pts.push({ t, c: Math.max(-TRACE_CENTS, Math.min(TRACE_CENTS, c)), i, held });
    while (pts.length && t - pts[0].t > TRACE_S * 1000 + 500) pts.shift();
  };
  const rgba = (hex, a) => `rgba(${parseInt(hex.slice(1, 3), 16)}, ${parseInt(hex.slice(3, 5), 16)}, ${parseInt(hex.slice(5, 7), 16)}, ${a})`;
  function draw(now, labels) {
    const cssW = cv.clientWidth, cssH = cv.clientHeight;
    if (!cssW || !cssH) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    if (cv.width !== Math.round(cssW * dpr) || cv.height !== Math.round(cssH * dpr)) {
      cv.width = Math.round(cssW * dpr);
      cv.height = Math.round(cssH * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);
    const L = 30, R = 10, T = 8, B = 8; // the plot inside the canvas
    const W = cssW - L - R, H = cssH - T - B, mid = T + H / 2;
    const y = (c) => mid - (c / TRACE_CENTS) * (H / 2);
    const x = (t) => L + W - ((now - t) / (TRACE_S * 1000)) * W;
    // the zones and the grid
    ctx.fillStyle = "rgba(255, 182, 46, .06)";
    ctx.fillRect(L, y(CLOSE_CENTS), W, y(-CLOSE_CENTS) - y(CLOSE_CENTS));
    ctx.fillStyle = "rgba(93, 255, 138, .16)";
    ctx.fillRect(L, y(IN_TUNE_CENTS), W, y(-IN_TUNE_CENTS) - y(IN_TUNE_CENTS));
    ctx.lineWidth = 1;
    ctx.font = "11px Oswald, 'Arial Narrow', sans-serif";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (const c of [-40, -20, 0, 20, 40]) {
      ctx.strokeStyle = c === 0 ? "rgba(93, 255, 138, .7)" : "rgba(141, 153, 135, .18)";
      ctx.beginPath();
      ctx.moveTo(L, Math.round(y(c)) + 0.5);
      ctx.lineTo(L + W, Math.round(y(c)) + 0.5);
      ctx.stroke();
      ctx.fillStyle = "rgba(141, 153, 135, .8)";
      ctx.fillText(c === 0 ? "0" : (c > 0 ? "+" : "−") + Math.abs(c), L - 6, y(c));
    }
    // the trace: runs of one string; a gap where nothing sounded
    ctx.save();
    ctx.beginPath();
    ctx.rect(L, T, W, H);
    ctx.clip();
    let run = [];
    const flush = () => {
      if (run.length) {
        const col = STRING_COLORS[run[0].i] || "#dbe4d3";
        const a = run[0].held ? 0.4 : 1;
        if (run.length > 1) {
          ctx.beginPath(); // the area between the string and the ideal
          ctx.moveTo(x(run[0].t), mid);
          for (const q of run) ctx.lineTo(x(q.t), y(q.c));
          ctx.lineTo(x(run[run.length - 1].t), mid);
          ctx.closePath();
          ctx.fillStyle = rgba(col, 0.13 * a);
          ctx.fill();
        }
        ctx.beginPath();
        ctx.moveTo(x(run[0].t), y(run[0].c));
        for (const q of run) ctx.lineTo(x(q.t), y(q.c));
        if (run.length === 1) ctx.lineTo(x(run[0].t) + 1, y(run[0].c));
        ctx.strokeStyle = rgba(col, a);
        ctx.lineWidth = 2.5;
        ctx.lineJoin = "round";
        ctx.lineCap = "round";
        ctx.setLineDash(run[0].held ? [5, 5] : []);
        ctx.stroke();
        ctx.setLineDash([]);
      }
      run = [];
    };
    let prev = null;
    for (const q of pts) {
      if (prev && (q.t - prev.t > 200 || q.i !== prev.i || q.held !== prev.held)) {
        const joined = q.t - prev.t <= 200 && q.i === prev.i; // live ↔ held of one string: no gap in the line
        flush();
        if (joined) run.push({ ...prev, held: q.held });
      }
      run.push(q);
      prev = q;
    }
    flush();
    ctx.restore();
    // the head: where the string is now, with its name
    const last = pts[pts.length - 1];
    if (last && now - last.t < 200) {
      const col = STRING_COLORS[last.i] || "#dbe4d3";
      ctx.beginPath();
      ctx.arc(x(last.t), y(last.c), 4.5, 0, 2 * Math.PI);
      ctx.fillStyle = rgba(col, last.held ? 0.5 : 1);
      ctx.shadowColor = col;
      ctx.shadowBlur = last.held ? 0 : 10;
      ctx.fill();
      ctx.shadowBlur = 0;
      ctx.font = "bold 12px Oswald, 'Arial Narrow', sans-serif";
      ctx.textAlign = "right";
      ctx.fillStyle = rgba(col, 0.95);
      const ly = y(last.c) + (last.c > TRACE_CENTS - 12 ? 14 : -12);
      ctx.fillText(labels[last.i] || "", x(last.t) - 9, ly);
    }
  }
  return { el: cv, push, draw, clear: () => { pts = []; } };
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
  let srcNode = null;
  let lastP = { full: null, near: null, flux: null }; // the raw readings of the last tick
  let rec = null; // a diagnostics recording in progress
  let sending = "";

  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(prefs)); } catch { /* private mode */ } };

  // ───── the display ─────
  const g = gauge();
  const tr = trace();
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
    targetEl,
    h(".tn-tracebox", null, h("span.tn-sharp2", null, "♯"), tr.el, h("span.tn-flat2", null, "♭")));

  // strings: as on the headstock, the 6th (lowest) on the left; Auto picks the one that sounds
  const autoBtn = h("button.btn.tn-auto", { type: "button", title: "Tune whichever string sounds", onclick: () => { pick = null; render(); } }, "Auto");
  const strBox = h(".tn-strings");
  function buildStrings() {
    clear(strBox);
    tuning.notes.forEach((m, i) => {
      strBox.append(h("button.tn-str", { type: "button", dataset: { i }, style: "--sc:" + STRING_COLORS[i], title: `String ${stringNumber(i)}: ${noteLabel(m)}`,
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
    tr.clear();
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

  // diagnostics (temporary): the raw microphone and what the tuner showed go to the device, to calibrate by a real guitar
  const recBtn = h("button.btn.tn-rec", { type: "button", onclick: () => (rec ? stopRec() : startRec()) });
  const recInfo = h("span.tn-recinfo.muted");
  const msg = h("div");
  const card = h(".card.tn-card", null,
    h(".tn-bar", null, h("label.tn-tun", null, h("span.tn-lbl", null, "Tuning"), tunSel), a4Box),
    display,
    h(".tn-strrow", null, strBox, autoBtn),
    h(".tn-ctl", null, micBtn, meter, toneBtn),
    h(".tn-ctl.tn-diag", null, recBtn, recInfo));
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
      srcNode = src;
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
    if (rec) stopRec();
    srcNode = null;
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
    const out = tracker.track(buf, sr, now);
    lastP = tracker.last;
    return out;
  }

  // the sound is read on its own clock; the needle is drawn every frame (and by the reading itself when the
  // browser holds the frames back)
  let reading = { hz: null, live: false, held: false };
  function tick() {
    if (!listening) return;
    const now = performance.now();
    reading = analyse(now);
    if (rec) {
      rec.log.push([Math.round(now - rec.t0), +ac.currentTime.toFixed(4), lastP.full ? +lastP.full.hz.toFixed(3) : 0,
        lastP.full ? +lastP.full.clarity.toFixed(3) : 0, +level.toFixed(4), reading.hz ? +reading.hz.toFixed(3) : 0,
        reading.live ? 1 : 0, reading.held ? 1 : 0, target, +spring.x.toFixed(2),
        lastP.near ? +lastP.near.hz.toFixed(3) : 0, lastP.near ? +lastP.near.clarity.toFixed(3) : 0,
        lastP.flux ? +lastP.flux.hz.toFixed(2) : 0, lastP.onset ? 1 : 0]);
    }
    if (lastFrame === null || now - lastFrame > 120) render(now);
  }
  function loop(now = performance.now()) {
    if (!listening) return;
    render(now);
    raf = requestAnimationFrame(loop);
  }

  // ───── diagnostics recording ─────
  async function startRec() {
    if (rec || sending) return;
    if (!listening) await startMic();
    if (!listening || !srcNode) return;
    const proc = ac.createScriptProcessor(4096, 1, 1); // the raw microphone, before the filters
    const mute = ac.createGain();
    mute.gain.value = 0;
    const r = { id: Date.now().toString(36) + Math.random().toString(36).slice(2, 6), sr: ac.sampleRate, chunks: [], n: 0,
      log: [], t0: performance.now(), at0: null, proc, mute, src: srcNode };
    proc.onaudioprocess = (e) => {
      if (rec !== r) return;
      if (r.at0 === null) r.at0 = e.playbackTime;
      const f = e.inputBuffer.getChannelData(0);
      const o = new Int16Array(f.length);
      for (let i = 0; i < f.length; i++) o[i] = Math.max(-32768, Math.min(32767, Math.round(f[i] * 32767)));
      r.chunks.push(o);
      r.n += o.length;
      if (r.n > r.sr * 180) stopRec(); // three minutes at most
    };
    srcNode.connect(proc);
    proc.connect(mute);
    mute.connect(ac.destination);
    rec = r;
    render();
  }

  function stopRec() {
    const r = rec;
    if (!r) return;
    rec = null;
    r.proc.onaudioprocess = null;
    for (const n of [r.proc, r.mute]) { try { n.disconnect(); } catch { /* already */ } }
    try { r.src.disconnect(r.proc); } catch { /* already */ }
    sendRec(r);
  }

  async function post(path, body, type) {
    let last = null;
    for (let i = 0; i < 3; i++) {
      try {
        const resp = await fetch(appUrl(path), { method: "POST", credentials: "same-origin", headers: { "Content-Type": type }, body });
        if (resp.ok) return;
        last = new Error("HTTP " + resp.status);
      } catch (e) { last = e; }
      await new Promise((ok) => setTimeout(ok, 1500));
    }
    throw last;
  }

  async function sendRec(r) {
    const all = new Int16Array(r.n);
    let o = 0;
    for (const c of r.chunks) { all.set(c, o); o += c.length; }
    r.chunks = [];
    const secs = Math.round(r.n / r.sr);
    const STEP = 400000; // samples a request (0.8 MB)
    const fft = raw ? raw.length : null;
    let track = {};
    try { track = stream ? stream.getAudioTracks()[0].getSettings() : {}; } catch { /* no settings */ }
    sending = "Sending 0%";
    if (!stopped) render();
    try {
      for (let i = 0; i < all.length; i += STEP) {
        await post(`/api/tuner/diag/${r.id}/audio`, all.slice(i, Math.min(all.length, i + STEP)).buffer, "application/octet-stream");
        sending = `Sending ${Math.round((100 * Math.min(all.length, i + STEP)) / all.length)}%`;
        if (!stopped) render();
      }
      await post(`/api/tuner/diag/${r.id}/log`, JSON.stringify({ id: r.id, sampleRate: r.sr, samples: r.n, at0: r.at0, ua: navigator.userAgent,
        track, tuning: tuning.id, a4: prefs.a4, pick, analyseMs: ANALYSE_MS, fftSize: fft,
        cols: ["ms", "acTime", "rawHz", "clarity", "level", "hz", "live", "held", "target", "needle", "nearHz", "nearClarity", "fluxHz", "onset"], log: r.log }), "application/json");
      sending = "";
      recInfo.dataset.done = `Sent: ${secs} s (${r.id})`;
      toast("ok", `Recording sent: ${secs} s`);
    } catch (e) {
      sending = "";
      recInfo.dataset.done = "Not sent: " + errText(e);
      toast("error", "Recording not sent: " + errText(e));
    }
    if (!stopped) render();
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
    // what the tracker may take for a string: the tuning (Auto), or the one tapped — wherever it is now
    tracker.strings = pick === null ? tuning.notes.map((m) => noteHz(m, a4)) : [noteHz(tuning.notes[pick], a4)];
    tracker.strict = pick === null;
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
    // (a thin string is live for a third of a second on a poor microphone: its held reading counts too)
    if (hz !== null && lvl === "good") {
      if (inTuneSince === null) inTuneSince = now;
      else if (now - inTuneSince >= TUNED_MS) tuned.add(target);
    } else inTuneSince = null;

    if (hz !== null) tr.push(now, Math.abs(raw) <= NEEDLE_CENTS ? pos : raw, target, reading.held && !reading.live);
    tr.draw(now, tuning.notes.map((m) => noteLabel(m)));
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
    const recSig = `${!!rec}|${sending}|${secure}`;
    if (recBtn.dataset.sig !== recSig) {
      recBtn.dataset.sig = recSig;
      clear(recBtn).append(h("span", null, rec ? "Stop and send" : "Record for diagnostics"));
      recBtn.disabled = !secure || !!sending;
      recBtn.classList.toggle("danger", !!rec);
    }
    put(recInfo, rec ? `Recording ${Math.floor((now - rec.t0) / 1000)} s — play the strings, then tap Stop and send`
      : sending || recInfo.dataset.done || "Records the microphone and sends it to the device");
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
