// fallout.nav admin service worker (docs/contracts/alarm.md §7): push notifications only.
// No fetch handler: every page and request goes to the network exactly as without a service worker.
// Scope — the admin directory (…/admin/), so it works the same at "/" on the device and under "/fnadmin/".
"use strict";

const ICON = "icons/icon-192.png";
const BADGE = "icons/badge-96.png";
const abs = (u) => new URL(u, self.registration.scope).href;

// The "Silence" button on a notification (Android; iOS shows no buttons — a tap opens the Alarm page there): on
// whatever may have the siren or the hazard lights going, not on the notes — the test, armed / disarmed, a camera
// back, the van offline / back online (tag "offline"), the siren sounded from the app.
const NO_SILENCE = new Set(["test", "armed", "disarmed", "video_back", "offline", "siren"]);
const SILENCE_TAG = "silence"; // the result of the button: one notification, the next replaces it

// short form of alarm-fmt.js deviceName() (a classic worker cannot import the admin's modules)
function deviceName() {
  const ua = self.navigator.userAgent || "";
  const os = /iPad/.test(ua) ? "iPad" : /iPhone|iPod/.test(ua) ? "iPhone" : /Android/.test(ua) ? "Android"
    : /Windows/.test(ua) ? "Windows" : /Mac OS X/.test(ua) ? "Mac" : /Linux/.test(ua) ? "Linux" : "Device";
  const br = /Edg/.test(ua) ? "Edge" : /Firefox\/|FxiOS/.test(ua) ? "Firefox" : /Chrome\/|CriOS/.test(ua) ? "Chrome" : "Safari";
  return `${os} · ${br}`;
}

/** showNotification options for a push body {"title", "body", "tag": kind, "url", "ts", "image"?}; image — the
 *  event's camera snapshot on the server (relative to the admin directory, like url): Android shows a big picture. */
function notificationOptions(d) {
  const tag = d.tag || "alarm";
  const opts = {
    body: d.body || "",
    tag,
    renotify: true,
    data: { url: d.url || "#/alarm" },
    icon: abs(ICON),
    badge: abs(BADGE),
  };
  if (typeof d.ts === "number") opts.timestamp = Math.round(d.ts * 1000);
  if (typeof d.image === "string" && d.image) opts.image = abs(d.image);
  if (!NO_SILENCE.has(tag)) opts.actions = [{ action: "silence", title: "Silence" }];
  return opts;
}

/** The result notification of "Silence" from the device's answer (Response or null — no connection, err). */
async function silenceResult(r, err) {
  const fail = (body) => ({ ok: false, title: "Could not silence the alarm", body });
  if (!r) return fail("No connection to the van" + (err && err.message ? ` (${err.message})` : "") + ". Open the app and press Stop on the Alarm page.");
  let j = null;
  try {
    j = await r.json();
  } catch { /* not JSON: nginx or the network */ }
  if (r.ok) return { ok: true, title: "Siren and hazard lights stopped", body: j && j.state === "off" ? "The alarm is off." : "The alarm stays armed." };
  if (r.status === 401) return fail("Signed out: open the app, sign in and press Stop on the Alarm page.");
  const detail = j && typeof j.detail === "string" ? j.detail : "";
  if (r.status === 503 && /offline/i.test(detail)) return fail("The van is offline: the server cannot reach it.");
  return fail(detail || `The van answered with error ${r.status}.`);
}

/** "Silence": POST api/alarm/silence on the device — same origin, the session cookie (on the device and under
 *  /fnadmin/ alike) — then a short notification with the result (quiet when it worked). */
async function silence() {
  let r = null;
  let err = null;
  try {
    r = await fetch(abs("../api/alarm/silence"), { method: "POST", credentials: "same-origin" });
  } catch (e) {
    err = e;
  }
  const res = await silenceResult(r, err);
  await self.registration.showNotification(res.title, {
    body: res.body, tag: SILENCE_TAG, renotify: true, silent: res.ok, data: { url: "#/alarm" }, icon: abs(ICON), badge: abs(BADGE),
  });
}

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

// push body from fnav-relay: {"title", "body", "tag": kind, "url", "ts", "image"?}
self.addEventListener("push", (event) => {
  let d = {};
  try {
    d = event.data ? event.data.json() : {};
  } catch {
    d = { body: event.data ? event.data.text() : "" };
  }
  event.waitUntil(self.registration.showNotification(d.title || "fallout.nav", notificationOptions(d)));
});

// click: an open admin window comes to the front and goes to the event's page, otherwise a new one opens;
// the "Silence" button — the device stops the siren and the hazard lights, no window
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  if (event.action === "silence") {
    event.waitUntil(silence().catch(() => {}));
    return;
  }
  const url = abs((event.notification.data && event.notification.data.url) || "#/alarm");
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const scope = self.registration.scope;
    const win = wins.find((c) => c.url.startsWith(scope) && !c.url.includes("/login.html")) || wins.find((c) => c.url.startsWith(scope));
    if (win) {
      try {
        await win.focus();
      } catch { /* focus may be refused; the message still switches the page */ }
      // the page itself switches (shell.js): same document — only the hash changes, no reload
      win.postMessage({ type: "fnav-open", url });
      return;
    }
    await self.clients.openWindow(url);
  })());
});

// the browser replaced the subscription (expired / keys rotated): subscribe again and tell the server
self.addEventListener("pushsubscriptionchange", (event) => {
  const api = (p) => new URL("../api/push/" + p, self.registration.scope).href;
  const post = (p, body) => fetch(api(p), {
    method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  event.waitUntil((async () => {
    let sub = event.newSubscription || null;
    if (!sub) {
      const r = await fetch(api("key"), { credentials: "same-origin" });
      if (!r.ok) return;
      const key = (await r.json()).public_key;
      const b = key.replace(/-/g, "+").replace(/_/g, "/");
      const raw = atob(b + "=".repeat((4 - (b.length % 4)) % 4));
      sub = await self.registration.pushManager.subscribe({
        userVisibleOnly: true, applicationServerKey: Uint8Array.from(raw, (c) => c.charCodeAt(0)),
      });
    }
    await post("subscribe", { subscription: sub.toJSON(), name: deviceName() });
    if (event.oldSubscription && event.oldSubscription.endpoint !== sub.endpoint) {
      await post("unsubscribe", { endpoint: event.oldSubscription.endpoint });
    }
  })().catch(() => {}));
});
