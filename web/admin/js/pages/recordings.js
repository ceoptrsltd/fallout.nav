// Recordings: list by day, in-browser playback (HLS over the .ts: native or local hls.js), download, protect, delete;
// both cameras (the rear's under "rear/"), All / Front / Rear.
import { h, icon, clear, fmtBytes, fmtDur, isNum } from "../dom.js";
import { api, appUrl, encPath, errText } from "../api.js";
import { loadHls } from "../vendor.js";
import { hlsUrl, playerKind, HLS_MIME, jumpTo, markPos, parseRecLink, recCamera, filterRecs, recTitle, downloadName } from "../rec-play.js";
import { kindInfo, markLabel, marksSummary, peakText } from "../alarm-fmt.js";
import { store } from "../store.js";
import { modal, confirmDialog, toast, poll, badge, emptyState, banner, segmented } from "../ui.js";

let camFilter = "all"; // All / Front / Rear: kept while the app is open

function fileUrl(path) { return appUrl("api/recordings/file/" + encPath(path)); }
/** A loud sound's clip (the rear camera's microphone, WAV): api/alarm/sound/<name>. */
export function soundUrl(name) { return appUrl("api/alarm/sound/" + encodeURIComponent(name)); }
function timeOf(r) {
  if (r.start) { const m = String(r.start).match(/T(\d\d:\d\d:\d\d)/); if (m) return m[1]; }
  const m = r.path.match(/_(\d\d)-(\d\d)-(\d\d)\.ts$/);
  return m ? `${m[1]}:${m[2]}:${m[3]}` : r.path.split("/").pop();
}
const LEAD_TEXT = "a few seconds";
const evClock = (e) => (Number.isFinite(e.ts) ? new Date(e.ts * 1000).toLocaleTimeString("en-GB") : "");
function dayTitle(d) {
  if (!/^\d{4}-\d\d-\d\d$/.test(d || "")) return "Time not synchronized";
  const dt = new Date(d + "T00:00:00");
  return dt.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
}

/**
 * Player: an HLS playlist over the .ts itself (byte ranges from keyframes, fnav-core builds it from the keyframe
 * index), so a click anywhere on the timeline loads only the ~3 s piece there. Safari / iPhone play it natively,
 * other browsers through hls.js (MSE).
 */
