// Recordings: list by day, in-browser .ts playback (local mpegts.js), download, protect, delete.
import { h, icon, clear, fmtBytes, fmtDur, isNum } from "../dom.js";
import { api, encPath, errText } from "../api.js";
import { modal, confirmDialog, toast, poll, badge, emptyState, banner } from "../ui.js";

let mpegtsPromise = null;
function loadMpegts() {
  if (window.mpegts) return Promise.resolve(window.mpegts);
  if (!mpegtsPromise) {
    mpegtsPromise = new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = "/admin/vendor/mpegts.js";
      s.onload = () => (window.mpegts ? resolve(window.mpegts) : reject(new Error("mpegts.js failed to initialize")));
      s.onerror = () => { mpegtsPromise = null; reject(new Error("could not load the mpegts.js player")); };
      document.head.append(s);
    });
  }
  return mpegtsPromise;
}

function fileUrl(path) { return "/api/recordings/file/" + encPath(path); }
function timeOf(r) {
  if (r.start) { const m = String(r.start).match(/T(\d\d:\d\d:\d\d)/); if (m) return m[1]; }
  const m = r.path.match(/_(\d\d)-(\d\d)-(\d\d)\.ts$/);
  return m ? `${m[1]}:${m[2]}:${m[3]}` : r.path.split("/").pop();
}
function dayTitle(d) {
  if (!/^\d{4}-\d\d-\d\d$/.test(d || "")) return "Time not synchronized";
  const dt = new Date(d + "T00:00:00");
  return dt.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
}

export function openPlayer(rec) {
  const video = h("video", { controls: true, autoplay: true, style: { width: "min(900px, 82vw)", maxHeight: "68vh", background: "#000", display: "block" } });
  video.muted = true;
  const status = h(".dim", { style: { marginTop: "8px", fontSize: "12.5px" } }, "Loading player…");
  let player = null;
  let closed = false;
  const m = modal({
    title: `Recording ${rec.path}`,
    body: [video, status,
      rec.in_progress ? h("div", { style: { marginTop: "8px" } }, banner("warn", "Recording is still in progress: the file is being appended to, playback may stop at the last loaded point.")) : null,
    ],
    foot: [h("a.btn", { href: fileUrl(rec.path), download: rec.path.split("/").pop() }, icon("dl"), "Download"), h("button.btn", { onclick: () => m.close() }, "Close")],
    onClose: () => {
      closed = true;
      if (player) { try { player.pause(); player.unload(); player.detachMediaElement(); player.destroy(); } catch { /* */ } player = null; }
      video.removeAttribute("src");
    },
  });
  (async () => {
    try {
      const mpegts = await loadMpegts();
      if (closed) return;
      if (!mpegts.isSupported()) throw new Error("this browser has no MSE support: download the file and open it in VLC");
      player = mpegts.createPlayer({ type: "mpegts", url: fileUrl(rec.path), isLive: false, hasAudio: false }, { enableWorker: false, lazyLoadMaxDuration: 120, seekType: "range" });
      player.on(mpegts.Events.ERROR, (type, detail, info) => {
        console.warn("mpegts error", type, detail, info);
        status.textContent = "Playback error: " + type + " / " + detail + (info && info.msg ? " — " + info.msg : "");
        status.className = "red";
      });
      video.addEventListener("loadedmetadata", () => {
        if (status.className !== "red") status.textContent = `${video.videoWidth}×${video.videoHeight} · H.264 via MSE · seeking works within the loaded part`;
      });
      player.attachMediaElement(video);
      player.load();
      const p = player.play();
      if (p && p.catch) p.catch(() => {});
    } catch (e) {
      status.textContent = "Could not play: " + (e.message || e);
      status.className = "red";
    }
  })();
}

export function mount(root) {
  const closedDays = new Set();
  let list = [];
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
        const days = [...new Set(list.map((r) => r.date))];
        days.slice(1).forEach((d) => closedDays.add(d));
      }
      render();
    } catch (e) {
      if (stopped) return;
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
      load();
    } catch (e) { toast("error", errText(e)); }
  }

  function row(r) {
    return h(".rec-row", null,
      h("span.t", null, timeOf(r)),
      h("span.num", null, fmtBytes(r.size_bytes)),
      h("span.num", null, isNum(r.duration_s) ? fmtDur(r.duration_s) : "—"),
      h("span", { style: { display: "flex", gap: "6px" } },
        r.in_progress ? badge("● recording", "rec") : null,
        r.protected ? h("span.badge.warn", null, icon("lock", 12), "protected") : null,
      ),
      h("span.btnrow", { style: { justifyContent: "flex-end" } },
        h("button.btn.sm", { title: "Watch in browser", onclick: () => openPlayer(r) }, icon("play"), "Watch"),
        h("a.btn.sm", { title: "Download", href: fileUrl(r.path), download: r.path.split("/").pop() }, icon("dl")),
        h("button.btn.sm" + (r.protected ? ".on" : ""), { title: r.protected ? "Remove protection" : "Protect from deletion", onclick: () => toggleProtect(r) }, icon(r.protected ? "lock" : "unlock")),
        h("button.btn.sm.danger", { title: r.in_progress ? "Recording in progress: cannot delete" : "Delete", disabled: !!r.in_progress, onclick: () => del(r) }, icon("trash")),
      ),
    );
  }

  function render() {
    const total = list.reduce((a, r) => a + (r.size_bytes || 0), 0);
    summary.textContent = `${list.length} file${list.length === 1 ? "" : "s"} · ${fmtBytes(total)} · protected: ${list.filter((r) => r.protected).length}`;
    clear(body);
    if (!list.length) return body.append(emptyState("No recordings yet"));
    const groups = new Map();
    for (const r of list) {
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
      body.append(day);
    }
  }

  root.append(
    h(".page-head", null,
      h("h1", null, "Recordings"),
      summary,
      h("button.btn", { onclick: async () => {
        try { const r = await api.post("/api/recordings/protect-current"); toast("ok", "Current recording protected" + (r && r.segment ? ": " + r.segment : "")); load(); } catch (e) { toast("error", errText(e)); }
      } }, icon("lock"), "Protect current"),
      h("button.btn", { onclick: load }, icon("refresh"), "Refresh"),
    ),
    body,
  );
  const stop = poll(load, 10000);
  return () => { stopped = true; stop(); };
}
