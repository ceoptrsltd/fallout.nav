// Recordings: list by day, in-browser playback (HLS over the .ts: native or local hls.js), download, protect, delete;
// both cameras (the rear's under "rear/"), All / Front / Rear. The player shows the speed and the place at the playback
// moment (the recording's GPS track) and impacts while driving on the timeline.
import { h, icon, clear, fmtBytes, fmtDur, isNum, compassWord } from "../dom.js";
import { api, appUrl, encPath, errText } from "../api.js";
import { loadHls, loadVendorCss, loadVendorScript } from "../vendor.js";
import { hlsUrl, playerKind, HLS_MIME, jumpTo, markPos, parseRecLink, linkOffset, recCamera, filterRecs, recTitle, downloadName } from "../rec-play.js";
import { gpsPath, gpxUrl, gpxName, cleanTrack, playUnix, sampleAt, fmtKmh, fmtLatLon, trackLatLngs, splitMarks, impactText } from "../rec-drive.js";
import { esriTemplates, googleMapsUrl, tileZoomLimits } from "../tracking-fmt.js";
import { deviceClock } from "../devtime.js";
import { kindInfo, markLabel, marksSummary, peakText } from "../alarm-fmt.js";
import { storageWarning } from "../camera-fmt.js";
import { store, on } from "../store.js";
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
const evClock = (e) => deviceClock(e.ts); // the device's clock, like the recording's own time
function dayTitle(d) {
  if (!/^\d{4}-\d\d-\d\d$/.test(d || "")) return "Time not synchronized";
  const dt = new Date(d + "T00:00:00");
  return dt.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
}

const MAP_KEY = "fnav.rec.map"; // the player's map open or not: kept on this phone
const TRACK_REFRESH_MS = 15000; // a recording still being written: its track grows too
const ESRI_ATTR = 'Powered by <a href="https://www.esri.com" target="_blank" rel="noopener">Esri</a>';

/**
 * Speed and place at the playback moment from the recording's GPS track (api/recordings/gps: ~1 point/s, synced by
 * unix time — the recording's start_ts + currentTime), a small map with the track on demand, Google Maps and GPX.
 * Hidden while the recording has no track (older recordings, no fix the whole time).
 */