export function openPlayer(rec, { at = null } = {}) {
  const video = h("video.rec-video", { controls: true, autoplay: true, playsinline: true });
  video.muted = true;
  video.playsInline = true;
  const status = h(".dim", { style: { marginTop: "8px", fontSize: "12.5px" } }, "Loading player…");
  let hls = null;
  let closed = false;
  let how = "";
  const fail = (text) => { status.textContent = text; status.className = "red"; };

  // alarm events in this recording: markers on a strip under the video and a list; a click jumps a few s before
  const evs = Array.isArray(rec.events) ? rec.events : [];
  let pendingSeek = Number.isFinite(at) ? jumpTo(at) : null;
  const seek = (t) => {
    if (video.readyState >= 1) {
      video.currentTime = t;
      const p = video.play();
      if (p && p.catch) p.catch(() => {});
    } else pendingSeek = t;
  };
  video.addEventListener("loadedmetadata", () => {
    if (pendingSeek !== null) { video.currentTime = pendingSeek; pendingSeek = null; }
  });
  // marks: movement without an alarm (cars, headlights, passers-by) — grey, under the alarm events
  const mks = Array.isArray(rec.marks) ? rec.marks : [];
  const tick = (cls, title, off) => {
    const b = h("button.rec-mark." + cls, { type: "button", title, onclick: () => seek(jumpTo(off)) });
    b.dataset.off = String(off);
    return b;
  };
  const strip = evs.length || mks.length ? h(".rec-marks", { title: "Alarm events (colour) and marks without an alarm (grey) in this recording" },
    mks.map((m) => m.kind === "sound"
      ? tick("k-mark.k-sound", `${evClock(m)} · ${markLabel(m.kind)}${m.peak_db != null ? ` (${peakText(m.peak_db)})` : ""}${m.clip ? " · with sound" : ""} · no alarm`, m.offset_s)
      : tick("k-mark", `${evClock(m)} · ${markLabel(m.kind)}${m.dur_s ? ` (${Math.round(m.dur_s)} s)` : ""} · no alarm`, m.offset_s)),
    evs.map((e) => {
      const ki = kindInfo(e.kind);
      return tick("k-" + (ki.kind || "plain"), `${evClock(e)} · ${e.title || ki.label}`, e.offset_s);
    })) : null;
  const mkNote = mks.length ? h(".rec-mknote.dim", null, h("span.rec-mkdot"), `Marks without an alarm: ${marksSummary(mks)}`) : null;
  // loud sounds (the rear camera's microphone, while armed): their clips play together with the video, in step —
  // the recording itself has no sound; Sound off — silent
  const clips = mks.filter((x) => x.clip && Number.isFinite(x.clip_offset_s) && x.clip_dur_s > 0);
  const audio = clips.length ? h("audio", { preload: "none" }) : null;
  let soundOn = true;
  let clipNow = null;
  const syncClip = () => {
    if (!audio) return;
    const t = video.currentTime;
    // the video really plays: not paused, not seeking and not waiting for data (a stalled video stops the clip too)
    const playing = !video.paused && !video.seeking && video.readyState >= 3;
    const c = soundOn && playing ? clips.find((x) => t >= x.clip_offset_s && t < x.clip_offset_s + x.clip_dur_s) : null;
    if (!c) {
      if (!audio.paused) audio.pause();
      clipNow = null;
      return;
    }
    const want = t - c.clip_offset_s;
    if (clipNow !== c) {
      clipNow = c;
      audio.src = soundUrl(c.clip);
      audio.currentTime = want;
    } else if (Math.abs(audio.currentTime - want) > 0.3) audio.currentTime = want;
    audio.playbackRate = video.playbackRate;
    if (audio.paused) {
      const p = audio.play();
      if (p && p.catch) p.catch(() => {});
    }
  };
  if (audio) for (const evName of ["timeupdate", "play", "playing", "pause", "waiting", "stalled", "seeking", "seeked", "ratechange"]) video.addEventListener(evName, syncClip);
  const clipTimer = audio ? setInterval(syncClip, 400) : null; // drift and stalls without an event
  const sndBtn = audio ? h("button.btn.sm.rec-sndbtn.on", { type: "button", title: "Loud sounds in this recording play with the video (the camera's microphone)" },
    icon("vol", 13), h("span", null, "Sound: on")) : null;
  if (sndBtn) sndBtn.onclick = () => {
    soundOn = !soundOn;
    sndBtn.classList.toggle("on", soundOn);
    sndBtn.lastChild.textContent = soundOn ? "Sound: on" : "Sound: off";
    syncClip();
  };
  const placeMarks = () => {
    if (!strip) return;
    const d = Number.isFinite(video.duration) && video.duration > 0 ? video.duration : rec.duration_s;
    for (const b of strip.children) {
      const p = markPos(Number(b.dataset.off), d);
      b.hidden = p === null;
      if (p !== null) b.style.left = p * 100 + "%";
    }
  };
  video.addEventListener("durationchange", placeMarks);
  placeMarks();
  const evList = evs.length ? h(".rec-evs", null, evs.map((e) => {
    const ki = kindInfo(e.kind);
    return h("button.btn.sm.rec-ev.k-" + (ki.kind || "plain"), { type: "button", title: `Jump to ${LEAD_TEXT} before the event`, onclick: () => seek(jumpTo(e.offset_s)) },
      icon(ki.icon, 13), h("span.mono", null, evClock(e)), h("span", null, e.title || ki.label));
  })) : null;

  const m = modal({
    title: recTitle(rec),
    body: [video, strip, evList, mkNote, sndBtn ? h(".rec-snd", null, sndBtn, audio) : null, status,
      rec.in_progress ? h("div", { style: { marginTop: "8px" } }, banner("warn", "Recording is still in progress: the timeline grows while it is being written.")) : null,
    ],
    foot: [h("a.btn", { href: fileUrl(rec.path), download: downloadName(rec) }, icon("dl"), "Download"), h("button.btn", { onclick: () => m.close() }, "Close")],
    onClose: () => {
      closed = true;
      if (audio) { clearInterval(clipTimer); audio.pause(); audio.removeAttribute("src"); }
      if (hls) { try { hls.destroy(); } catch { /* */ } hls = null; }
      video.pause();
      video.removeAttribute("src");
      try { video.load(); } catch { /* */ }
    },
  });
  video.addEventListener("loadedmetadata", () => {
    if (status.className !== "red") status.textContent = `${rec.path} · ${video.videoWidth}×${video.videoHeight} · H.264 · ${how} · click anywhere on the timeline to jump there`;
  });
  video.addEventListener("error", () => {
    if (hls || closed) return; // hls.js reports its own errors
    const e = video.error;
    fail("Playback error" + (e ? ` (${e.code}${e.message ? ": " + e.message : ""})` : "") + ": download the file and open it in VLC");
  });
  (async () => {
    try {
      const url = hlsUrl(rec.path);
      const mse = !!(window.MediaSource || window.ManagedMediaSource);
      let kind = playerKind({ vendor: navigator.vendor, nativeHls: video.canPlayType(HLS_MIME) !== "", mse });
      let Hls = null;
      if (kind === "hlsjs") {
        Hls = await loadHls();
        if (closed) return;
        if (!Hls.isSupported()) kind = video.canPlayType(HLS_MIME) ? "native" : "none";
      }
      if (kind === "none") throw new Error("this browser cannot play HLS video: download the file and open it in VLC");
      if (kind === "native") {
        how = "native HLS";
        video.src = url;
      } else {
        how = "HLS via hls.js";
        // backBufferLength: decoded 1440×1080 behind the playhead is freed (phones)
        hls = new Hls({ backBufferLength: 30, maxBufferLength: 30, startPosition: pendingSeek ?? -1 });
        let recovered = false;
        hls.on(Hls.Events.ERROR, (_ev, d) => {
          if (!d.fatal) return;
          console.warn("hls.js error", d);
          if (d.type === Hls.ErrorTypes.MEDIA_ERROR && !recovered) { recovered = true; hls.recoverMediaError(); return; }
          const code = d.response && d.response.code;
          fail("Playback error: " + d.details + (code ? ` (HTTP ${code})` : ""));
        });
        hls.loadSource(url);
        hls.attachMedia(video);
      }
      const p = video.play();
      if (p && p.catch) p.catch(() => {});
    } catch (e) {
      fail("Could not play: " + (e.message || e));
    }
  })();
}

