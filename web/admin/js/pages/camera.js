// Camera: Front | Rear (the rear is an IP camera over RTSP, docs/contracts/camera.md «Задняя камера») — live video,
// status, settings (camera.* / rear_camera.*), a snapshot of the rear and what the car screen shows (api/camera/view).
// The transport is chosen automatically: on the device's network WebRTC (WHEP, lowest latency), falling back
// to the on-demand MPEG-TS stream if WebRTC cannot connect; through the server (docs/contracts/remote.md)
// WebRTC does not pass the tunnel, so the stream right away. Nothing is streamed unless this page is open
// and the tab is visible; only the camera whose tab is shown.
import { h, icon, fmt, fmtBytes, isNum } from "../dom.js";
import { api, appUrl, errText, goLogin, REMOTE } from "../api.js";
import { loadMpegts } from "../vendor.js";
import { SettingsForm, section } from "../form.js";
import { store, on } from "../store.js";
import { banner, confirmDialog, segmented, switchEl, toast } from "../ui.js";
import {
  camFromHash, camDot, rearStatus, screenInfo, rtspProblem, passwordInfo, takePassword, rearConfirm, snapName,
  protectedInfo, storageWarning,
} from "../camera-fmt.js";

const CAM_STATE = { starting: "starting", recording: "recording", stopped: "stopped", error: "error" };
// applied by fnav-camera on the fly: no "recording restart" confirmation for these
const LIVE_KEYS = ["live_bitrate_kbps", "max_exposure_ms_driving", "max_exposure_ms_parked"];
const frameMs = () => Math.floor(1000 / (store.settings?.camera?.fps || 15));

/** "125 ms · gain 16× · 3.4 lux · limit 125 ms (parked)" */
function exposureText(c) {
  if (!isNum(c.exposure_ms)) return "—";
  const parts = [`${fmt(c.exposure_ms, 0)} ms`];
  if (isNum(c.gain)) parts.push(`gain ${fmt(c.gain, 1)}×`);
  if (isNum(c.lux)) parts.push(`${fmt(c.lux, 1)} lux`);
  if (isNum(c.max_exposure_ms)) {
    const why = c.moving === true ? "driving" : c.moving === false ? "parked" : "speed unknown";
    parts.push(`limit ${fmt(c.max_exposure_ms, 0)} ms (${why})`);
  }
  return parts.join(" · ");
}

/** Minimal WHEP client (RFC 9725) on RTCPeerConnection. */
export class WhepClient {
  constructor(video, { url = appUrl("api/camera/whep"), onState } = {}) {
    this.video = video;
    this.url = url;
    this.onState = onState || (() => {});
    this.pc = null;
    this.resource = null;
    this.closed = false;
  }

  async start(timeoutMs = 9000) {
    if (typeof RTCPeerConnection === "undefined") throw new Error("WebRTC is not supported by this browser");
    this.onState("connecting");
    const pc = (this.pc = new RTCPeerConnection({ iceServers: [] }));
    const tr = pc.addTransceiver("video", { direction: "recvonly" });
    try {
      const caps = RTCRtpReceiver.getCapabilities("video");
      const h264 = caps.codecs.filter((c) => c.mimeType.toLowerCase() === "video/h264");
      if (h264.length) tr.setCodecPreferences(h264);
    } catch { /* not critical */ }

    const gotVideo = new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error("no video within " + timeoutMs / 1000 + " s")), timeoutMs);
      pc.ontrack = (e) => {
        this.video.srcObject = e.streams[0] || new MediaStream([e.track]);
        this.video.play().catch(() => {});
      };
      this.video.onplaying = () => { clearTimeout(t); resolve(); };
      pc.onconnectionstatechange = () => {
        if (pc.connectionState === "failed") { clearTimeout(t); reject(new Error("WebRTC connection failed")); }
        if (!this.closed && (pc.connectionState === "disconnected" || pc.connectionState === "closed")) this.onState("lost");
      };
    });
    gotVideo.catch(() => {});

    await pc.setLocalDescription(await pc.createOffer());
    await new Promise((res) => {
      if (pc.iceGatheringState === "complete") return res();
      const done = () => { if (pc.iceGatheringState === "complete") { pc.removeEventListener("icegatheringstatechange", done); res(); } };
      pc.addEventListener("icegatheringstatechange", done);
      setTimeout(res, 2500);
    });

    const resp = await fetch(this.url, {
      method: "POST",
      headers: { "Content-Type": "application/sdp" },
      body: pc.localDescription.sdp,
      credentials: "same-origin",
    });
    if (resp.status === 401) { goLogin(); throw new Error("login required"); }
    if (!resp.ok) {
      let msg = "";
      try { msg = (await resp.json()).detail || ""; } catch { /* */ }
      throw new Error(msg || "WHEP: HTTP " + resp.status);
    }
    const loc = resp.headers.get("Location");
    if (loc) this.resource = this._resolve(loc);
    await pc.setRemoteDescription({ type: "answer", sdp: await resp.text() });
    await gotVideo;
    this.onState("playing");
  }

  _resolve(loc) {
    // Location points to the WHEP resource; DELETE/PATCH go through /api/camera/whep/...
    try {
      const u = new URL(loc, location.origin + this.url);
      if (u.origin === location.origin && u.pathname.startsWith(this.url)) return u.pathname;
      return this.url + "/" + u.pathname.split("/").filter(Boolean).pop();
    } catch { return null; }
  }

  async stats(prev) {
    if (!this.pc) return null;
    const rep = await this.pc.getStats();
    let out = null;
    rep.forEach((r) => {
      if (r.type === "inbound-rtp" && r.kind === "video") {
        const now = performance.now();
        out = { fps: r.framesPerSecond, w: r.frameWidth, h: r.frameHeight, lost: r.packetsLost, bytes: r.bytesReceived, t: now, kbps: null };
        if (prev && now > prev.t) out.kbps = ((r.bytesReceived - prev.bytes) * 8) / (now - prev.t);
      }
    });
    return out;
  }

  async stop() {
    this.closed = true;
    const res = this.resource;
    this.resource = null;
    try { this.video.onplaying = null; this.video.srcObject = null; } catch { /* */ }
    if (this.pc) { try { this.pc.close(); } catch { /* */ } this.pc = null; }
    if (res) { try { await fetch(res, { method: "DELETE", credentials: "same-origin" }); } catch { /* */ } }
  }
}

