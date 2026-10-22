// REST client. On 401 — redirect to the login page.
//
// Address prefix (docs/contracts/remote.md): on the device the app lives at "/", through the server at
// "/fnadmin/". Every app URL is built from ROOT; request() resolves "/api/…" against it centrally.
// Same functions as web/display/map/root.js (the admin does not depend on display modules).

/** "/admin/…" → "/", "/fnadmin/admin/…" → "/fnadmin/". */
export function appRoot(pathname) {
  const r = String(pathname || "/").replace(/\/(admin|display)(\/.*)?$/, "");
  return r.endsWith("/") ? r : r + "/";
}

export const ROOT = typeof location !== "undefined" ? appRoot(location.pathname) : "/";
/** Opened through the server (tunnel), not from the device itself. */
export const REMOTE = ROOT !== "/";

/** "api/x" or "/api/x" → URL under the app root; full URLs (https://…) are returned as is. */
export function appUrl(p, root = ROOT) {
  const s = String(p);
  if (/^[a-z][a-z0-9+.-]*:/i.test(s) || s.startsWith("//")) return s;
  return root + s.replace(/^\/+/, "");
}

export const LOGIN_URL = appUrl("admin/login.html");

/** Login "next" target: a path relative to ROOT inside admin/ (no open redirect) → full URL. */
export function safeNext(n, root = ROOT) {
  const p = String(n || "").replace(/^\//, ""); // the device middleware sends "/admin/…"
  const ok = /^admin\/[\w\-./#?=&%]*$/.test(p) && !p.includes("//") && !p.includes("..") && !p.includes("login.html");
  return root + (ok ? p : "admin/");
}

export class ApiError extends Error {
  constructor(status, detail, raw) {
    super(typeof detail === "string" ? detail : "Error " + status);
    this.status = status;
    this.detail = detail; // string or a list of pydantic errors [{loc,msg}]
    this.raw = raw;
  }
  /** Per-field errors: {"camera.bitrate_kbps": "text"} + a general message for the rest. */
  fieldErrors() {
    const out = {};
    let general = null;
    if (Array.isArray(this.detail)) {
      for (const e of this.detail) {
        const loc = (e.loc || []).filter((x) => x !== "body" && x !== "__root__");
        const key = loc.filter((x) => typeof x === "string").join(".");
        const msg = e.msg || String(e);
        if (key) out[key] = (out[key] ? out[key] + "; " : "") + msg;
        else general = (general ? general + "; " : "") + msg;
      }
    } else if (this.detail && typeof this.detail === "object") {
      for (const [k, v] of Object.entries(this.detail)) out[k] = String(v);
    } else {
      general = String(this.detail || this.message);
    }
    return { fields: out, general };
  }
}

let redirecting = false;
export function goLogin() {
  if (redirecting) return;
  redirecting = true;
  const path = location.pathname.startsWith(ROOT) ? location.pathname.slice(ROOT.length) : "admin/";
  location.href = LOGIN_URL + "?next=" + encodeURIComponent(path + location.hash);
}

export async function request(method, url, body, opts = {}) {
  if (typeof url === "string" && url.startsWith("/") && !url.startsWith("//")) url = appUrl(url);
  const init = { method, credentials: "same-origin", headers: {}, ...opts.init };
  if (body !== undefined && body !== null) {
    if (typeof body === "string") {
      init.body = body;
      init.headers["Content-Type"] = opts.contentType || "text/plain";
    } else {
      init.body = JSON.stringify(body);
      init.headers["Content-Type"] = "application/json";
    }
  }
  let resp;
  try {
    resp = await fetch(url, init);
  } catch (e) {
    throw new ApiError(0, "No connection to the device");
  }
  if (resp.status === 401 && !opts.noRedirect) {
    goLogin();
    throw new ApiError(401, "Login required");
  }
  if (opts.raw) return resp;
  const text = await resp.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = text; }
  }
  if (!resp.ok) {
    const detail = data && typeof data === "object" && "detail" in data ? data.detail : data;
    throw new ApiError(resp.status, detail || resp.statusText, data);
  }
  return data;
}

export const api = {
  get: (url, opts) => request("GET", url, undefined, opts),
  post: (url, body, opts) => request("POST", url, body === undefined ? {} : body, opts),
  patch: (url, body, opts) => request("PATCH", url, body, opts),
  put: (url, body, opts) => request("PUT", url, body, opts),
  del: (url, opts) => request("DELETE", url, undefined, opts),
};

export const encPath = (p) => p.split("/").map(encodeURIComponent).join("/");

/** Error text for the user. */
export function errText(e) {
  if (e instanceof ApiError) {
    const { fields, general } = e.fieldErrors();
    const parts = Object.entries(fields).map(([k, v]) => `${k}: ${v}`);
    if (general) parts.unshift(general);
    return parts.join("; ") || e.message;
  }
  return e && e.message ? e.message : String(e);
}
