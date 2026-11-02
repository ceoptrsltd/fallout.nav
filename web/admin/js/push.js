// Push notifications in the browser (docs/contracts/alarm.md §6–7): service worker web/admin/sw.js (scope — the
// admin directory), subscription through the server (fnav-relay answers api/push/* behind https …/fnadmin/).
// The device itself serves the admin over plain http: there the browser has no service worker and no Push API.
import { api } from "./api.js";
import { b64uToBytes, bytesToB64u, deviceName, isIos, pushMode } from "./alarm-fmt.js";

const ID_KEY = "fnav.push.id"; // id of this browser's subscription on the server (to mark "this device")

/** Facts about this browser for alarm-fmt.pushMode(). */
export function pushEnv() {
  const nav = typeof navigator !== "undefined" ? navigator : {};
  const ios = isIos({ ua: nav.userAgent || "", platform: nav.platform || "", touch: nav.maxTouchPoints || 0 });
  const standalone = nav.standalone === true
    || (typeof matchMedia === "function" && (matchMedia("(display-mode: standalone)").matches || matchMedia("(display-mode: fullscreen)").matches));
  return {
    secure: typeof window !== "undefined" && window.isSecureContext === true,
    sw: "serviceWorker" in nav,
    push: typeof window !== "undefined" && "PushManager" in window,
    notification: typeof Notification !== "undefined",
    ios,
    standalone,
  };
}

export const thisDeviceName = () => deviceName({
  ua: navigator.userAgent || "", platform: navigator.platform || "", touch: navigator.maxTouchPoints || 0,
});

export const currentMode = () => pushMode(pushEnv());

/** Register sw.js (once per page load, from shell.js). Only in a secure context; errors are only logged. */
export async function registerSw() {
  const env = pushEnv();
  if (!env.secure || !env.sw) return null;
  try {
    return await navigator.serviceWorker.register("sw.js", { scope: "./" });
  } catch (e) {
    console.warn("service worker", e);
    return null;
  }
}

/** The admin's service worker registration (registers it if needed), active. */
export async function swReady(timeoutMs = 8000) {
  let reg = await navigator.serviceWorker.getRegistration("./");
  if (!reg) reg = await navigator.serviceWorker.register("sw.js", { scope: "./" });
  if (reg.active) return reg;
  return Promise.race([
    navigator.serviceWorker.ready,
    new Promise((_, rej) => setTimeout(() => rej(new Error("The service worker did not start")), timeoutMs)),
  ]);
}

export const fetchKey = async () => (await api.get("/api/push/key", { noRedirect: true })).public_key;
export const listSubs = async () => (await api.get("/api/push/subscriptions", { noRedirect: true })).subscriptions || [];

/** This browser's push subscription or null. */
export async function currentSub(reg) {
  try { return reg ? await reg.pushManager.getSubscription() : null; } catch { return null; }
}

export const storedId = () => { try { return Number(localStorage.getItem(ID_KEY)) || null; } catch { return null; } };
const storeId = (id) => { try { if (id) localStorage.setItem(ID_KEY, String(id)); else localStorage.removeItem(ID_KEY); } catch { /* */ } };

/**
 * Subscribe this browser. Call from a click handler with permP = Notification.requestPermission() started as the
 * very first statement of the handler (Safari only asks inside a user gesture); key and reg — fetched beforehand.
 * Returns the subscription id on the server.
 */
export async function subscribe(permP, key, reg) {
  const perm = await permP;
  if (perm !== "granted") {
    const e = new Error(perm === "denied" ? "blocked" : "Notifications were not allowed — press the button again and choose Allow.");
    e.name = perm === "denied" ? "NotAllowedError" : "PermissionDismissed";
    throw e;
  }
  let sub = await reg.pushManager.getSubscription();
  // the server key changed (relay database recreated): the old subscription is useless
  if (sub && sub.options && sub.options.applicationServerKey && bytesToB64u(sub.options.applicationServerKey) !== key) {
    try { await sub.unsubscribe(); } catch { /* */ }
    sub = null;
  }
  if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64uToBytes(key) });
  const r = await api.post("/api/push/subscribe", { subscription: sub.toJSON(), name: thisDeviceName() }, { noRedirect: true });
  storeId(r && r.id);
  return r && r.id;
}

/** Unsubscribe this browser (browser and server). */
export async function unsubscribe(reg) {
  const sub = await currentSub(reg);
  storeId(null);
  if (!sub) return;
  const endpoint = sub.endpoint;
  try { await sub.unsubscribe(); } catch (e) { console.warn("unsubscribe", e); }
  await api.post("/api/push/unsubscribe", { endpoint }, { noRedirect: true });
}

/**
 * Log out: a logged-out phone must not keep getting alarm texts. Removes this browser's subscription in the browser and
 * on the server (that needs the session — call before api/logout). Never throws, never takes longer than timeoutMs.
 */
export async function unsubscribeOnLogout(timeoutMs = 5000) {
  storeId(null);
  const env = pushEnv();
  if (!env.secure || !env.sw || !env.push) return; // the device's own http page: no push here
  const work = (async () => {
    const reg = await navigator.serviceWorker.getRegistration("./");
    const sub = await currentSub(reg);
    if (!sub) return;
    const endpoint = sub.endpoint;
    // both at once: the browser side works offline too, the server side tells the relay to stop sending
    await Promise.allSettled([
      api.post("/api/push/unsubscribe", { endpoint }, { noRedirect: true, timeout: timeoutMs }),
      sub.unsubscribe(),
    ]);
  })().catch((e) => console.warn("push unsubscribe", e));
  await Promise.race([work, new Promise((r) => setTimeout(r, timeoutMs + 500))]);
}

/** Forget the stored id (the subscription was deleted from the list). */
export const forgetId = () => storeId(null);

/** Test notification to this browser (endpoint) or to every device (null). */
export const sendTest = (endpoint) => api.post("/api/push/test", { endpoint: endpoint ?? null }, { noRedirect: true });

export const deleteSub = (id) => api.del("/api/push/subscriptions/" + encodeURIComponent(id), { noRedirect: true });