/**
 * Live MPEG-TS over HTTP (api/camera/live.ts: a separate low-bitrate H.264 encode that the camera starts
 * only while someone is watching) played with mpegts.js through MSE, with latency chasing.
 */
export class TsLiveClient {
  constructor(video, { url = appUrl("api/camera/live.ts"), onState, audio = false } = {}) {
    this.video = video;
    this.url = url;
    this.audio = audio; // the stream carries AAC (the rear camera's microphone)
    this.onState = onState || (() => {});
    this.player = null;
    this.closed = false;
    this.stat = null; // last mpegts STATISTICS_INFO
    this.watch = null;
  }

  async start(timeoutMs = 15000) {
    const mpegts = await loadMpegts();
    if (this.closed) return;
    if (!mpegts.isSupported()) throw new Error("this browser has no MSE support");
    this.onState("connecting");
    const p = (this.player = mpegts.createPlayer(
      { type: "mpegts", isLive: true, url: this.url, hasAudio: this.audio, hasVideo: true },
      {
        enableWorker: false,
        enableStashBuffer: false, // live: hand data to MSE immediately
        lazyLoad: false,
        autoCleanupSourceBuffer: true,
        autoCleanupMaxBackwardDuration: 20,
        autoCleanupMinBackwardDuration: 10,
        liveBufferLatencyChasing: true, // jump forward if the buffer grows beyond 3 s (bad link)
        liveBufferLatencyMaxLatency: 3.0,
        liveBufferLatencyMinRemain: 0.5,
        liveSync: true, // and play slightly faster to stay ~0.8 s behind live
        liveSyncMaxLatency: 1.5,
        liveSyncTargetLatency: 0.8,
        liveSyncPlaybackRate: 1.15,
      },
    ));
    const E = mpegts.Events;
    let playing = false;
    await new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error("no video within " + timeoutMs / 1000 + " s")), timeoutMs);
      const fail = (why) => {
        if (this.closed) return;
        if (!playing) { clearTimeout(t); reject(new Error(why)); } else this.onState("lost", why);
      };
      p.on(E.ERROR, (type, detail, info) => {
        const code = info && info.code;
        if (code === 401) { goLogin(); fail("login required"); return; }
        fail(code > 0 ? `live stream unavailable (HTTP ${code})` : `${type}: ${detail}`);
      });
      p.on(E.LOADING_COMPLETE, () => fail("the stream ended"));
      p.on(E.STATISTICS_INFO, (st) => { this.stat = { ...st, t: performance.now() }; });
      this.video.onplaying = () => {
        if (playing) return;
        playing = true;
        clearTimeout(t);
        this.onState("playing");
        resolve();
      };
      p.attachMediaElement(this.video);
      p.load();
      const pr = p.play();
      if (pr && pr.catch) pr.catch(() => {});
    });
    // stall watchdog: the picture froze although nothing reported an error (link silently dropped)
    let lastT = -1, still = 0;
    this.watch = setInterval(() => {
      const ct = this.video.currentTime;
      still = ct === lastT ? still + 1 : 0;
      lastT = ct;
      if (still >= 4) { still = 0; this.onState("lost", "the picture froze"); }
    }, 2000);
  }

  /** {w, h, fps, kbps, dropped} from the mpegts statistics (fps from the delta to the previous call). */
  stats(prev) {
    const s = this.stat;
    if (!s) return null;
    const out = { w: this.video.videoWidth, h: this.video.videoHeight, frames: s.decodedFrames || 0, dropped: s.droppedFrames || 0, t: s.t, fps: null, kbps: isNum(s.speed) ? s.speed * 8 : null };
    if (prev && s.t > prev.t) out.fps = ((out.frames - prev.frames) * 1000) / (s.t - prev.t);
    return out;
  }

  stop() {
    this.closed = true;
    clearInterval(this.watch);
    this.video.onplaying = null;
    const p = this.player;
    this.player = null;
    if (p) { try { p.pause(); p.unload(); p.detachMediaElement(); p.destroy(); } catch { /* */ } }
    try { this.video.removeAttribute("src"); this.video.load(); } catch { /* */ }
  }
}

/**
 * Live video of one camera: the player box and a stats line under it. start() picks the transport (see the top),
 * stop() ends all streaming; fitAspect — the box takes the video's proportions (an IP camera is usually 16:9).
 */
