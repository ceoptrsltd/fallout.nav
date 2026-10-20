// Small helpers: DOM creation, icons, formatting.

/** h("div.cls#id", {onclick, class, dataset, style, ...}, ...children) */
export function h(tag, attrs, ...children) {
  let cls = [];
  let id = null;
  const m = tag.match(/^([a-z0-9]*)((?:[.#][\w-]+)*)$/i);
  let name = tag;
  if (m) {
    name = m[1] || "div";
    for (const part of m[2].match(/[.#][\w-]+/g) || []) {
      if (part[0] === ".") cls.push(part.slice(1));
      else id = part.slice(1);
    }
  }
  const svgNs = ["svg", "path", "circle", "line", "rect", "g", "text", "polygon", "polyline", "ellipse"];
  const el = svgNs.includes(name)
    ? document.createElementNS("http://www.w3.org/2000/svg", name)
    : document.createElement(name);
  if (cls.length) el.setAttribute("class", cls.join(" "));
  if (id) el.id = id;
  if (attrs && (typeof attrs !== "object" || attrs instanceof Node || Array.isArray(attrs))) {
    children.unshift(attrs);
    attrs = null;
  }
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.setAttribute("class", (el.getAttribute("class") ? el.getAttribute("class") + " " : "") + v);
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "value" || k === "checked" || k === "disabled" || k === "selected") el[k] = v;
    else if (k === "html") el.innerHTML = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

const ICONS = {
  dash: "M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z",
  cam: "M17 10.5V7a1 1 0 0 0-1-1H4a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-3.5l4 4v-11l-4 4z",
  rec: "M4 4h16v16H4V4zm4 4v8h8V8H8z",
  nav: "M12 2 4.5 20.3 12 17l7.5 3.3L12 2z",
  compass: "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm3.8 5.7-2.2 5.9-5.9 2.2 2.2-5.9 5.9-2.2z",
  gps: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zm8.9 3A9 9 0 0 0 13 3.1V1h-2v2.1A9 9 0 0 0 3.1 11H1v2h2.1A9 9 0 0 0 11 20.9V23h2v-2.1a9 9 0 0 0 7.9-7.9H23v-2h-2.1zM12 19a7 7 0 1 1 0-14 7 7 0 0 1 0 14z",
  sys: "M19.4 13a7.5 7.5 0 0 0 0-2l2.1-1.6-2-3.5-2.5 1a7.4 7.4 0 0 0-1.7-1L15 3h-4l-.4 2.9a7.4 7.4 0 0 0-1.7 1l-2.5-1-2 3.5L6.6 11a7.5 7.5 0 0 0 0 2l-2.1 1.6 2 3.5 2.5-1c.5.4 1.1.7 1.7 1L11 21h4l.4-2.9c.6-.3 1.2-.6 1.7-1l2.5 1 2-3.5L19.4 13zM13 15.5a3.5 3.5 0 1 1 0-7 3.5 3.5 0 0 1 0 7z",
  screen: "M3 4h18a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1h-7v2h3v2H7v-2h3v-2H3a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1zm1 2v9h16V6H4z",
  lock: "M18 8h-1V6a5 5 0 0 0-10 0v2H6a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V10a2 2 0 0 0-2-2zM9 6a3 3 0 0 1 6 0v2H9V6z",
  unlock: "M18 8h-1V6a5 5 0 0 0-9.6-2l1.9.7A3 3 0 0 1 15 6v2H6a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V10a2 2 0 0 0-2-2z",
  play: "M8 5v14l11-7L8 5z",
  dl: "M5 20h14v-2H5v2zM19 9h-4V3H9v6H5l7 7 7-7z",
  trash: "M6 19a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z",
  x: "M19 6.4 17.6 5 12 10.6 6.4 5 5 6.4 10.6 12 5 17.6 6.4 19 12 13.4 17.6 19 19 17.6 13.4 12 19 6.4z",
  warn: "M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z",
  info: "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z",
  out: "M17 7l-1.4 1.4L18.2 11H8v2h10.2l-2.6 2.6L17 17l5-5-5-5zM4 5h8V3H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h8v-2H4V5z",
  refresh: "M17.6 6.4A8 8 0 1 0 19.9 14h-2.1A6 6 0 1 1 12 6c1.7 0 3.1.7 4.2 1.8L13 11h7V4l-2.4 2.4z",
  plus: "M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z",
  wifi: "M12 18.5a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3zM2 8.8l1.4 1.4a12 12 0 0 1 17.2 0L22 8.8a14 14 0 0 0-20 0zm4.2 4.2 1.4 1.4a6 6 0 0 1 8.8 0l1.4-1.4a8 8 0 0 0-11.6 0zm-2.1-2.1L5.5 12.3a9 9 0 0 1 13 0l1.4-1.4a11 11 0 0 0-15.8 0z",
};

export function icon(name, size) {
  const s = h("svg", { viewBox: "0 0 24 24", fill: "currentColor", "aria-hidden": "true" });
  if (size) { s.style.width = size + "px"; s.style.height = size + "px"; }
  s.append(h("path", { d: ICONS[name] || ICONS.info }));
  return s;
}

// ───── formatting ─────
export const isNum = (v) => typeof v === "number" && Number.isFinite(v);
export const fmt = (v, d = 1, dash = "—") => (isNum(v) ? v.toFixed(d) : dash);
export const kmh = (mps) => (isNum(mps) ? mps * 3.6 : null);

export function fmtBytes(n) {
  if (!isNum(n)) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n.toFixed(0) : n.toFixed(n >= 100 ? 0 : n >= 10 ? 1 : 2)) + " " + u[i];
}

export function fmtDur(s) {
  if (!isNum(s)) return "—";
  s = Math.round(s);
  const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
  const p = (x) => String(x).padStart(2, "0");
  return hh ? `${hh}:${p(mm)}:${p(ss)}` : `${mm}:${p(ss)}`;
}

export function fmtUptime(s) {
  if (!isNum(s)) return "—";
  const d = Math.floor(s / 86400), hh = Math.floor((s % 86400) / 3600), mm = Math.floor((s % 3600) / 60);
  return (d ? d + " d " : "") + hh + " h " + mm + " min";
}

export function compassWord(deg) {
  if (!isNum(deg)) return "—";
  const w = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
  return w[Math.round(deg / 22.5) % 16];
}

export function deepEqual(a, b) {
  if (a === b) return true;
  if (typeof a !== typeof b || a === null || b === null || typeof a !== "object") return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  const ka = Object.keys(a), kb = Object.keys(b);
  if (ka.length !== kb.length) return false;
  return ka.every((k) => deepEqual(a[k], b[k]));
}

export const clone = (o) => (o === undefined ? undefined : JSON.parse(JSON.stringify(o)));

export function getPath(obj, path) {
  return path.split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj);
}

export function setPath(obj, path, value) {
  const keys = path.split(".");
  let o = obj;
  for (let i = 0; i < keys.length - 1; i++) {
    if (typeof o[keys[i]] !== "object" || o[keys[i]] === null) o[keys[i]] = {};
    o = o[keys[i]];
  }
  o[keys[keys.length - 1]] = value;
  return obj;
}