function drivePanel(rec, video) {
  let track = [];
  let closed = false;
  let map = null, marker = null, line = null;
  let mapOn = false;
  try { mapOn = localStorage.getItem(MAP_KEY) === "1"; } catch { /* */ }
  const spd = h("b.rec-gps-spd", null, "— km/h");
  const pos = h("span.mono.rec-gps-pos", null, "—");
  const crs = h("span.dim.rec-gps-crs");
  const mapBtn = h("button.btn.sm" + (mapOn ? ".on" : ""), { type: "button", title: "The track of this recording on a map", onclick: () => showMap(!mapOn) }, icon("track", 13), "Map");
  const gmaps = h("a.btn.sm", { target: "_blank", rel: "noopener", title: "This place in Google Maps" }, icon("pin", 13), "Google Maps");
  const gpx = h("a.btn.sm", { href: gpxUrl(rec.path), download: gpxName(rec), title: "Download the track (GPX)" }, icon("dl", 13), "GPX");
  const mapEl = h(".rec-map", { hidden: true });
  const el = h(".rec-gps", { hidden: true },
    h(".rec-gps-row", { title: "Where the van was at this moment of the recording (GPS)" }, icon("gps", 14), spd, pos, crs, h(".grow"), mapBtn, gmaps, gpx),
    mapEl);

  function update() {
    if (!track.length || closed) return;
    const s = sampleAt(track, playUnix(rec, video.currentTime));
    el.classList.toggle("nofix", !s);
    if (!s) {
      spd.textContent = "No GPS";
      pos.textContent = "—";
      crs.textContent = "";
      gmaps.removeAttribute("href");
      return;
    }
    spd.textContent = fmtKmh(s.speed);
    pos.textContent = fmtLatLon(s.lat, s.lon);
    crs.textContent = isNum(s.course) && isNum(s.speed) && s.speed >= 1 ? "heading " + compassWord(s.course) : "";
    gmaps.href = googleMapsUrl(s.lat, s.lon);
    if (map && marker) {
      marker.setLatLng([s.lat, s.lon]);
      if (!map.getBounds().contains(marker.getLatLng())) map.panTo(marker.getLatLng(), { animate: false });
    }
  }

  async function showMap(on) {
    mapOn = on;
    try { localStorage.setItem(MAP_KEY, on ? "1" : "0"); } catch { /* */ }
    mapBtn.classList.toggle("on", on);
    mapEl.hidden = !on;
    if (!on || !track.length) return;
    if (map) { map.invalidateSize(); return; }
    let L;
    try {
      await loadVendorCss("leaflet/leaflet.css");
      L = await loadVendorScript("leaflet/leaflet.js", "L");
    } catch (e) {
      mapEl.replaceChildren(h(".overlay-msg", null, "Map library failed to load: " + errText(e)));
      return;
    }
    if (closed || map) return;
    const zl = tileZoomLimits(store.settings);
    const tpl = esriTemplates(store.settings);
    map = L.map(mapEl, { maxZoom: zl.imagery, zoomControl: true });
    map.attributionControl.setPrefix('<a href="https://leafletjs.com" target="_blank" rel="noopener">Leaflet</a>');
    L.tileLayer(tpl.imagery, { maxZoom: zl.imagery, maxNativeZoom: zl.imagery, attribution: ESRI_ATTR }).addTo(map);
    L.tileLayer(tpl.transport, { maxZoom: zl.imagery, maxNativeZoom: zl.overlay }).addTo(map);
    line = L.polyline(trackLatLngs(track), { color: "#ffb62e", weight: 3, opacity: 0.9, interactive: false }).addTo(map);
    marker = L.circleMarker([track[0].lat, track[0].lon], { radius: 6, weight: 2, color: "#0a0c0a", fillColor: "#ffd24a", fillOpacity: 1, interactive: false }).addTo(map);
    map.fitBounds(line.getBounds().pad(0.15), { maxZoom: 17 });
    setTimeout(() => map && map.invalidateSize(), 0); // the modal has just been laid out
    update();
  }

  async function load() {
    try {
      const r = await api.get(gpsPath(rec.path));
      if (closed) return;
      track = cleanTrack(r && r.samples);
    } catch {
      return; // an older device (no such API) or no connection: the player works without it
    }
    el.hidden = !track.length;
    if (line) line.setLatLngs(trackLatLngs(track));
    if (track.length && mapOn && !map) showMap(true);
    update();
  }
  for (const ev of ["timeupdate", "seeked", "loadedmetadata"]) video.addEventListener(ev, update);
  load();
  const timer = rec.in_progress ? setInterval(load, TRACK_REFRESH_MS) : null;
  return {
    el,
    destroy() {
      closed = true;
      if (timer) clearInterval(timer);
      if (map) { map.remove(); map = null; }
    },
  };
}

/**
 * Player: an HLS playlist over the .ts itself (byte ranges from keyframes, fnav-core builds it from the keyframe
 * index), so a click anywhere on the timeline loads only the ~3 s piece there. Safari / iPhone play it natively,
 * other browsers through hls.js (MSE).
 */