function livePlayer({ whepUrl, tsUrl, fitAspect = false, onSound = null }) {
  const video = h("video", { autoplay: true, muted: true, playsinline: true, hidden: true });
  video.muted = true;
  const msg = h(".overlay-msg", null, "Connecting…");
  const box = h(".video-box" + (fitAspect ? ".cam-wide" : ""), null, video, msg);
  const statsEl = h(".mono.dim", { style: { fontSize: "12.5px", marginTop: "8px", minHeight: "20px" } });
  if (fitAspect) {
    const fit = () => { if (video.videoWidth && video.videoHeight) box.style.aspectRatio = `${video.videoWidth} / ${video.videoHeight}`; };
    video.addEventListener("loadedmetadata", fit);
    video.addEventListener("resize", fit);
  }

  let mode = null; // "webrtc" | "stream" — what is playing (or connecting) now
  let soundOn = false; // the rear camera's microphone: only the stream carries it (AAC), WebRTC here has none
  let client = null;
  let gen = 0;
  let retryTimer = null;
  let statsTimer = null;

  function stopAll() {
    gen++;
    clearTimeout(retryTimer);
    clearInterval(statsTimer);
    if (client) { client.stop(); client = null; }
    video.hidden = true;
  }

  function showMsg(t) { msg.textContent = t; msg.hidden = !t; }

  /** Automatic choice: WebRTC on the device's network (stream if it fails), the stream through the server. */
  function start() {
    if (REMOTE || soundOn) startStream();
    else startWebrtc();
  }

  /** Sound on / off. Called from the click itself: a user gesture is what lets the page play sound. */
  function setSound(on) {
    soundOn = !!on;
    video.muted = !soundOn;
    if (soundOn && !(mode === "stream" && client && client.audio)) startStream();
    if (onSound) onSound(soundOn);
  }

  /** Stop everything; sound goes off with it (a later start without a click could not play sound anyway). */
  function stop() {
    if (soundOn) { soundOn = false; video.muted = true; if (onSound) onSound(false); }
    stopAll();
  }

  async function startWebrtc() {
    stopAll();
    const my = gen;
    mode = "webrtc";
    showMsg("Connecting WebRTC…");
    statsEl.textContent = "";
    const c = (client = new WhepClient(video, {
      url: whepUrl,
      onState: (s) => {
        if (my !== gen) return;
        if (s === "playing") { video.hidden = false; showMsg(""); }
        if (s === "lost") { showMsg("Connection lost, reconnecting…"); retryTimer = setTimeout(() => { if (my === gen && mode === "webrtc") startWebrtc(); }, 1500); }
      },
    }));
    try {
      await c.start();
      if (my !== gen) return;
      let prev = null;
      statsTimer = setInterval(async () => {
        const s = await c.stats(prev).catch(() => null);
        if (!s) return;
        prev = s;
        statsEl.textContent = `${s.w || video.videoWidth || "?"}×${s.h || video.videoHeight || "?"} · ${fmt(s.fps, 0)} fps · ${s.kbps ? Math.round(s.kbps) + " kbit/s" : "…"} · packets lost: ${s.lost ?? 0} · WebRTC`;
      }, 1000);
    } catch (e) {
      if (my !== gen) return;
      console.warn("WHEP:", e);
      startStream();
      statsEl.textContent = "WebRTC unavailable (" + (e.message || e) + ") — using the live stream";
    }
  }

  async function startStream(attempt = 0) {
    stopAll();
    const my = gen;
    mode = "stream";
    showMsg(attempt ? "Reconnecting to the live stream…" : "Starting the live stream…");
    if (!attempt) statsEl.textContent = "";
    const retry = (why) => {
      if (my !== gen) return;
      video.hidden = true;
      const d = Math.min(10, 2 + attempt * 2);
      showMsg(`${why}. Retrying in ${d} s…`);
      retryTimer = setTimeout(() => { if (my === gen && mode === "stream") startStream(attempt + 1); }, d * 1000);
    };
    const c = (client = new TsLiveClient(video, {
      url: tsUrl,
      audio: soundOn,
      onState: (s, why) => {
        if (my !== gen) return;
        if (s === "playing") { video.hidden = false; showMsg(""); }
        if (s === "lost") retry("Stream interrupted: " + (why || "connection lost"));
      },
    }));
    try {
      await c.start();
      if (my !== gen) return;
      attempt = 0;
      let prev = null;
      statsTimer = setInterval(() => {
        const s = c.stats(prev);
        if (!s) return;
        prev = s;
        statsEl.textContent = `${s.w || "?"}×${s.h || "?"} · ${fmt(s.fps, 0)} fps · ${s.kbps ? Math.round(s.kbps) + " kbit/s" : "…"} · dropped: ${s.dropped} · MPEG-TS on demand${c.audio ? " · with sound" : ""}`;
      }, 1000);
    } catch (e) {
      if (my !== gen) return;
      console.warn("live.ts:", e);
      const why = String(e.message || e);
      retry(why.charAt(0).toUpperCase() + why.slice(1));
    }
  }

  return { box, statsEl, start, stop, showMsg, setSound, sound: () => soundOn };
}

const kindCls = (k) => (k === "bad" ? "red" : k === "warn" ? "amber" : k === "ok" ? "green" : "");
let lastTab = "front"; // the camera shown last: the page opens on it again (the module stays loaded)

