// "Install app": which way this browser installs the admin, and the steps to show. Pure (no DOM).
// Tests: tests/admin/install.test.mjs.
import { deviceName, isIos } from "./alarm-fmt.js";

/** Facts → {os, browser, ios, android, mobile}. os/browser as in alarm-fmt deviceName(). */
export function platformOf({ ua = "", platform = "", touch = 0 } = {}) {
  const [os, browser] = deviceName({ ua, platform, touch }).split(" · ");
  const ios = isIos({ ua, platform, touch });
  const android = os === "Android";
  return { os, browser, ios, android, mobile: ios || android };
}

/**
 * How to install here:
 *   "installed"  running as the installed app (standalone) — hide the entry;
 *   "prompt"     the browser handed us its install prompt (beforeinstallprompt) — one tap;
 *   "ios"        iPhone / iPad: Share → Add to Home Screen;
 *   "android"    Android without a prompt (dismissed, not offered yet, or another browser): the browser menu;
 *   "desktop"    a computer without a prompt: the address bar icon or the browser menu.
 */
export function installMode({ standalone = false, hasPrompt = false, ios = false, android = false } = {}) {
  if (standalone) return "installed";
  if (hasPrompt) return "prompt";
  if (ios) return "ios";
  if (android) return "android";
  return "desktop";
}

// steps: arrays of strings and {icon} / {b} parts (the page draws icons and bold text)
const SHARE = { icon: "share" };
const DOTS = { icon: "dots" };
const b = (t) => ({ b: t });

/** Steps for a mode and browser: {title, steps: [[part…]…], note}. */
export function installSteps(mode, browser, { secure = true } = {}) {
  let title = "Install fallout.nav";
  let steps = [];
  let note = "";
  if (mode === "ios") {
    title = "Add to Home Screen";
    if (browser === "Safari") {
      steps = [
        ["Tap ", SHARE, " ", b("Share"), " in Safari's toolbar."],
        ["Scroll down and tap ", b("Add to Home Screen"), ", then ", b("Add"), "."],
        ["Open ", b("fallout.nav"), " from the Home Screen and sign in once — the app keeps its own login."],
      ];
    } else {
      steps = [
        ["Tap ", SHARE, " ", b("Share"), " (next to the address bar or in the menu)."],
        ["Tap ", b("Add to Home Screen"), ", then ", b("Add"), "."],
        ["Not there? Open this page in ", b("Safari"), " and add it from there."],
      ];
    }
    note = "On iPhone alarm notifications work only in the app added to the Home Screen.";
  } else if (mode === "android") {
    if (browser === "Samsung Internet") {
      steps = [["Tap the menu ", b("≡"), " at the bottom."], ["Tap ", b("Add page to"), " → ", b("Home screen"), "."]];
    } else if (browser === "Firefox") {
      steps = [["Tap the menu ", DOTS, "."], ["Tap ", b("Add app to Home screen"), " (or ", b("Install"), ")."]];
    } else if (browser === "Edge") {
      steps = [["Tap the menu ", b("⋯"), " at the bottom."], ["Tap ", b("Add to phone"), " (or ", b("Install app"), ")."]];
    } else {
      steps = [
        ["Tap the menu ", DOTS, " at the top right."],
        ["Tap ", b("Install app"), " or ", b("Add to Home screen"), " → ", b("Install"), "."],
      ];
    }
  } else if (mode === "desktop") {
    if (browser === "Edge") {
      steps = [["Click the install icon at the right of the address bar,"], ["or the menu ", b("⋯"), " → ", b("Apps"), " → ", b("Install this site as an app"), "."]];
    } else if (browser === "Safari") {
      steps = [["In the menu bar choose ", b("File"), " → ", b("Add to Dock…"), "."]];
    } else if (browser === "Firefox") {
      steps = [["Firefox does not install web apps on a computer — open this page in ", b("Chrome"), " or ", b("Edge"), "."]];
    } else {
      steps = [["Click the install icon at the right of the address bar,"], ["or the menu ", DOTS, " → ", b("Cast, save and share"), " → ", b("Install page as app…"), "."]];
    }
  } else if (mode === "prompt") {
    steps = [["Tap ", b("Install"), " in the browser's dialog."]];
  }
  if (!secure && mode !== "installed") {
    note = (note ? note + " " : "")
      + "This is the device's own http address: the browser can add a shortcut here, but the full app (and notifications) "
      + "needs the secure remote address https://<your server>/fnadmin/ — install it from there.";
  }
  return { title, steps, note };
}

/** Plain text of a step (for titles and tests). */
export function stepText(step) {
  return (step || []).map((p) => (typeof p === "string" ? p : p.b ? p.b : p.icon === "share" ? "[Share]" : p.icon === "dots" ? "⋮" : "")).join("");
}
