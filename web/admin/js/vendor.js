// Lazy loading of vendored libraries (web/admin/vendor/): classic scripts that set a window global.
import { appUrl } from "./api.js";

const scripts = new Map(); // src -> Promise

/** Load admin/vendor/<path> once; resolves with window[globalName]. */
export function loadVendorScript(path, globalName) {
  if (window[globalName]) return Promise.resolve(window[globalName]);
  const src = appUrl("admin/vendor/" + path);
  if (!scripts.has(src)) {
    scripts.set(src, new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = src;
      s.onload = () => (window[globalName] ? resolve(window[globalName]) : reject(new Error(path + " failed to initialize")));
      s.onerror = () => { scripts.delete(src); s.remove(); reject(new Error("could not load " + path)); };
      document.head.append(s);
    }));
  }
  return scripts.get(src);
}

const sheets = new Map(); // path -> Promise

/** Add <link rel=stylesheet> for admin/vendor/<path> once; resolves when it is loaded (or failed). */
export function loadVendorCss(path) {
  if (!sheets.has(path)) {
    sheets.set(path, new Promise((resolve) => {
      const l = document.createElement("link");
      l.rel = "stylesheet";
      l.href = appUrl("admin/vendor/" + path);
      l.dataset.vendor = path;
      l.onload = l.onerror = () => resolve();
      // insert before app.css so that the admin theme overrides the library defaults
      const own = document.querySelector('link[rel="stylesheet"]');
      if (own) own.before(l); else document.head.append(l);
    }));
  }
  return sheets.get(path);
}

export const loadMpegts = () => loadVendorScript("mpegts.js", "mpegts");