export function mount(root) {
  const closedDays = new Set();
  let list = [];
  let shown = null; // what render() last drew: the 10 s poll does not rebuild an unchanged list
  let firstLoad = true;
  const body = h("div", { style: { display: "flex", flexDirection: "column", gap: "12px" } });
  const summary = h("span.dim.mono");
  let stopped = false;

  async function load() {
    try {
      list = await api.get("/api/recordings");
      if (stopped) return;
      if (firstLoad) {
        firstLoad = false;
        // only the newest day is expanded by default
        const days = [...new Set(filterRecs(list, camFilter).map((r) => r.date))];
        days.slice(1).forEach((d) => closedDays.add(d));
        render();
        // "#/recordings?play=<path>&t=<s>" (alarm log, dashboard): straight to the moment of the event
        const link = parseRecLink(location.hash);
        if (link) {
          history.replaceState(null, "", "#/recordings");
          const r = list.find((x) => x.path === link.play);
          if (r) openPlayer(r, { at: link.t });
          else toast("warn", "That recording is no longer on the device");
        }
        return;
      }
      render();
    } catch (e) {
      if (stopped) return;
      shown = null;
      clear(body).append(banner("bad", "Could not load the recordings list: " + errText(e)));
    }
  }

  async function toggleProtect(r) {
    try {
      await api.post("/api/recordings/protect", { path: r.path, protected: !r.protected });
      toast("ok", r.protected ? "Protection removed" : "Recording protected from deletion");
      load();
    } catch (e) { toast("error", errText(e)); }
  }

  async function del(r) {
    const ok = await confirmDialog({
      title: r.protected ? "Delete a PROTECTED recording?" : "Delete recording?",
      text: h("div", null, h("div.mono", null, r.path), h("div", { style: { marginTop: "8px" } }, r.protected ? "This recording is protected from deletion. It will be deleted permanently." : "The file will be deleted permanently.")),
      okText: "Delete",
      danger: true,
    });
    if (!ok) return;
    try {
      await api.del("/api/recordings/file/" + encPath(r.path) + (r.protected ? "?force=1" : ""));
      toast("ok", "Deleted: " + r.path.split("/").pop());
      // the row goes away where it is: the list stays where it was scrolled to
      list = list.filter((x) => x.path !== r.path);
      render();
      load();
    } catch (e) { toast("error", errText(e)); }
  }

  function row(r) {
    const rear = recCamera(r) === "rear";
    return h(".rec-row" + (rear ? ".rear" : ""), null,
      h("span.t", null, timeOf(r)),
      h("span.num", null, fmtBytes(r.size_bytes)),
      h("span.num", null, isNum(r.duration_s) ? fmtDur(r.duration_s) : "—"),
      h("span", { style: { display: "flex", gap: "6px" } },
        rear ? h("span.badge.rec-cam", { title: "Rear camera" }, "Rear") : null,
        r.in_progress ? badge("● recording", "rec") : null,
        r.events && r.events.length ? h("button.badge.bad.rec-evbadge", {
          type: "button", onclick: () => openPlayer(r, { at: r.events[0].offset_s }),
          title: r.events.map((e) => `${evClock(e)} ${e.title}`).join("\n") + "\nClick to watch from the first one",
        }, icon("alarm", 12), `${r.events.length} event${r.events.length === 1 ? "" : "s"}`) : null,
        r.protected ? h("span.badge.warn", null, icon("lock", 12), "protected") : null,
        r.marks && r.marks.length ? h("button.badge.rec-mkbadge", {
          type: "button", onclick: () => openPlayer(r, { at: r.marks[0].offset_s }),
          title: `Marks without an alarm: ${marksSummary(r.marks)}\nClick to watch from the first one`,
        }, `${r.marks.length} mark${r.marks.length === 1 ? "" : "s"}`) : null,
      ),
      h("span.btnrow", { style: { justifyContent: "flex-end" } },
        h("button.btn.sm", { title: "Watch in browser", onclick: () => openPlayer(r) }, icon("play"), "Watch"),
        h("a.btn.sm", { title: "Download", href: fileUrl(r.path), download: downloadName(r) }, icon("dl")),
        h("button.btn.sm" + (r.protected ? ".on" : ""), { title: r.protected ? "Remove protection" : "Protect from deletion", onclick: () => toggleProtect(r) }, icon(r.protected ? "lock" : "unlock")),
        h("button.btn.sm.danger", { title: r.in_progress ? "Recording in progress: cannot delete" : "Delete", disabled: !!r.in_progress, onclick: () => del(r) }, icon("trash")),
      ),
    );
  }

  // All / Front / Rear: shown once the device has a rear camera (turned on or recordings of it)
  const camSeg = segmented({ name: "rec-cam", value: camFilter, onchange: (v) => setFilter(v),
    options: [{ value: "all", label: "All" }, { value: "front", label: "Front" }, { value: "rear", label: "Rear" }] });
  camSeg.classList.add("rec-camseg");
  camSeg.hidden = !(store.settings && store.settings.rear_camera && store.settings.rear_camera.enabled);
  function setFilter(v) {
    camFilter = v;
    // the newest day of what is shown now opens
    const first = filterRecs(list, camFilter)[0];
    if (first) closedDays.delete(first.date);
    render();
  }

  function render() {
    const hasRear = !!(store.settings && store.settings.rear_camera && store.settings.rear_camera.enabled) || list.some((r) => recCamera(r) === "rear");
    camSeg.hidden = !hasRear;
    const cam = hasRear ? camFilter : "all";
    const recs = filterRecs(list, cam);
    const sig = cam + JSON.stringify(list);
    if (sig === shown) return;
    shown = sig;
    const total = recs.reduce((a, r) => a + (r.size_bytes || 0), 0);
    summary.textContent = `${recs.length} file${recs.length === 1 ? "" : "s"} · ${fmtBytes(total)} · protected: ${recs.filter((r) => r.protected).length}`;
    // the page scrolls in #view: rebuilding the list must not move it (one DOM swap, the position put back)
    const scroller = body.closest("#view");
    const top = scroller ? scroller.scrollTop : 0;
    const days = [];
    if (!recs.length) days.push(emptyState(cam === "rear" ? "No rear camera recordings yet" : cam === "front" ? "No front camera recordings yet" : "No recordings yet"));
    const groups = new Map();
    for (const r of recs) {
      if (!groups.has(r.date)) groups.set(r.date, []);
      groups.get(r.date).push(r);
    }
    for (const [date, items] of groups) {
      const size = items.reduce((a, r) => a + (r.size_bytes || 0), 0);
      const dur = items.reduce((a, r) => a + (r.duration_s || 0), 0);
      const day = h(".day" + (closedDays.has(date) ? ".closed" : ""), null,
        h(".day-head", { onclick: () => { closedDays.has(date) ? closedDays.delete(date) : closedDays.add(date); day.classList.toggle("closed"); } },
          h("span.chev", null, "▾"),
          h("b", null, dayTitle(date)),
          h("span.dim.mono", null, date && /^\d{4}/.test(date) ? date : ""),
          h(".grow"),
          h("span.dim.mono", null, `${items.length} file${items.length === 1 ? "" : "s"} · ${fmtBytes(size)} · ${fmtDur(dur)}`),
        ),
        h(".day-body", null, items.map(row)),
      );
      days.push(day);
    }
    body.replaceChildren(...days);
    if (scroller) scroller.scrollTop = top;
  }

  root.append(
    h(".page-head", null,
      h("h1", null, "Recordings"),
      summary,
      camSeg,
      h("button.btn", { onclick: async () => {
        try {
          const r = await api.post("/api/recordings/protect-current");
          const segs = r ? [r.segment, r.segment_rear].filter(Boolean) : [];
          toast("ok", (segs.length > 1 ? "Current recordings protected" : "Current recording protected") + (segs.length ? ": " + segs.join(", ") : ""));
          load();
        } catch (e) { toast("error", errText(e)); }
      } }, icon("lock"), "Protect current"),
      h("button.btn", { onclick: load }, icon("refresh"), "Refresh"),
    ),
    body,
  );
  const stop = poll(load, 10000);
  return () => { stopped = true; stop(); };
}