export function mount(root) {
  root.classList.add("cam-page");
  let stopped = false;
  const front = livePlayer({ whepUrl: appUrl("api/camera/whep"), tsUrl: appUrl("api/camera/live.ts") });

  // ── front camera status ──
  const stRefs = {};
  const kvRow = (k, label) => { const v = h("span.v", null, "—"); stRefs[k] = v; return [h("span.k", null, label), v]; };
  const statusCard = h(".card", null, h("h2", null, "Status"),
    h(".kv", null, kvRow("state", "State"), kvRow("fps", "Frames/s"), kvRow("br", "Bitrate"), kvRow("mode", "Sensor → output"), kvRow("seg", "Current segment"), kvRow("drop", "Dropped frames"), kvRow("exp", "Exposure"), kvRow("rc", "Recordings"), kvRow("prot", "Protected"), kvRow("free", "Free space"), kvRow("sync", "Time synchronized"), kvRow("err", "Error")));
  const setS = (k, t, cls) => { stRefs[k].textContent = t; stRefs[k].className = "v" + (cls ? " " + cls : ""); };
  function renderStatus() {
    const c = store.telemetry?.camera;
    if (!c) return;
    setS("state", CAM_STATE[c.state] || c.state, c.state === "error" ? "red" : c.state === "recording" ? "green" : "amber");
    setS("fps", fmt(c.fps_actual, 1));
    setS("br", isNum(c.bitrate_kbps) ? Math.round(c.bitrate_kbps) + " kbit/s" : "—");
    setS("mode", `${c.sensor_mode || "—"} → ${c.output_size || "—"}`);
    setS("seg", c.current_segment ? c.current_segment.split("/").pop() : "—");
    setS("drop", String(c.frames_dropped ?? 0), c.frames_dropped > 0 ? "amber" : "");
    setS("exp", exposureText(c));
    setS("rc", `${c.recordings_count ?? "—"} · ${isNum(c.recordings_gb) ? c.recordings_gb.toFixed(2) + " GB" : "—"}`);
    const pi = protectedInfo(c);
    setS("prot", pi.text, kindCls(pi.kind));
    setS("free", isNum(c.free_gb) ? c.free_gb.toFixed(1) + " GB" : "—");
    setS("sync", c.time_synced ? "yes" : "no", c.time_synced ? "" : "amber");
    setS("err", c.error || "—", c.error ? "red" : "");
  }

  // ── protected recordings squeeze the unprotected ones (camera.md «Минимум петли»): above both tabs ──
  const warnBox = h("div", { hidden: true });
  let warnText = null;
  function renderWarn() {
    const t = store.telemetry;
    const w = t ? storageWarning(t.camera, t.camera_rear, store.settings?.storage?.max_recordings_gb) : null;
    if (w === warnText) return;
    warnText = w;
    warnBox.hidden = !w;
    warnBox.replaceChildren(...(w ? [banner("warn", w)] : []));
  }

  // ── settings ──
  const form = new SettingsForm({
    beforeSave: (patch) =>
      patch.camera && Object.keys(patch.camera).some((k) => !LIVE_KEYS.includes(k))
        ? confirmDialog({
            title: "Recording restart",
            text: "Applying camera settings restarts the video pipeline: the current segment is closed cleanly and recording resumes after a few seconds. Continue?",
            okText: "Apply and restart",
          })
        : true,
  });
  const f = (spec) => form.add(spec);
  const frontScrSlot = h("div.cam-scrslot"); // "Car screen shows" sits here on the Front tab (setTab moves it)
  const settingsCard = section("Camera settings",
    frontScrSlot,
    banner("warn", "Applying these settings restarts recording: the current segment is closed and there will be a gap of a few seconds in the footage."),
    h("div", { style: { height: "6px" } }),
    f({ path: "camera.bitrate_kbps", label: "Bitrate", hint: "Quality and file size", type: "number", min: 500, max: 25000, step: 100, unit: "kbit/s", help: "Rule of thumb: 6000 kbit/s ≈ 2.7 GB per hour." }),
    store.settings?.camera?.live_bitrate_kbps !== undefined
      ? f({ path: "camera.live_bitrate_kbps", label: "Live stream bitrate", hint: "The on-demand \"Stream\" for remote viewing; recordings are not affected", type: "number", min: 200, max: 5000, step: 50, unit: "kbit/s", help: "800 kbit/s ≈ 360 MB per hour of watching through the phone's mobile data." })
      : null,
    f({ path: "camera.segment_minutes", label: "Segment length", hint: "Recording is split into files of this length", type: "number", min: 1, max: 60, step: 1, unit: "min" }),
    f({ path: "storage.max_recordings_gb", label: "Recordings limit", hint: "All recordings together; the oldest are deleted first, protected ones are kept", type: "number", min: 1, max: 500, step: 1, unit: "GB", help: "At 6000 kbit/s, 10 GB ≈ 3.5 hours of driving." }),
    f({ path: "camera.hflip", label: "Mirror horizontally", type: "switch" }),
    f({ path: "camera.vflip", label: "Flip vertically", type: "switch" }),
    f({ path: "camera.exposure_mode", label: "Exposure", type: "select", options: [
      { value: "normal", label: "Normal" },
      { value: "short", label: "Short (less motion blur)" },
      { value: "long", label: "Long (less noise, more blur)" },
    ] }),
    f({ path: "camera.ev", label: "EV compensation", hint: "Brighter / darker", type: "range", min: -4, max: 4, step: 0.1 }),
    store.settings?.camera?.max_exposure_ms_parked !== undefined
      ? [
          f({ path: "camera.max_exposure_ms_driving", label: "Night exposure, driving", hint: "Longest exposure in the dark while moving", type: "number", min: 10, max: 1000, step: 1, unit: "ms", help: `Up to ${frameMs()} ms keeps the full frame rate; longer is brighter, but blurs and drops frames. Applied without restarting the recording.` }),
          f({ path: "camera.max_exposure_ms_parked", label: "Night exposure, parked", hint: "Longest exposure in the dark when the van stands still for 10 s", type: "number", min: 10, max: 1000, step: 1, unit: "ms", help: "125 ms ≈ 8 frames/s at night: twice as bright as 66 ms. Daytime footage is not affected: the camera goes this far only when it is dark." }),
        ]
      : null,
    f({ path: "camera.overlay_timestamp", label: "Timestamp overlay", hint: "Burned into the recording", type: "switch" }),
    form.bar,
  );

  // impact while driving (settings.impact, docs/contracts/camera.md «Удар на ходу»): its own form — saving it does not
  // touch the recording; an older device without the section — no card
  const iform = new SettingsForm();
  const impactCard = store.settings?.impact !== undefined ? section("Impact while driving",
    h("p.dim", { style: { margin: "0 0 8px", fontSize: "13px" } }, "A hit while the van is moving (or stopped less than a minute ago) protects the current recordings of both cameras — the neighbouring segment too near a segment boundary — beeps and marks the moment on the recording timeline. Parked, the alarm watches for impacts instead."),
    iform.add({ path: "impact.enabled", label: "Protect recordings on impact", type: "switch" }),
    iform.add({ path: "impact.threshold_g", label: "Threshold", hint: "Strength of a hit after filtering out braking, turns and body roll", type: "number", min: 0.5, max: 3, step: 0.1, unit: "g", help: "Potholes and speed bumps give 0.3–0.8 g; 1.5 g leaves a safe margin. The sensor measures up to 2 g per axis: a hit that reaches it counts as at least 2 g." }),
    iform.add({ path: "impact.cooldown_s", label: "Pause after an impact", hint: "The next impact is not watched before", type: "number", min: 5, max: 600, step: 5, unit: "s" }),
    iform.bar,
  ) : null;

  const frontPane = h(".cam-pane", null,
    h(".cam-layout", null,
      h("div", null,
        h(".card", null,
          h(".card-head", null, h("h2", null, "Live video"), h("button.btn.sm", { type: "button", title: "Reconnect", onclick: () => reload("front") }, icon("refresh"), "Reload")),
          front.box, front.statsEl,
        ),
      ),
      h("div", { style: { display: "flex", flexDirection: "column", gap: "14px" } }, statusCard),
    ),
    settingsCard,
    impactCard,
  );

  // ═════ rear camera: an IP camera over RTSP (settings.rear_camera; telemetry camera_rear, null while it is off) ═════
  const rearCfg = () => (store.settings && store.settings.rear_camera) || null;
  const rearOn = () => !!(rearCfg() && rearCfg().enabled);
  const hasRear = !!rearCfg(); // older device software: no rear camera, the page is the front camera only
  // Sound: the rear camera's microphone (shown when the camera reports one)
  const soundBtn = h("button.btn.sm.cam-sound", { type: "button", hidden: true, title: "Listen to the rear camera's microphone" },
    icon("vol"), h("span", null, "Sound"));
  const rear = livePlayer({ whepUrl: appUrl("api/camera/rear/whep"), tsUrl: appUrl("api/camera/rear/live.ts"), fitAspect: true,
    onSound: (on) => { soundBtn.classList.toggle("on", on); soundBtn.lastChild.textContent = on ? "Sound on" : "Sound"; } });
  soundBtn.onclick = () => rear.setSound(!rear.sound());

  // ── rear status ──
  const rRefs = {};
  const rRow = (k, label) => { const v = h("span.v", null, "—"); rRefs[k] = v; return [h("span.k", null, label), v]; };
  const setR = (k, t, kind) => { rRefs[k].textContent = t; rRefs[k].className = "v" + (kindCls(kind) ? " " + kindCls(kind) : ""); };
  const restartBtn = h("button.btn.sm", { type: "button", title: "Drop the connection to the camera and connect again", onclick: () => restartRear() }, icon("refresh"), "Reconnect");
  const rStatusCard = h(".card", null, h(".card-head", null, h("h2", null, "Status"), restartBtn),
    h(".kv", null, rRow("state", "State"), rRow("err", "Error"), rRow("codec", "Codec"), rRow("size", "Size"), rRow("sfps", "Camera frames/s"),
      rRow("fps", "Received frames/s"), rRow("br", "Bitrate"), rRow("drop", "Dropped frames"), rRow("seg", "Current segment")));
  function renderRearStatus() {
    const c = store.telemetry ? store.telemetry.camera_rear : null;
    const s = rearStatus(c || (rearOn() ? null : { state: "disabled" }));
    setR("state", s.state.text, s.state.kind);
    setR("err", s.error.text, s.error.kind);
    setR("codec", s.codec.text, s.codec.kind);
    setR("size", s.size);
    setR("sfps", s.streamFps);
    setR("fps", s.fps);
    setR("br", s.bitrate);
    setR("drop", s.dropped.text, s.dropped.kind);
    setR("seg", s.segment);
    soundBtn.hidden = !(c && c.audio);
    // the first snapshot by itself, once the camera delivers pictures
    if (tab === "rear" && !snapTaken && !document.hidden && s.state.kind === "ok") takeSnapshot();
    // password_set as the camera reports it (not right after a save: the status lags a moment behind)
    if (c && typeof c.password_set === "boolean" && c.password_set !== pwSet && Date.now() > pwHoldUntil) {
      pwSet = c.password_set;
      pwRefresh();
    }
  }
  async function restartRear() {
    restartBtn.disabled = true;
    try {
      await api.post("/api/camera/rear/restart");
      toast("ok", "Rear camera: connecting again");
    } catch (e) {
      toast("error", "Rear camera: " + errText(e));
    } finally {
      restartBtn.disabled = false;
    }
  }

  // ── rear snapshot: fetched once as a file, so Save keeps exactly the frame shown ──
  const snapImg = h("img", { alt: "Rear camera snapshot", hidden: true });
  const snapMsg = h(".overlay-msg", null, "No snapshot yet");
  const snapBox = h(".video-box.cam-wide.cam-snapbox", null, snapImg, snapMsg);
  const snapInfo = h(".mono.dim.cam-snapinfo");
  const snapBtn = h("button.btn.sm", { type: "button", title: "Take a new snapshot", onclick: () => takeSnapshot() }, icon("refresh"), "Take");
  const snapDl = h("a.btn.sm", { hidden: true, title: "Save the snapshot to this device" }, icon("dl"), "Save");
  const snapCard = h(".card", null, h(".card-head", null, h("h2", null, "Snapshot"), snapBtn, snapDl), snapBox, snapInfo);
  let snapUrl = null;
  let snapBusy = false;
  let snapTaken = false;
  async function takeSnapshot() {
    if (snapBusy || !rearOn()) return;
    snapBusy = true;
    snapTaken = true;
    snapBtn.disabled = true;
    if (snapImg.hidden) { snapMsg.hidden = false; snapMsg.textContent = "Taking a snapshot…"; }
    snapInfo.className = "mono dim cam-snapinfo";
    try {
      const resp = await fetch(appUrl("api/camera/rear/snapshot.jpg") + "?t=" + Date.now(), { credentials: "same-origin", cache: "no-store" });
      if (resp.status === 401) { goLogin(); return; }
      if (!resp.ok) {
        let m = "";
        try { m = (await resp.json()).detail || ""; } catch { /* */ }
        throw new Error(m || "HTTP " + resp.status);
      }
      const blob = await resp.blob();
      if (stopped) return;
      const at = new Date();
      if (snapUrl) URL.revokeObjectURL(snapUrl);
      snapUrl = URL.createObjectURL(blob);
      snapImg.onload = () => {
        const w = snapImg.naturalWidth, hh = snapImg.naturalHeight;
        if (w && hh) snapBox.style.aspectRatio = `${w} / ${hh}`;
        snapInfo.textContent = `${at.toLocaleTimeString("en-GB")} · ${w}×${hh} · ${fmtBytes(blob.size)}`;
      };
      snapImg.src = snapUrl;
      snapImg.hidden = false;
      snapMsg.hidden = true;
      snapDl.href = snapUrl;
      snapDl.download = snapName("rear", at);
      snapDl.hidden = false;
    } catch (e) {
      if (stopped) return;
      const why = "No snapshot: " + (e.message || e);
      if (snapImg.hidden) snapMsg.textContent = why;
      else { snapInfo.textContent = why; snapInfo.className = "mono red cam-snapinfo"; }
    } finally {
      snapBusy = false;
      snapBtn.disabled = false;
    }
  }

  // ── rear settings: one Save for everything; the password is not a setting (api/camera/rear/credentials) ──
  let pwSet = null; // the camera has a password in secrets.env: true / false / null — not known (camera off)
  let pwHoldUntil = 0;
  let pwRefresh = () => {};
  const rform = new SettingsForm({
    beforeSave: async (patch) => {
      const pw = takePassword(patch);
      const ask = rearConfirm(patch.rear_camera, rearCfg(), pw !== undefined);
      if (ask && !(await confirmDialog(ask))) return false;
      if (pw !== undefined) {
        // the camera reconnects before this answers: the bar shows it is busy (no second Save meanwhile)
        rform.btnSave.disabled = rform.btnRevert.disabled = true;
        rform.btnSave.textContent = "Saving…";
        try {
          const r = await api.post("/api/camera/rear/credentials", { password: pw });
          pwSet = !!(r && r.password_set);
          pwHoldUntil = Date.now() + 8000;
        } catch (e) {
          toast("error", "Password not saved: " + errText(e));
          rform.refreshBar();
          return false;
        }
      }
      return true;
    },
    onSaved: () => pwRefresh(),
  });
  const rf = (spec) => rform.add(spec);
  /** Write-only password: typing sets a new one, Remove deletes it, the status says whether one is set. */
  const passwordWidget = (fd) => {
    const inp = h("input", { type: "password", autocomplete: "new-password", spellcheck: "false", placeholder: "unchanged", "aria-label": "Camera password" });
    const eye = h("button.btn.sm", { type: "button", onclick: () => { inp.type = inp.type === "password" ? "text" : "password"; eye.textContent = inp.type === "password" ? "Show" : "Hide"; } }, "Show");
    const rm = h("button.btn.sm.danger", { type: "button", title: "Delete the saved password", onclick: () => { inp.value = ""; fd.change(""); refresh(); } }, "Remove");
    const st = h("span.cam-pwst");
    const refresh = () => {
      const v = fd.value === undefined ? null : fd.value;
      const pi = passwordInfo(pwSet, v, rearOn());
      st.textContent = pi.text;
      st.className = "cam-pwst " + kindCls(pi.kind);
      rm.hidden = !(pwSet === true && v === null);
    };
    pwRefresh = refresh;
    inp.addEventListener("input", () => { fd.change(inp.value === "" ? null : inp.value); refresh(); });
    return { el: h(".cam-pw", null, h("span.cam-pwrow", null, inp, eye, rm), st), update: (v) => { inp.value = v || ""; refresh(); } };
  };
  const enRow = rf({ path: "rear_camera.enabled", label: "Rear camera", type: "switch", switchLabel: "record it and show it on the car screen" });
  const urlRow = rf({ path: "rear_camera.url", label: "RTSP address", hint: "the camera's main stream, without the login", type: "text", placeholder: "rtsp://192.168.1.60:554/stream1",
    validate: (v) => rtspProblem(v, { required: !!rform.get("rear_camera.enabled") }),
    help: "From the camera's manual or app. The camera joins the van's Wi-Fi: give it a fixed IP address there, so the address does not change." });
  const rearScrSlot = h("div.cam-scrslot"); // …and here on the Rear tab
  const rearCard = section("Rear camera settings",
    rearScrSlot,
    enRow,
    urlRow,
    rf({ path: "rear_camera.lores_url", label: "Second stream", hint: "optional: the camera's smaller sub-stream", type: "text", placeholder: "rtsp://192.168.1.60:554/stream2",
      validate: (v) => rtspProblem(v),
      help: "Used for the car screen, snapshots and the camera watch, so the device decodes less. Empty: the main stream is used." }),
    rf({ path: "rear_camera.username", label: "Username", hint: "the camera's login", type: "text" }),
    rf({ path: "rear_camera.password", label: "Password", hint: "write-only: kept on the device, never shown", type: "custom", build: passwordWidget }),
    rf({ path: "rear_camera.transport", label: "Transport", type: "seg", options: [{ value: "tcp", label: "TCP" }, { value: "udp", label: "UDP" }],
      help: "TCP is steadier over Wi-Fi; UDP only if the camera cannot do TCP." }),
    rf({ path: "rear_camera.record", label: "Record", type: "switch", switchLabel: "write it to the SD card next to the front camera",
      help: "Off: live view only. Both cameras share the recordings limit (Front → Camera settings); the oldest recordings go first." }),
    rf({ path: "rear_camera.mirror", label: "Mirror on the car screen", type: "switch", switchLabel: "like a rear-view mirror",
      help: "Only the car screen: recordings and this page show the picture as the camera sees it. Applied at once." }),
    store.settings?.rear_camera?.audio !== undefined
      ? rf({ path: "rear_camera.audio", label: "Microphone", type: "switch", switchLabel: "sound in the live video here and loud sounds while armed",
        help: "Loud sounds while the alarm is armed also get a mark with a short clip (Alarm → Loud sounds)." })
      : null,
    store.settings?.rear_camera?.record_audio !== undefined
      ? rf({ path: "rear_camera.record_audio", label: "Record the sound", type: "switch", switchLabel: "the microphone in the rear recordings, all the time",
        help: "AAC 32 kbit/s: about 14 MB an hour on top of the video. Off: recordings stay silent, loud sounds still get their clips." })
      : null,
    h("details.al-adv.cam-adv", null, h("summary", null, "Estimates"),
      h(".hint", null, "Used only until the camera reports its own values."),
      rf({ path: "rear_camera.fps", label: "Expected frame rate", type: "number", min: 1, max: 60, step: 1, unit: "fps" }),
      rf({ path: "rear_camera.bitrate_kbps", label: "Expected bitrate", hint: "keeps disk space free for the next segment", type: "number", min: 200, max: 25000, step: 100, unit: "kbit/s" })),
    rform.bar,
  );

  // ── rear off: what it is and the switch (it saves the address typed below together with it) ──
  const offSwitch = switchEl({ checked: false, label: "Turn on", onchange: (v) => enableRear(v) });
  const offCard = h(".card.cam-off", null,
    h("span.cam-offic", null, icon("cam")),
    h(".cam-offtxt", null,
      h(".cam-offword", null, "Rear camera off"),
      h("p", null, "An IP camera on the van's Wi-Fi (RTSP, H.264): recorded next to the front camera and shown on the car screen with key 4. Enter its address and login below, then turn it on."),
      offSwitch));
  async function enableRear(v) {
    if (!v) return;
    const prob = rtspProblem(rform.get("rear_camera.url"), { required: true });
    if (prob) {
      offSwitch.input.checked = false;
      toast("warn", prob === "Enter the camera's RTSP address" ? "Enter the camera's RTSP address below first" : prob);
      const inp = urlRow.querySelector("input");
      if (inp) { inp.scrollIntoView({ block: "center", behavior: "smooth" }); inp.focus({ preventScroll: true }); }
      return;
    }
    rform.set("rear_camera.enabled", true);
    await rform.save();
    if (stopped) return;
    if (!rearOn()) { // not saved: back to off, the rest stays as typed
      rform.set("rear_camera.enabled", false);
      offSwitch.input.checked = false;
    } else toast("ok", "Rear camera on: connecting…");
  }

  const rearOnBox = h(".cam-layout", null,
    h("div", null,
      h(".card", null,
        h(".card-head", null, h("h2", null, "Live video"), soundBtn, h("button.btn.sm", { type: "button", title: "Reconnect", onclick: () => reload("rear") }, icon("refresh"), "Reload")),
        rear.box, rear.statsEl,
      ),
    ),
    h(".cam-side", null, rStatusCard, snapCard),
  );
  const rearPane = h(".cam-pane", null, rearOnBox, offCard, rearCard);

  // ── the car screen: front or rear (POST api/camera/view, the same as key 4), from telemetry display.camera ──
  const scrText = h("span.cam-scrtxt", null, "—");
  let scrBusy = false;
  let scrExpect = null; // {camera, until}: the answer of the last switch, until telemetry shows it
  const scrSeg = segmented({ name: "cam-screen", options: [{ value: "front", label: "Front" }, { value: "rear", label: "Rear" }], value: null, onchange: (v) => setScreen(v) });
  const screenCard = h(".cam-screen", { title: "The same as key 4 on the keypad" },
    h("span.cam-scric", null, icon("screen")),
    h(".cam-scrtxtbox", null, h(".cam-scrlbl", null, "Car screen shows"), scrText),
    scrSeg);
  async function setScreen(v) {
    if (scrBusy) return;
    scrBusy = true;
    screenCard.classList.add("busy");
    try {
      const r = await api.post("/api/camera/view", { camera: v });
      scrExpect = { camera: r && r.camera ? r.camera : v, until: Date.now() + 2500 };
    } catch (e) {
      toast("error", "Car screen: " + errText(e));
    } finally {
      scrBusy = false;
      screenCard.classList.remove("busy");
      renderScreen();
    }
  }
  function renderScreen() {
    screenCard.hidden = !hasRear || !rearOn();
    for (const slot of [frontScrSlot, rearScrSlot]) slot.hidden = screenCard.hidden || !slot.contains(screenCard);
    if (screenCard.hidden) return;
    const d = store.online && store.telemetry ? store.telemetry.display : null;
    let si = screenInfo(d);
    if (scrExpect && d) {
      if (si.camera === scrExpect.camera || Date.now() > scrExpect.until) scrExpect = null;
      else si = screenInfo({ ...d, camera: scrExpect.camera });
    }
    if (scrText.textContent !== si.text) scrText.textContent = si.text;
    if (!scrBusy) scrSeg.set(si.camera);
  }

  // ── Front | Rear ──
  let tab = hasRear ? camFromHash(location.hash) || lastTab : "front";
  const dots = { front: h("i.cam-dot"), rear: h("i.cam-dot") };
  const tabLabel = (cam, text) => h("span.cam-tl", null, dots[cam], text);
  const tabSeg = segmented({ name: "cam-tab", options: [{ value: "front", label: tabLabel("front", "Front") }, { value: "rear", label: tabLabel("rear", "Rear") }],
    value: tab, onchange: (v) => setTab(v) });
  tabSeg.classList.add("cam-tabs");
  function setTab(v) {
    tab = lastTab = v;
    frontPane.hidden = v !== "front";
    rearPane.hidden = v !== "rear";
    (v === "rear" ? rearScrSlot : frontScrSlot).append(screenCard); // the settings of the tab on show
    renderScreen();
    history.replaceState(null, "", "#/camera" + (v === "rear" ? "?cam=rear" : ""));
    syncPlayers();
    renderRearStatus();
  }
  function renderDots() {
    const t = store.online && store.telemetry ? store.telemetry : null;
    const set = (cam, c) => { const k = "cam-dot " + (t ? camDot(c) : "off"); if (dots[cam].className !== k) dots[cam].className = k; };
    set("front", t && t.camera);
    set("rear", t && t.camera_rear ? t.camera_rear : { state: rearOn() ? "starting" : "disabled" });
  }

  function renderRearMode() {
    const on = rearOn();
    rearOnBox.hidden = !on;
    offCard.hidden = on;
    offSwitch.input.checked = false;
    enRow.hidden = !on; // while off, the switch above turns it on
    if (!on) snapTaken = false;
    pwRefresh();
    renderScreen();
    syncPlayers();
  }

  // ── who streams: only the camera on screen, only while this browser tab is visible ──
  const P = { front, rear };
  let running = null; // "front" | "rear" | null
  const wanted = () => (document.hidden ? null : tab === "front" ? "front" : rearOn() ? "rear" : null);
  function syncPlayers() {
    const want = wanted();
    if (want !== running) {
      if (running) P[running].stop();
      running = want;
      if (want) P[want].start();
    }
    if (document.hidden) { front.showMsg("Paused while the tab is hidden"); rear.showMsg("Paused while the tab is hidden"); }
  }
  function reload(cam) {
    if (wanted() !== cam) return;
    running = cam;
    P[cam].start();
  }
  // hidden tab: stop streaming (no traffic through the phone's mobile data), resume when visible again
  const onVis = () => syncPlayers();
  document.addEventListener("visibilitychange", onVis);

  root.append(
    h(".page-head", null, h("h1", null, "Camera"), hasRear ? tabSeg : null),
    warnBox,
    frontPane,
    hasRear ? rearPane : null,
  );
  frontPane.hidden = tab !== "front";
  rearPane.hidden = tab !== "rear";
  (tab === "rear" ? rearScrSlot : frontScrSlot).append(screenCard);

  renderStatus();
  renderRearMode();
  renderRearStatus();
  renderDots();
  renderWarn();
  const offT = on("telemetry", () => { renderStatus(); renderRearStatus(); renderScreen(); renderDots(); renderWarn(); });
  const offS = on("settings", () => { renderRearMode(); renderWarn(); });
  const offC = on("conn", () => { renderScreen(); renderDots(); });

  return () => {
    stopped = true;
    offT();
    offS();
    offC();
    document.removeEventListener("visibilitychange", onVis);
    form.destroy();
    rform.destroy();
    iform.destroy();
    front.stop();
    rear.stop();
    if (snapUrl) URL.revokeObjectURL(snapUrl);
  };
}
