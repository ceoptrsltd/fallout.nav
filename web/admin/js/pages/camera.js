// Camera: live video (WebRTC WHEP, MJPEG fallback), status, camera.* settings
import { h, icon, fmt, isNum } from "../dom.js";
import { SettingsForm, section } from "../form.js";
import { store, on } from "../store.js";
import { banner, confirmDialog, segmented } from "../ui.js";

const BLANK = "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==";
const CAM_STATE = { starting: "starting", recording: "recording", stopped: "stopped", error: "error" };

/** Minimal WHEP client (RFC 9725) on RTCPeerConnection. */
export class WhepClient {
  constructor(video, { url = "/api/camera/whep", onState } = {}) {
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
    if (resp.status === 401) { location.href = "/admin/login.html"; throw new Error("login required"); }
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

export function mount(root) {
  const video = h("video", { autoplay: true, muted: true, playsinline: true, hidden: true });
  video.muted = true;
  const img = h("img", { alt: "MJPEG", hidden: true });
  const msg = h(".overlay-msg", null, "Connecting…");
  const tag = h(".tag.badge", null, "—");
  const box = h(".video-box", null, video, img, msg, tag);
  const statsEl = h(".mono.dim", { style: { fontSize: "12.5px", marginTop: "8px", minHeight: "20px" } });

  let mode = "webrtc";
  let client = null;
  let gen = 0;
  let mjpegTimer = null;
  let statsTimer = null;
  let note = "";

  function stopAll() {
    gen++;
    clearTimeout(mjpegTimer);
    clearInterval(statsTimer);
    if (client) { client.stop(); client = null; }
    img.onerror = img.onload = null;
    img.src = BLANK; // aborts the MJPEG stream
    video.hidden = true;
    img.hidden = true;
  }

  function showMsg(t) { msg.textContent = t; msg.hidden = !t; }

  function startMjpeg(reason) {
    stopAll();
    const my = gen;
    mode = "mjpeg";
    seg.set("mjpeg");
    tag.textContent = "MJPEG 768×576";
    note = reason || "";
    showMsg("Connecting to MJPEG…");
    const load = () => {
      if (my !== gen) return;
      img.hidden = false;
      img.onload = () => { showMsg(""); };
      img.onerror = () => { showMsg("No signal from the camera. Retrying…"); mjpegTimer = setTimeout(load, 3000); };
      img.src = "/api/camera/mjpeg?t=" + Date.now();
    };
    load();
    statsEl.textContent = reason ? "WebRTC unavailable: " + reason : "";
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
        if (s === "lost") { showMsg("Connection lost, reconnecting…"); setTimeout(() => { if (my === gen && mode === "webrtc") startWebrtc(); }, 1500); }
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
      startMjpeg(e.message || String(e));
    }
  }

  const seg = segmented({
    name: "camsrc",
    value: "webrtc",
    options: [{ value: "webrtc", label: "WebRTC (H.264)" }, { value: "mjpeg", label: "MJPEG" }],
    onchange: (v) => (v === "webrtc" ? startWebrtc() : startMjpeg("")),
  });

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
          h(".card-head", null, h("h2", null, "Live video"), seg, h("button.btn.sm", { type: "button", title: "Reconnect", onclick: () => (mode === "webrtc" ? startWebrtc() : startMjpeg("")) }, icon("refresh"), "Reload")),
          box, statsEl,
        ),
      ),
      h("div", { style: { display: "flex", flexDirection: "column", gap: "14px" } }, statusCard),
    ),
    settingsCard,
  );

  renderStatus();
  const offT = on("telemetry", renderStatus);
  startWebrtc();

  return () => {
    offT();
    form.destroy();
    stopAll();
  };
}
