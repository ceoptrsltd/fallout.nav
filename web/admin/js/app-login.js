// The installed app asks for the password the first time it opens on a device, even when the browser is already
// signed in (on Android the app shares the browser's cookies). A mark in localStorage, set only by a sign-in made
// inside the installed app and removed by Log out, says "the password was entered in the app here". The real
// protection stays the server's session: this only makes the app's first start explicit.
export const APP_LOGIN_KEY = "fnav.appLogin";

/** Running as the installed app (Home Screen / app list), not in a browser tab. */
export function isStandalone(w = globalThis) {
  try {
    if (w.navigator && w.navigator.standalone === true) return true; // iOS Home Screen
    const mq = (m) => !!(w.matchMedia && w.matchMedia(`(display-mode: ${m})`).matches);
    return mq("standalone") || mq("fullscreen");
  } catch {
    return false;
  }
}

function storage(w) {
  try { return w.localStorage || null; } catch { return null; }
}

/** The installed app has not been signed in to on this device yet. False when the mark cannot be kept (no
 *  storage, or writes refused): asking then would never end. */
export function needsAppLogin(w = globalThis) {
  if (!isStandalone(w)) return false;
  const s = storage(w);
  if (!s) return false;
  try {
    if (s.getItem(APP_LOGIN_KEY) === "1") return false;
    s.setItem(APP_LOGIN_KEY + ".probe", "1");
    s.removeItem(APP_LOGIN_KEY + ".probe");
    return true;
  } catch {
    return false;
  }
}

/** After a successful sign-in: inside the installed app, remember it. */
export function markAppLogin(w = globalThis) {
  if (!isStandalone(w)) return;
  try { storage(w)?.setItem(APP_LOGIN_KEY, "1"); } catch { /* */ }
}

/** Log out: the app asks for the password again next time. */
export function clearAppLogin(w = globalThis) {
  try { storage(w)?.removeItem(APP_LOGIN_KEY); } catch { /* */ }
}
