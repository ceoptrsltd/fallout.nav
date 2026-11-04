// REST client. On 401 — redirect to the login page. Every request has a timeout (requestTimeout) with a clear error.
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

// Request timeouts: without one a half-dead tunnel leaves a request hanging until nginx gives up (60 s) — Arm spinning
// for a minute. Most answers are a small JSON from the device; a few wait for hardware, the network or the server.
export const TIMEOUT_MS = 15000;
const SLOW = [
  // [method ("*" — any), path (after the root, without the query), ms]
  ["*", /^\/api\/bluetooth\/(power|scan|devices\/)/, 120000], // pairing ≤ 60 s + connecting ≤ 40 s, adapter reset ≤ 90 s
  ["POST", /^\/api\/push\/test$/, 60000], // the server waits for the push services
  ["POST", /^\/api\/gps\/configure$/, 60000], // UBX configuration of the module
  ["POST", /^\/api\/system\/wifi$/, 60000], // nmcli joins the network
  ["DELETE", /^\/api\/tiles$/, 60000], // deletes the whole tile cache
  ["*", /^\/api\/tracking\//, 30000], // the server's journal: a month of places
  ["*", /^\/api\/push\//, 30000],
  ["GET", /^\/api\/recordings$/, 30000], // the whole list: big through the tunnel
  ["GET", /^\/api\/system\/logs$/, 30000],
  ["GET", /^\/api\/radio\/stations$/, 30000], // radio-browser on the internet
  ["DELETE", /^\/api\/recordings\/file\//, 30000],
  ["POST", /^\/api\/recordings\/delete-all$/, 60000], // every recording of both cameras off the SD card
  ["POST", /^\/api\/system\/password$/, 30000], // scrypt on the Pi
  ["POST", /^\/api\/camera\/(rear\/)?(restart|credentials)$/, 30000],
  ["POST", /^\/api\/(alarm\/hazard\/test|alarm\/test|level\/calibration\/)/, 30000],
  ["*", /^\/api\/display\/output$/, 25000], // wlr-randr (≤ 8 s per call)
  ["*", /^\/api\/settings(\/reset)?$/, 25000], // written to the SD card, the camera reloads
];

/** Timeout (ms) of a request; url — "/api/…" (a query is ignored). Full URLs get the default. */
export function requestTimeout(method, url) {
  const m = String(method || "GET").toUpperCase();
  const p = String(url || "").replace(/[?#].*$/, "");
  for (const [mm, re, ms] of SLOW) if ((mm === "*" || mm === m) && re.test(p)) return ms;
  return TIMEOUT_MS;
}

/** "No answer in 15 s — …": the device, or the server for its own paths (journal, push). */
export function timeoutText(url, ms) {
  const who = /^\/api\/(tracking|push)\//.test(String(url || "")) ? "the server" : "the device";
  return `No answer from ${who} in ${Math.round(ms / 1000)} s — the connection is slow or down`;
}

/** Text of a failed fetch (no answer at all). */
export function noConnectionText(online) {
  return online === false ? "No connection: this phone is offline" : "No connection to the device";
}

/**
 * request(method, "/api/…", body, {noRedirect, raw, contentType, init, timeout}). timeout: ms (0 — none), by default
 * requestTimeout(); a raw response (streams) is limited only until its headers come. A caller's own init.signal
 * still aborts (its AbortError is passed on as is).
 */
export async function request(method, url, body, opts = {}) {
  const path = typeof url === "string" ? url : "";
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
  const ms = opts.timeout ?? requestTimeout(method, path);
  const own = init.signal || null;
  const ctl = typeof AbortController === "function" ? new AbortController() : null;
  let timedOut = false;
  let timer = null;
  if (ctl) {
    if (own) {
      if (own.aborted) ctl.abort();
      else own.addEventListener("abort", () => ctl.abort(), { once: true });
    }
    if (ms > 0) timer = setTimeout(() => { timedOut = true; ctl.abort(); }, ms);
    init.signal = ctl.signal;
  }
  const failed = (e) => {
    if (timedOut) return Object.assign(new ApiError(0, timeoutText(path, ms)), { timeout: true });
    if (own && own.aborted) return e; // the caller's own abort
    return new ApiError(0, noConnectionText(typeof navigator !== "undefined" ? navigator.onLine : undefined));
  };
  let resp;
  let text;
  try {
    try {
      resp = await fetch(url, init);
    } catch (e) {
      throw failed(e);
    }
    if (resp.status === 401 && !opts.noRedirect) {
      goLogin();
      throw new ApiError(401, "Login required");
    }
    if (opts.raw) return resp;
    try {
      text = await resp.text();
    } catch (e) {
      throw failed(e);
    }
  } finally {
    clearTimeout(timer); // a raw response: the stream itself is not limited
  }
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
