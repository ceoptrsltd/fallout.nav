// fallout.nav admin service worker (docs/contracts/alarm.md §7): push notifications only.
// No fetch handler: every page and request goes to the network exactly as without a service worker.
// Scope — the admin directory (…/admin/), so it works the same at "/" on the device and under "/fnadmin/".
"use strict";

const ICON = "icons/icon-192.png";
const BADGE = "icons/badge-96.png";
const abs = (u) => new URL(u, self.registration.scope).href;

// short form of alarm-fmt.js deviceName() (a classic worker cannot import the admin's modules)
function deviceName() {
  const ua = self.navigator.userAgent || "";
  const os = /iPad/.test(ua) ? "iPad" : /iPhone|iPod/.test(ua) ? "iPhone" : /Android/.test(ua) ? "Android"
    : /Windows/.test(ua) ? "Windows" : /Mac OS X/.test(ua) ? "Mac" : /Linux/.test(ua) ? "Linux" : "Device";
  const br = /Edg/.test(ua) ? "Edge" : /Firefox\/|FxiOS/.test(ua) ? "Firefox" : /Chrome\/|CriOS/.test(ua) ? "Chrome" : "Safari";
  return `${os} · ${br}`;
}

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

// push body from fnav-relay: {"title", "body", "tag": kind, "url", "ts"}
self.addEventListener("push", (event) => {
  let d = {};
  try {
    d = event.data ? event.data.json() : {};
  } catch {
    d = { body: event.data ? event.data.text() : "" };
  }
  const opts = {
    body: d.body || "",
    tag: d.tag || "alarm",
    renotify: true,
    data: { url: d.url || "#/alarm" },
    icon: abs(ICON),
    badge: abs(BADGE),
  };
  if (typeof d.ts === "number") opts.timestamp = Math.round(d.ts * 1000);
  event.waitUntil(self.registration.showNotification(d.title || "fallout.nav", opts));
});

// click: an open admin window comes to the front and goes to the event's page, otherwise a new one opens
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
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
