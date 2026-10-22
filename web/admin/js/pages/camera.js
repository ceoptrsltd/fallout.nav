// Camera: live video (WebRTC WHEP | live MPEG-TS stream | MJPEG), status, camera.* settings.
// Remote access (docs/contracts/remote.md): WebRTC does not pass the tunnel, so the default there is the
// on-demand MPEG-TS stream; nothing is streamed unless this page is open and the tab is visible.
import { h, icon, fmt, isNum } from "../dom.js";
import { appUrl, goLogin, REMOTE } from "../api.js";
import { loadMpegts } from "../vendor.js";
import { SettingsForm, section } from "../form.js";
import { store, on } from "../store.js";
import { banner, confirmDialog, segmented } from "../ui.js";

const BLANK = "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==";
const CAM_STATE = { starting: "starting", recording: "recording", stopped: "stopped", error: "error" };
const MODES = ["webrtc", "stream", "mjpeg"];
const MODE_KEY = "fnav.camera.mode." + (REMOTE ? "remote" : "local");

/** Mode chosen by the user earlier (per local/remote), else: remote → stream, device → webrtc. */
export function initialMode(saved, remote = REMOTE) {
  return MODES.includes(saved) ? saved : remote ? "stream" : "webrtc";
}
function loadMode() { try { return initialMode(localStorage.getItem(MODE_KEY)); } catch { return initialMode(null); } }
function saveMode(m) { try { localStorage.setItem(MODE_KEY, m); } catch { /* */ } }

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
  constructor(video, { url = appUrl("api/camera/live.ts"), onState } = {}) {
    this.video = video;
    this.url = url;
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
      { type: "mpegts", isLive: true, url: this.url, hasAudio: false, hasVideo: true },
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

export function mount(root) {
  const video = h("video", { autoplay: true, muted: true, playsinline: true, hidden: true });
  video.muted = true;
  const img = h("img", { alt: "MJPEG", hidden: true });
  const msg = h(".overlay-msg", null, "Connecting…");
  const tag = h(".tag.badge", null, "—");
  const box = h(".video-box", null, video, img, msg, tag);
  const statsEl = h(".mono.dim", { style: { fontSize: "12.5px", marginTop: "8px", minHeight: "20px" } });

  let mode = loadMode();
  let client = null;
  let gen = 0;
  let retryTimer = null;
  let statsTimer = null;

  function stopAll() {
    gen++;
    clearTimeout(retryTimer);
    clearInterval(statsTimer);
    if (client) { client.stop(); client = null; }
    img.onerror = img.onload = null;
    img.src = BLANK; // aborts the MJPEG stream
    video.hidden = true;
    img.hidden = true;
  }

  function showMsg(t) { msg.textContent = t; msg.hidden = !t; }

  /** Start (or restart) a mode; remember = the user picked it. */
  function start(m, { remember = false } = {}) {
    if (remember) saveMode(m);
    if (m === "webrtc") startWebrtc();
    else if (m === "stream") startStream();
    else startMjpeg("");
  }

  function startMjpeg(reason) {
    stopAll();
    const my = gen;
    mode = "mjpeg";
    seg.set("mjpeg");
    tag.textContent = "MJPEG 768×576";
    showMsg("Connecting to MJPEG…");
    const load = () => {
      if (my !== gen) return;
      img.hidden = false;
      img.onload = () => { showMsg(""); };
      img.onerror = () => { showMsg("No signal from the camera. Retrying…"); retryTimer = setTimeout(load, 3000); };
      // remote: thin the JPEG stream out (~5 fps) — it goes through the phone's mobile uplink
      img.src = appUrl("api/camera/mjpeg") + (REMOTE ? "?fps=5&" : "?") + "t=" + Date.now();
    };
    load();
    statsEl.textContent = reason ? "WebRTC unavailable: " + reason : REMOTE ? "MJPEG reduced to ~5 fps for remote viewing" : "";
  }

  async function startWebrtc() {
    stopAll();
    const my = gen;
    mode = "webrtc";
    seg.set("webrtc");
    tag.textContent = "WebRTC";
    showMsg("Connecting WebRTC…");
    statsEl.textContent = "";
    const c = (client = new WhepClient(video, {
      onState: (s) => {
        if (my !== gen) return;
        if (s === "playing") { video.hidden = false; showMsg(""); tag.textContent = "WebRTC · H.264"; }
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
        statsEl.textContent = `${s.w || video.videoWidth || "?"}×${s.h || video.videoHeight || "?"} · ${fmt(s.fps, 0)} fps · ${s.kbps ? Math.round(s.kbps) + " kbit/s" : "…"} · packets lost: ${s.lost ?? 0}`;
      }, 1000);
    } catch (e) {
      if (my !== gen) return;
      console.warn("WHEP:", e);
      if (!REMOTE) { startMjpeg(e.message || String(e)); return; }
      // through the server WebRTC cannot connect: the TS stream is the better fallback (less data than MJPEG)
      startStream();
      statsEl.textContent = "WebRTC is not available remotely — using the live stream";
    }
  }

  async function startStream(attempt = 0) {
    stopAll();
    const my = gen;
    mode = "stream";
    seg.set("stream");
    tag.textContent = "Stream";
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
      onState: (s, why) => {
        if (my !== gen) return;
        if (s === "playing") { video.hidden = false; showMsg(""); tag.textContent = "Stream · H.264"; }
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
        statsEl.textContent = `${s.w || "?"}×${s.h || "?"} · ${fmt(s.fps, 0)} fps · ${s.kbps ? Math.round(s.kbps) + " kbit/s" : "…"} · dropped: ${s.dropped} · MPEG-TS on demand`;
      }, 1000);
    } catch (e) {
      if (my !== gen) return;
      console.warn("live.ts:", e);
      const why = String(e.message || e);
      retry(why.charAt(0).toUpperCase() + why.slice(1));
    }
  }

  const seg = segmented({
    name: "camsrc",
    value: mode,
    options: [{ value: "webrtc", label: "WebRTC" }, { value: "stream", label: "Stream" }, { value: "mjpeg", label: "MJPEG" }],
    onchange: (v) => start(v, { remember: true }),
  });

  // hidden tab: stop streaming (no traffic through the phone's mobile data), resume when visible again
  const onVis = () => {
    if (document.hidden) { stopAll(); showMsg("Paused while the tab is hidden"); }
    else start(mode);
  };
  document.addEventListener("visibilitychange", onVis);

  // ── camera status ──
  const stRefs = {};
  const kvRow = (k, label) => { const v = h("span.v", null, "—"); stRefs[k] = v; return [h("span.k", null, label), v]; };
  const statusCard = h(".card", null, h("h2", null, "Status"),
    h(".kv", null, kvRow("state", "State"), kvRow("fps", "Frames/s"), kvRow("br", "Bitrate"), kvRow("mode", "Sensor → output"), kvRow("seg", "Current segment"), kvRow("drop", "Dropped frames"), kvRow("rc", "Recordings"), kvRow("free", "Free space"), kvRow("sync", "Time synchronized"), kvRow("err", "Error")));
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
    setS("rc", `${c.recordings_count ?? "—"} · ${isNum(c.recordings_gb) ? c.recordings_gb.toFixed(2) + " GB" : "—"}`);
    setS("free", isNum(c.free_gb) ? c.free_gb.toFixed(1) + " GB" : "—");
    setS("sync", c.time_synced ? "yes" : "no", c.time_synced ? "" : "amber");
    setS("err", c.error || "—", c.error ? "red" : "");
  }

  // ── settings ──
  const form = new SettingsForm({
    beforeSave: (patch) =>
      patch.camera
        ? confirmDialog({
            title: "Recording restart",
            text: "Applying camera settings restarts the video pipeline: the current segment is closed cleanly and recording resumes after a few seconds. Continue?",
            okText: "Apply and restart",
          })
        : true,
  });
  const f = (spec) => form.add(spec);
  const settingsCard = section("Camera settings",
    banner("warn", "Applying these settings restarts recording: the current segment is closed and there will be a gap of a few seconds in the footage."),
    h("div", { style: { height: "6px" } }),
    f({ path: "camera.bitrate_kbps", label: "Bitrate", hint: "Quality and file size", type: "number", min: 500, max: 25000, step: 100, unit: "kbit/s", help: "Rule of thumb: 6000 kbit/s ≈ 2.7 GB per hour." }),
    store.settings?.camera?.live_bitrate_kbps !== undefined
      ? f({ path: "camera.live_bitrate_kbps", label: "Live stream bitrate", hint: "The on-demand \"Stream\" for remote viewing; recordings are not affected", type: "number", min: 200, max: 5000, step: 50, unit: "kbit/s", help: "800 kbit/s ≈ 360 MB per hour of watching through the phone's mobile data." })
      : null,
    f({ path: "camera.segment_minutes", label: "Segment length", hint: "Recording is split into files of this length", type: "number", min: 1, max: 60, step: 1, unit: "min" }),
    f({ path: "camera.hflip", label: "Mirror horizontally", type: "switch" }),
    f({ path: "camera.vflip", label: "Flip vertically", type: "switch" }),
    f({ path: "camera.exposure_mode", label: "Exposure", type: "select", options: [
      { value: "normal", label: "Normal" },
      { value: "short", label: "Short (less motion blur)" },
      { value: "long", label: "Long (night)" },
    ] }),
    f({ path: "camera.ev", label: "EV compensation", hint: "Brighter / darker", type: "range", min: -4, max: 4, step: 0.1 }),
    f({ path: "camera.overlay_timestamp", label: "Timestamp overlay", hint: "Burned into the recording", type: "switch" }),
    form.bar,
  );

  root.append(
    h(".page-head", null, h("h1", null, "Camera")),
    h(".cam-layout", null,
      h("div", null,
        h(".card", null,
          h(".card-head", null, h("h2", null, "Live video"), seg, h("button.btn.sm", { type: "button", title: "Reconnect", onclick: () => start(mode) }, icon("refresh"), "Reload")),
          box, statsEl,
        ),
      ),
      h("div", { style: { display: "flex", flexDirection: "column", gap: "14px" } }, statusCard),
    ),
    settingsCard,
  );

  renderStatus();
  const offT = on("telemetry", renderStatus);
  if (document.hidden) showMsg("Paused while the tab is hidden");
  else start(mode);

  return () => {
    offT();
    document.removeEventListener("visibilitychange", onVis);
    form.destroy();
    stopAll();
  };
}
