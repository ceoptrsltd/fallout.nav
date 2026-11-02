// Shell: menu, status bar, routing, notifications from WS events. Phones (<= 760 px or sideways, css "phone shell"): a
// bottom tab bar with the most used pages and a More sheet with the rest, a one-line status bar; "Install app" in the
// More sheet and the sidebar (install.js). After a deploy: "A new version is available" with Reload (version-fmt.js);
// the installed app has no browser Reload, so it is also in the More sheet and in the page error banner.
import { h, icon, clear, fmt, kmh, isNum, compassWord } from "./dom.js";
import { api, goLogin, errText, ROOT, appUrl } from "./api.js";
import { registerSw, unsubscribeOnLogout } from "./push.js";
import { store, start, on, setOffline, forgetKept } from "./store.js";
import { toast, banner, modal, closeModals, navigate } from "./ui.js";
import { linkBanner } from "./conn-fmt.js";
import { openPreview } from "./preview.js";
import { installEntry } from "./install.js";
import { needsAppLogin, clearAppLogin } from "./app-login.js";
import { versionOf, versionStep, versionDue, VERSION_CHECK_MS } from "./version-fmt.js";
import { setDeviceZone } from "./devtime.js";

const PAGES = [
  { id: "dashboard", title: "Dashboard", icon: "dash", load: () => import("./pages/dashboard.js") },
  { id: "alarm", title: "Alarm", icon: "alarm", load: () => import("./pages/alarm.js") },
  { id: "tracking", title: "Tracking", icon: "track", load: () => import("./pages/tracking.js") },
  { id: "camera", title: "Camera", icon: "cam", load: () => import("./pages/camera.js") },
  { id: "recordings", title: "Recordings", icon: "rec", load: () => import("./pages/recordings.js") },
  { id: "radio", title: "Radio", icon: "radio", load: () => import("./pages/radio.js") },
  { id: "bluetooth", title: "Bluetooth", icon: "bt", load: () => import("./pages/bluetooth.js") },
  { id: "nav", title: "Navigation", icon: "nav", load: () => import("./pages/nav.js") },
  { id: "compass", title: "Compass", icon: "compass", load: () => import("./pages/compass.js") },
  { id: "level", title: "Level", icon: "level", load: () => import("./pages/level.js") },
  { id: "gps", title: "GPS", icon: "gps", load: () => import("./pages/gps.js") },
  { id: "controls", title: "Controls", icon: "keys", load: () => import("./pages/controls.js") },
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

// phone: bottom tab bar (the most used pages + More); the rest in the More sheet
const TABS = ["dashboard", "alarm", "camera", "recordings"];
const alarmDot = h("i.tab-dot", { hidden: true });
const tabbar = h("nav.tabbar", { "aria-label": "Main" });
for (const id of TABS) {
  const p = PAGES.find((x) => x.id === id);
  tabbar.append(h("a.tab", { href: "#/" + id, dataset: { id } },
    h("span.tab-ic", null, icon(p.icon), id === "alarm" ? alarmDot : null), h("span.tab-lbl", null, p.title)));
}
const moreTab = h("button.tab", { type: "button", dataset: { id: "more" }, "aria-haspopup": "dialog", onclick: () => openMore() },
  h("span.tab-ic", null, icon("more")), h("span.tab-lbl", null, "More"));
tabbar.append(moreTab);
document.querySelector(".main").append(tabbar);
let currentId = null;
let moreSheet = null;

function markTabs(id) {
  currentId = id;
  for (const a of tabbar.children) {
    const on = a.dataset.id === id || (a === moreTab && !TABS.includes(id));
    a.classList.toggle("active", on);
    if (a.tagName === "A") a.toggleAttribute("aria-current", on);
  }
}

function openMore() {
  if (moreSheet) return;
  const link = (p) => h("a.more-item" + (p.id === currentId ? ".active" : ""),
    { href: "#/" + p.id, onclick: () => setTimeout(() => moreSheet && moreSheet.close(), 0) }, icon(p.icon), h("span", null, p.title));
  const install = installEntry("button.more-row", "Install app");
  install.addEventListener("click", () => moreSheet && moreSheet.close()); // the steps / the prompt replace the sheet
  const body = [
    h(".more-grid", null, PAGES.filter((p) => !TABS.includes(p.id)).map(link)),
    h(".more-rows", null,
      install,
      h("button.more-row", { type: "button", onclick: () => { moreSheet.close(); openPreview(); } }, icon("screen"), h("span", null, "Device screen")),
      h("button.more-row", { type: "button", onclick: reloadApp }, icon("refresh"), h("span", null, "Reload app")),
      h("button.more-row", { type: "button", onclick: () => logout() }, icon("out"), h("span", null, "Log out"))),
  ];
  moreSheet = modal({ title: "More", body, onClose: () => { install.dispose(); moreSheet = null; } });
  moreSheet.el.classList.add("more-sheet");
}

// alarm state on the Alarm tab: arming amber, armed green, triggered red (blinking)
function renderAlarmDot() {
  const a = store.online && store.telemetry ? store.telemetry.alarm : null;
  const st = a && a.state !== "off" ? a.state : null;
  alarmDot.hidden = !st;
  if (st && alarmDot.className !== "tab-dot " + st) alarmDot.className = "tab-dot " + st;
}
on("telemetry", renderAlarmDot);
on("conn", renderAlarmDot);

// tablet / desktop sidebar: Install app above Log out
document.querySelector(".side-foot").prepend(installEntry("button.btn.sm.side-install", "Install app"));

async function route() {
  // device offline (remote): only the server-side journal is available — open Tracking by default
  const id = (location.hash.match(/^#\/([\w-]+)/) || [])[1] || (store.offline ? "tracking" : "dashboard");
  const page = PAGES.find((p) => p.id === id) || PAGES[0];
  const token = ++routeToken;
  for (const a of navEl.children) a.classList.toggle("active", a.dataset.id === page.id);
  markTabs(page.id);
  closeModals(); // the More sheet, a player, a snapshot: nothing stays over the new page
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
    view.append(h(".page", null, reloadBanner("bad", "Page error: " + errText(e))));
    checkVersion(true); // usually a deploy: the modules of this page are older than the page just loaded
  }
}

// ───── status bar ─────
const sb = document.getElementById("statusbar");
function st(key, label, withDot) {
  const dot = withDot ? h("span.dot.rec-dot") : null;
  const txt = h("span.full", null, "—");
  const short = h("span.short", null, "—"); // phones: the one-line status bar
  const el = h(".st", { dataset: { key } }, h("span.k", null, label), h("span.v", null, dot, txt, short));
  el.txt = txt;
  el.short = short;
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
const btnScreen = h("button.btn.sm.sb-screen", { type: "button", onclick: () => openPreview() }, icon("screen"), "Screen");
sb.append(h(".sb-brand", { "aria-hidden": "true" }, "FNAV"), ...Object.values(S), h(".grow"), btnScreen);

function setSt(el, text, cls, blink, short) {
  el.txt.textContent = text;
  el.short.textContent = short ?? text;
  el.className = "st" + (cls ? " " + cls : "");
  if (el.dot) el.dot.classList.toggle("on", !!blink);
}

function renderStatus() {
  const t = store.telemetry;
  const online = store.online;
  setSt(S.link, online ? "online" : online === null ? "connecting…" : store.wsOpen ? "no data" : "offline",
    online ? "ok" : online === null ? "warn" : "bad");
  if (!t || !online) {
    for (const k of ["gps", "speed", "head", "rec", "disk", "cpu"]) setSt(S[k], "—", "");
    return;
  }
  const g = t.gps || {};
  const fixTxt = g.fix === "3d" ? "3D" : g.fix === "2d" ? "2D" : "no fix";
  setSt(S.gps, `${fixTxt} · ${g.sats_used ?? 0}/${g.sats_visible ?? 0} sats`, g.fix === "3d" ? "ok" : g.fix === "2d" ? "warn" : "bad",
    false, g.fix === "3d" || g.fix === "2d" ? `${fixTxt} ${g.sats_used ?? 0}` : "no fix");
  setSt(S.speed, isNum(g.speed_mps) ? `${fmt(kmh(g.speed_mps), 0)} km/h` : "—", "");
  const hd = t.heading || {};
  setSt(S.head, isNum(hd.deg) ? `${hd.deg.toFixed(0)}° ${compassWord(hd.deg)}` : "—", "");
  const cam = t.camera || {};
  if (cam.recording) setSt(S.rec, "REC", "bad", true);
  else if (cam.state === "error") setSt(S.rec, "camera error", "bad", false, "cam err");
  else setSt(S.rec, cam.state === "starting" ? "starting…" : "not recording", "warn", false, cam.state === "starting" ? "…" : "no rec");
  const free = t.disk?.free_gb ?? cam.free_gb;
  setSt(S.disk, isNum(free) ? `${free.toFixed(1)} GB` : "—", isNum(free) && free < 2 ? "bad" : "");
  const temp = t.system?.cpu_temp_c;
  setSt(S.cpu, isNum(temp) ? `${temp.toFixed(0)} °C` : "—", isNum(temp) ? (temp >= 80 ? "bad" : temp >= 70 ? "warn" : "") : "",
    false, isNum(temp) ? `${temp.toFixed(0)}°` : "—");
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
async function logout() {
  clearAppLogin();
  forgetKept(); // the last position and alarm state must not stay on this phone
  await unsubscribeOnLogout(); // no alarm texts to a logged-out phone (needs the session: before api/logout)
  try { await api.post("/api/logout", {}, { noRedirect: true, timeout: 8000 }); } catch { /* */ }
  goLogin();
}
document.getElementById("logout").addEventListener("click", logout);

// ───── device offline (remote access, docs/contracts/remote.md) or the link lost: follows the connection ─────
const offlineBanner = banner("warn", "");
offlineBanner.classList.add("offline-banner");
offlineBanner.hidden = true;
view.before(offlineBanner);
function renderLinkBanner() {
  const b = linkBanner({ offline: store.offline, online: store.online });
  offlineBanner.hidden = !b;
  if (b && offlineBanner.lastChild.textContent !== b.text) offlineBanner.lastChild.textContent = b.text;
}
on("conn", renderLinkBanner);

// ───── new version after a deploy (web/admin/version.json, version-fmt.js) ─────
// the modules loaded at startup stay old until the page reloads: the installed app has no browser Reload button
function reloadApp() {
  location.reload();
}

/** Banner with a Reload button (page error, new version). */
function reloadBanner(kind, text, iconName) {
  const b = banner(kind, [h("span", null, text),
    h("button.btn.sm", { type: "button", onclick: reloadApp }, icon("refresh"), "Reload")], iconName);
  b.classList.add("banner-act");
  return b;
}

const updateBanner = reloadBanner("ok", "A new version is available", "refresh");
updateBanner.classList.add("update-banner");
updateBanner.hidden = true;
view.before(updateBanner);

const VERSION_URL = appUrl("admin/version.json");
let verBase; // undefined: not known yet; null: no marker here (dev, mock) — off; a string: what this page started with
let verAt = NaN;
let verBusy = false;

async function checkVersion(force) {
  if (verBase === null || verBusy || !updateBanner.hidden) return;
  if (!versionDue(verAt, Date.now(), { hidden: document.hidden, force })) return;
  verBusy = true;
  verAt = Date.now();
  let res;
  try {
    const r = await fetch(VERSION_URL, { cache: "no-store", credentials: "same-origin" });
    res = { status: r.status, version: r.ok ? versionOf(await r.json().catch(() => null)) : null };
  } catch {
    res = { status: 0, version: null }; // no connection: the next check decides
  } finally {
    verBusy = false;
  }
  const s = versionStep(verBase, res);
  verBase = s.base;
  if (s.changed) updateBanner.hidden = false;
}
checkVersion(true); // as early as possible: the version of the modules this page has just loaded
setInterval(() => checkVersion(false), VERSION_CHECK_MS);
document.addEventListener("visibilitychange", () => { if (!document.hidden) checkVersion(false); });

// ───── service worker: push notifications (docs/contracts/alarm.md §7), only in a secure context ─────
registerSw();
if (window.isSecureContext && "serviceWorker" in navigator) {
  // a click on a notification while the admin is open: sw.js focuses this window and sends the page to open
  navigator.serviceWorker.addEventListener("message", (ev) => {
    const m = ev.data;
    if (!m || m.type !== "fnav-open" || typeof m.url !== "string") return;
    let u;
    try { u = new URL(m.url, location.href); } catch { return; }
    if (u.origin !== location.origin || !u.pathname.startsWith(ROOT + "admin/")) return;
    const isIndex = (p) => p === ROOT + "admin/" || p === ROOT + "admin/index.html";
    // the open dialogs close (a player must not keep playing over the alarm page)
    if (isIndex(u.pathname) && isIndex(location.pathname)) navigate(u.hash);
    else location.href = u.href;
  });
}

// ───── startup ─────
(async function init() {
  try {
    const s = await api.get("/api/session", { noRedirect: true });
    if (!s || !s.authenticated || needsAppLogin()) return goLogin();
    if (s.offline) setOffline(true);
    setDeviceZone(s.timezone); // recording names are the device's local time
  } catch (e) {
    toast("error", "No connection to the device: " + errText(e));
  }
  await start();
  window.addEventListener("hashchange", route);
  route();
})();
