// REST client. On 401 — redirect to the login page.

export const LOGIN_URL = "/admin/login.html";

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
  const next = encodeURIComponent(location.pathname + location.hash);
  location.href = LOGIN_URL + "?next=" + next;
}

export async function request(method, url, body, opts = {}) {
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
