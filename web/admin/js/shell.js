// Shell: menu, status bar, routing, notifications from WS events.
import { h, icon, clear, fmt, kmh, isNum, compassWord } from "./dom.js";
import { api, goLogin, errText } from "./api.js";
import { store, start, on } from "./store.js";
import { toast, banner } from "./ui.js";
import { openPreview } from "./preview.js";

const PAGES = [
  { id: "dashboard", title: "Dashboard", icon: "dash", load: () => import("./pages/dashboard.js") },
  { id: "camera", title: "Camera", icon: "cam", load: () => import("./pages/camera.js") },
  { id: "recordings", title: "Recordings", icon: "rec", load: () => import("./pages/recordings.js") },
  { id: "nav", title: "Navigation", icon: "nav", load: () => import("./pages/nav.js") },
  { id: "compass", title: "Compass", icon: "compass", load: () => import("./pages/compass.js") },
  { id: "gps", title: "GPS", icon: "gps", load: () => import("./pages/gps.js") },
  { id: "system", title: "System", icon: "sys", load: () => import("./pages/system.js") },
];

const view = document.getElementById("view");
let cleanup = null;
let routeToken = 0;

// ───── menu ─────
const navEl = document.getElementById("nav");
for (const p of PAGES) {
  navEl.append(h("a", { href: "#/" + p.id, dataset: { id: p.id } }, icon(p.icon), h("span.lbl", null, p.title)));
}

async function route() {
  const id = (location.hash.match(/^#\/([\w-]+)/) || [])[1] || "dashboard";
  const page = PAGES.find((p) => p.id === id) || PAGES[0];
  const token = ++routeToken;
  for (const a of navEl.children) a.classList.toggle("active", a.dataset.id === page.id);
  document.title = page.title + " — fallout.nav";
  if (cleanup) {
    try { cleanup(); } catch (e) { console.error("cleanup", e); }
    cleanup = null;
  }
  clear(view);
  view.scrollTop = 0;
  try {
    const mod = await page.load();
    if (token !== routeToken) return;
    const box = h(".page");
    view.append(box);
    const res = await mod.mount(box, { title: page.title });
    if (token !== routeToken) { if (typeof res === "function") res(); return; }
    cleanup = typeof res === "function" ? res : null;
  } catch (e) {
    console.error("page", page.id, e);
    view.append(h(".page", null, banner("bad", "Page error: " + errText(e))));
  }
}

// ───── status bar ─────
const sb = document.getElementById("statusbar");
function st(key, label, withDot) {
  const dot = withDot ? h("span.dot.rec-dot") : null;
  const txt = h("span", null, "—");
  const el = h(".st", { dataset: { key } }, h("span.k", null, label), h("span.v", null, dot, txt));
  el.txt = txt;
  el.dot = dot;
  return el;
}
const S = {
  gps: st("gps", "GPS", true),
  speed: st("speed", "Speed"),
  head: st("head", "Heading"),
  rec: st("rec", "Rec", true),
  disk: st("disk", "Free"),
  cpu: st("cpu", "CPU"),
  link: st("link", "Link", true),
};
const btnScreen = h("button.btn.sm", { type: "button", onclick: () => openPreview() }, icon("screen"), "Screen");
sb.append(...Object.values(S), h(".grow"), btnScreen);

function setSt(el, text, cls, blink) {
  el.txt.textContent = text;
  el.className = "st" + (cls ? " " + cls : "");
  if (el.dot) el.dot.classList.toggle("on", !!blink);
}

function renderStatus() {
  const t = store.telemetry;
  const online = store.online;
  setSt(S.link, online ? "online" : store.wsOpen ? "no data" : "offline", online ? "ok" : "bad");
  if (!t || !online) {
    for (const k of ["gps", "speed", "head", "rec", "disk", "cpu"]) setSt(S[k], "—", "");
    return;
  }
  const g = t.gps || {};
  const fixTxt = g.fix === "3d" ? "3D" : g.fix === "2d" ? "2D" : "no fix";
  setSt(S.gps, `${fixTxt} · ${g.sats_used ?? 0}/${g.sats_visible ?? 0} sats`, g.fix === "3d" ? "ok" : g.fix === "2d" ? "warn" : "bad");
  setSt(S.speed, isNum(g.speed_mps) ? `${fmt(kmh(g.speed_mps), 0)} km/h` : "—", "");
  const hd = t.heading || {};
  setSt(S.head, isNum(hd.deg) ? `${hd.deg.toFixed(0)}° ${compassWord(hd.deg)}` : "—", "");
  const cam = t.camera || {};
  if (cam.recording) setSt(S.rec, "REC", "bad", true);
  else if (cam.state === "error") setSt(S.rec, "camera error", "bad");
  else setSt(S.rec, cam.state === "starting" ? "starting…" : "not recording", "warn");
  const free = t.disk?.free_gb ?? cam.free_gb;
  setSt(S.disk, isNum(free) ? `${free.toFixed(1)} GB` : "—", isNum(free) && free < 2 ? "bad" : "");
  const temp = t.system?.cpu_temp_c;
  setSt(S.cpu, isNum(temp) ? `${temp.toFixed(0)} °C` : "—", isNum(temp) ? (temp >= 80 ? "bad" : temp >= 70 ? "warn" : "") : "");
}
on("telemetry", renderStatus);
on("conn", renderStatus);
renderStatus();

// ───── WS events → toasts ─────
on("event", (m) => {
  if (m.event === "toast") toast(m.level === "error" ? "error" : m.level === "warn" ? "warn" : "info", m.text || "");
  else if (m.event === "protected") toast("ok", "Recording protected from deletion: " + (m.segment || ""));
  else if (m.event === "button") toast("info", m.value === "long_press" ? "Module button: long press" : "Module button: press");
});

let wasOnline = null;
on("conn", (o) => {
  if (wasOnline === null) { wasOnline = o; return; }
  if (o !== wasOnline) toast(o ? "ok" : "warn", o ? "Connection to the device restored" : "Connection to the device lost");
  wasOnline = o;
});

// ───── logout ─────
document.getElementById("logout").addEventListener("click", async () => {
  try { await api.post("/api/logout", {}, { noRedirect: true }); } catch { /* */ }
  goLogin();
});

// ───── startup ─────
(async function init() {
  try {
    const s = await api.get("/api/session", { noRedirect: true });
    if (!s || !s.authenticated) return goLogin();
  } catch (e) {
    toast("error", "No connection to the device: " + errText(e));
  }
  await start();
  window.addEventListener("hashchange", route);
  route();
})();