export function openPlayer(rec, { at = null } = {}) {
  const video = h("video.rec-video", { controls: true, autoplay: true, playsinline: true });
  // a recording with sound (the rear camera's microphone): played with it — the Play click allows sound; if the
  // browser still refuses, muted (the volume button is there)
  video.muted = !rec.audio;
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
  // marks: movement without an alarm (cars, headlights, passers-by) — grey, under the alarm events; impacts while
  // driving (the recording was protected by them) — red, like alarm events
  const { impacts, others: mks } = splitMarks(rec.marks);
  const tick = (cls, title, off) => {
    const b = h("button.rec-mark." + cls, { type: "button", title, onclick: () => seek(jumpTo(off)) });
    b.dataset.off = String(off);
    return b;
  };
  const strip = evs.length || mks.length || impacts.length ? h(".rec-marks", { title: "Alarm events and impacts while driving (colour), marks without an alarm (grey) in this recording" },
    mks.map((m) => m.kind === "sound"
      ? tick("k-mark.k-sound", `${evClock(m)} · ${markLabel(m.kind)}${m.peak_db != null ? ` (${peakText(m.peak_db)})` : ""}${m.clip ? " · with sound" : ""} · no alarm`, m.offset_s)
      : tick("k-mark", `${evClock(m)} · ${markLabel(m.kind)}${m.dur_s ? ` (${Math.round(m.dur_s)} s)` : ""} · no alarm`, m.offset_s)),
    impacts.map((m) => tick("k-bad.k-impact", `${evClock(m)} · ${impactText(m)}`, m.offset_s)),
    evs.map((e) => {
      const ki = kindInfo(e.kind);
      return tick("k-" + (ki.kind || "plain"), `${evClock(e)} · ${e.title || ki.label}`, e.offset_s);
    })) : null;
  const mkNote = mks.length ? h(".rec-mknote.dim", null, h("span.rec-mkdot"), `Marks without an alarm: ${marksSummary(mks)}`) : null;
  const drive = drivePanel(rec, video);
  // loud sounds (the rear camera's microphone, while armed): in a recording without sound their clips play
  // together with the video, in step; Sound off — silent. A recording with sound plays its own.
  const clips = rec.audio ? [] : mks.filter((x) => x.clip && Number.isFinite(x.clip_offset_s) && x.clip_dur_s > 0);
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
  // alarm events and impacts while driving, in time order: a click jumps a few s before
  const listed = [
    ...evs.map((e) => {
      const ki = kindInfo(e.kind);
      return { off: e.offset_s, cls: ki.kind || "plain", ic: ki.icon, at: evClock(e), text: e.title || ki.label };
    }),
    ...impacts.map((x) => ({ off: x.offset_s, cls: "bad", ic: "warn", at: evClock(x), text: impactText(x) })),
  ].sort((a, b) => a.off - b.off);
  const evList = listed.length ? h(".rec-evs", null, listed.map((x) =>
    h("button.btn.sm.rec-ev.k-" + x.cls, { type: "button", title: `Jump to ${LEAD_TEXT} before it`, onclick: () => seek(jumpTo(x.off)) },
      icon(x.ic, 13), h("span.mono", null, x.at), h("span", null, x.text)))) : null;

  const m = modal({
    title: recTitle(rec),
    body: [video, strip, drive.el, evList, mkNote, sndBtn ? h(".rec-snd", null, sndBtn, audio) : null, status,
      rec.in_progress ? h("div", { style: { marginTop: "8px" } }, banner("warn", "Recording is still in progress: the timeline grows while it is being written.")) : null,
    ],
    foot: [h("a.btn", { href: fileUrl(rec.path), download: downloadName(rec) }, icon("dl"), "Download"), h("button.btn", { onclick: () => m.close() }, "Close")],
    onClose: () => {
      closed = true;
      drive.destroy();
      if (audio) { clearInterval(clipTimer); audio.pause(); audio.removeAttribute("src"); }
      if (hls) { try { hls.destroy(); } catch { /* */ } hls = null; }
      video.pause();
      video.removeAttribute("src");
      try { video.load(); } catch { /* */ }
    },
  });
  video.addEventListener("loadedmetadata", () => {
    if (status.className !== "red") status.textContent = `${rec.path} · ${video.videoWidth}×${video.videoHeight} · H.264${rec.audio ? " + sound" : ""} · ${how} · click anywhere on the timeline to jump there`;
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
      if (p && p.catch) p.catch(() => {
        if (closed || video.muted) return;
        video.muted = true; // autoplay with sound refused: play muted
        const p2 = video.play();
        if (p2 && p2.catch) p2.catch(() => {});
      });
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
  // protected recordings squeeze the unprotected ones (camera.md «Минимум петли»): unprotecting happens here
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
        // "#/recordings?play=<path>&t=<s>&at=<unix>" (alarm log, dashboard): straight to the moment of the event
        const link = parseRecLink(location.hash);
        if (link) {
          history.replaceState(null, "", "#/recordings");
          const r = list.find((x) => x.path === link.play);
          if (r) openPlayer(r, { at: linkOffset(link, r) });
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
    const { impacts, others } = splitMarks(r.marks);
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
        impacts.length ? h("button.badge.bad.rec-evbadge", {
          type: "button", onclick: () => openPlayer(r, { at: impacts[0].offset_s }),
          title: impacts.map((x) => `${evClock(x)} ${impactText(x)}`).join("\n") + "\nClick to watch from the first one",
        }, icon("warn", 12), `${impacts.length} impact${impacts.length === 1 ? "" : "s"}`) : null,
        r.protected ? h("span.badge.warn", null, icon("lock", 12), "protected") : null,
        others.length ? h("button.badge.rec-mkbadge", {
          type: "button", onclick: () => openPlayer(r, { at: others[0].offset_s }),
          title: `Marks without an alarm: ${marksSummary(others)}\nClick to watch from the first one`,
        }, `${others.length} mark${others.length === 1 ? "" : "s"}`) : null,
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
    warnBox,
    body,
  );
  renderWarn();
  const offT = on("telemetry", renderWarn);
  const offS = on("settings", renderWarn);
  const stop = poll(load, 10000);
  return () => { stopped = true; stop(); offT(); offS(); };
}
