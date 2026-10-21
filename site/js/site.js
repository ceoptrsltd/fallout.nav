// fallout.nav project page: live CRT screens in photos, demo console, diagrams, gallery.
// Plain ES module, no dependencies.
import { SCREENS } from "./screens.js";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const SVGNS = "http://www.w3.org/2000/svg";
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

function svgEl(tag, attrs = {}, text) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (text != null) el.textContent = text;
  return el;
}

/* ───────────── perspective: 768×576 raster -> quad in the photo ───────────── */

// Projective map of the unit square onto quad p (TL, TR, BR, BL), returned as a CSS matrix3d for a w×h element.
function quadMatrix(w, h, p) {
  const [[x0, y0], [x1, y1], [x2, y2], [x3, y3]] = p;
  const dx1 = x1 - x2, dx2 = x3 - x2, dx3 = x0 - x1 + x2 - x3;
  const dy1 = y1 - y2, dy2 = y3 - y2, dy3 = y0 - y1 + y2 - y3;
  let a, b, c, d, e, f, g, hh;
  if (Math.abs(dx3) < 1e-9 && Math.abs(dy3) < 1e-9) {
    a = x1 - x0; b = x3 - x0; c = x0; d = y1 - y0; e = y3 - y0; f = y0; g = 0; hh = 0;
  } else {
    const den = dx1 * dy2 - dx2 * dy1;
    g = (dx3 * dy2 - dx2 * dy3) / den;
    hh = (dx1 * dy3 - dx3 * dy1) / den;
    a = x1 - x0 + g * x1; b = x3 - x0 + hh * x3; c = x0;
    d = y1 - y0 + g * y1; e = y3 - y0 + hh * y3; f = y0;
  }
  const m = [a / w, d / w, 0, g / w, b / h, e / h, 0, hh / h, 0, 0, 1, 0, c, f, 0, 1];
  return `matrix3d(${m.map((v) => +v.toFixed(8)).join(",")})`;
}

/* ───────────── live CRTs ───────────── */

const frames = new Map(); // contentWindow -> {crt, iframe, ready}
let heroWin = null;

function setupCrt(crt) {
  const spec = SCREENS[crt.dataset.screen];
  if (!spec) return;
  const screen = $(".crt-screen", crt);
  const power = $(".crt-power", crt);
  const iframe = $("iframe", crt);
  const glasses = $$(".crt-glass", crt);
  const lines = +crt.dataset.lines || 240;
  screen.style.clipPath = spec.clip;
  screen.style.webkitClipPath = spec.clip;

  const layout = () => {
    const W = crt.clientWidth, H = crt.clientHeight;
    if (!W || !H) return;
    const q = spec.quad.map(([x, y]) => [x * W, y * H]);
    iframe.style.transform = quadMatrix(768, 576, q);
    const xs = q.map((p) => p[0]), ys = q.map((p) => p[1]);
    const L = Math.min(...xs), T = Math.min(...ys), R = Math.max(...xs), B = Math.max(...ys);
    // glass effects cover the whole glass (a little beyond the raster), the clip-path trims them
    const pad = (R - L) * 0.03;
    for (const g of glasses) Object.assign(g.style, { left: `${L - pad}px`, top: `${T - pad}px`, width: `${R - L + 2 * pad}px`, height: `${B - T + 2 * pad}px` });
    const sh = B - T;
    const sl = Math.max(2, Math.round(sh / lines));
    crt.style.setProperty("--sl", `${sl}px`);
    crt.style.setProperty("--sla", sh < 160 ? "0.16" : sl >= 3 ? "0.34" : "0.24");
    crt.style.setProperty("--bloom", `${Math.max(1.5, (R - L) / 130).toFixed(1)}px`);
    crt.style.setProperty("--gw", `${Math.round(R - L)}px`);
    power.style.transformOrigin = `${((L + R) / 2).toFixed(1)}px ${((T + B) / 2).toFixed(1)}px`;
  };
  new ResizeObserver(layout).observe(crt);
  layout();
  crt.classList.add("ready");

  const powerOn = () => {
    if (crt.classList.contains("on")) return;
    crt.classList.add("on");
  };
  const start = () => {
    if (iframe.src) return;
    iframe.src = iframe.dataset.src;
    frames.set(iframe.contentWindow, { crt, iframe, ready: false, powerOn });
    if (crt.dataset.screen === "monitor") heroWin = iframe.contentWindow;
    // the demo announces itself; if it never does (old build), still switch the tube on after load
    iframe.addEventListener("load", () => setTimeout(powerOn, 1800), { once: true });
  };
  if (crt.dataset.lazy) {
    const io = new IntersectionObserver((es) => {
      if (es.some((e) => e.isIntersecting)) { io.disconnect(); start(); }
    }, { rootMargin: "400px 0px" });
    io.observe(crt);
  } else {
    start();
  }
}

/* ───────────── demo console ───────────── */

const ctl = { play: true, speedOverride: null, orientation: "heading_up", compassStyle: "v1", forceMode: "auto", layer: "imagery" };
const touched = new Set(); // controls the user changed (state echoes must not overwrite them)

function send(patch, win) {
  const msg = { type: "fnav-demo", ...patch };
  const targets = win ? [win] : [...frames.keys()];
  for (const w of targets) {
    try { w.postMessage(msg, "*"); } catch { /* frame gone */ }
  }
}

function setPressed(group, v) {
  for (const b of $$("button", group)) b.setAttribute("aria-pressed", String(b.dataset.v === v));
}

function setupConsole() {
  // segmented switches
  for (const group of $$(".seg[data-key]")) {
    const key = group.dataset.key;
    group.addEventListener("click", (ev) => {
      const b = ev.target.closest("button[data-v]");
      if (!b) return;
      ctl[key] = b.dataset.v;
      touched.add(key);
      setPressed(group, b.dataset.v);
      send({ [key]: b.dataset.v });
    });
  }
  // play / pause
  const play = $("#play");
  play.addEventListener("click", () => {
    ctl.play = !ctl.play;
    play.setAttribute("aria-pressed", String(ctl.play));
    play.setAttribute("aria-label", ctl.play ? "Pause the drive" : "Resume the drive");
    send({ play: ctl.play });
  });
  // speed
  const range = $("#speed"), auto = $("#speed-auto"), out = $("#speed-out");
  const paintRange = () => range.style.setProperty("--p", `${(range.value / range.max) * 100}%`);
  const setAuto = (on) => {
    auto.setAttribute("aria-pressed", String(on));
    range.classList.toggle("auto", on);
    out.textContent = on ? "Auto" : `${range.value} km/h`;
  };
  range.addEventListener("input", () => {
    paintRange();
    setAuto(false);
    ctl.speedOverride = range.value / 3.6;
    send({ speedOverride: ctl.speedOverride });
  });
  auto.addEventListener("click", () => {
    const on = auto.getAttribute("aria-pressed") !== "true";
    setAuto(on);
    ctl.speedOverride = on ? null : range.value / 3.6;
    send({ speedOverride: ctl.speedOverride });
  });
  paintRange();
  window.__fnavSpeedRange = { range, paintRange, isAuto: () => auto.getAttribute("aria-pressed") === "true" };

  // dial ticks
  const st = $("#speed-ticks");
  for (let v = 0; v <= 140; v += 10) {
    const a = ((-120 + (v / 140) * 240) * Math.PI) / 180;
    const major = v % 20 === 0;
    const r1 = 90, r2 = major ? 78 : 84;
    st.append(svgEl("line", { x1: 100 + r1 * Math.sin(a), y1: 100 - r1 * Math.cos(a), x2: 100 + r2 * Math.sin(a), y2: 100 - r2 * Math.cos(a), stroke: major ? "#c7cbb4" : "#5b6150", "stroke-width": major ? 2.4 : 1.4 }));
    if (major) st.append(svgEl("text", { x: 100 + 64 * Math.sin(a), y: 100 - 64 * Math.cos(a) + 4, "text-anchor": "middle", "font-size": 11 }, v));
  }
  const rt = $("#rose-ticks");
  for (let d = 0; d < 360; d += 10) {
    const a = (d * Math.PI) / 180, major = d % 30 === 0;
    const r1 = 68, r2 = major ? 58 : 63;
    rt.append(svgEl("line", { x1: 100 + r1 * Math.sin(a), y1: 92 - r1 * Math.cos(a), x2: 100 + r2 * Math.sin(a), y2: 92 - r2 * Math.cos(a), stroke: major ? "#c7cbb4" : "#4d5345", "stroke-width": major ? 2 : 1.2 }));
  }
}

const RHUMBS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
let roseAngle = null;

function showState(s) {
  const kmh = Math.max(0, (s.speedMps || 0) * 3.6);
  $("#speed-val").textContent = Math.round(kmh);
  const frac = Math.min(1, kmh / 140);
  $("#speed-needle").style.transform = `rotate(${-120 + frac * 240}deg)`;
  $("#speed-arc").setAttribute("stroke-dasharray", `${(frac * 100).toFixed(2)} 100`);
  const sr = window.__fnavSpeedRange;
  if (sr && sr.isAuto() && document.activeElement !== sr.range) {
    sr.range.value = Math.min(130, Math.round(kmh));
    sr.paintRange();
  }

  if (Number.isFinite(s.headingDeg)) {
    const h = ((s.headingDeg % 360) + 360) % 360;
    $("#hdg-val").textContent = String(Math.round(h) % 360).padStart(3, "0");
    $("#hdg-rhumb").textContent = `° ${RHUMBS[Math.round(h / 22.5) % 16]}`;
    // unwrap so the card takes the short way round
    const target = -h;
    if (roseAngle == null) roseAngle = target;
    else roseAngle += ((((target - roseAngle) % 360) + 540) % 360) - 180;
    $("#rose").style.transform = `rotate(${roseAngle}deg)`;
  }
  const cam = s.mode === "camera";
  $("#lamp-map").classList.toggle("on", s.mode === "map");
  $("#lamp-cam").classList.toggle("on", cam);
  if (Number.isFinite(s.zoom)) {
    $("#zoom-val").textContent = s.zoom.toFixed(1);
    $("#zoom-bar").style.width = `${Math.max(0, Math.min(1, (s.zoom - 14) / 6)) * 100}%`;
  }
  const hu = (s.orientation || ctl.orientation) === "heading_up";
  $("#lamp-hu").classList.toggle("on", hu);
  $("#lamp-nu").classList.toggle("on", !hu);
  if (Number.isFinite(s.lat) && Number.isFinite(s.lon)) {
    $("#coords").textContent = `${s.lat.toFixed(5)}  ${s.lon.toFixed(5)}`;
  }
  // reflect the demo's own settings until the user touches a switch
  for (const [key, val] of [["orientation", s.orientation], ["compassStyle", s.compassStyle]]) {
    if (val && !touched.has(key) && ctl[key] !== val) {
      ctl[key] = val;
      setPressed($(`.seg[data-key="${key}"]`), val);
    }
  }
}

function setupMessaging() {
  window.addEventListener("message", (ev) => {
    const d = ev.data;
    if (!d || typeof d !== "object" || typeof d.type !== "string" || !d.type.startsWith("fnav-demo")) return;
    const fr = frames.get(ev.source);
    if (!fr) return;
    if (d.type === "fnav-demo-ready") {
      fr.ready = true;
      fr.powerOn();
      // bring a late-loading screen in line with the console
      const patch = { play: ctl.play, speedOverride: ctl.speedOverride, forceMode: ctl.forceMode, layer: ctl.layer };
      for (const k of ["orientation", "compassStyle"]) if (touched.has(k)) patch[k] = ctl[k];
      send(patch, ev.source);
      if (ev.source === heroWin) {
        const ls = $("#link-state");
        ls.classList.add("ok");
        ls.textContent = "demo link: live";
      }
    } else if (d.type === "fnav-demo-state" && ev.source === heroWin) {
      showState(d);
    }
  });
}

/* ───────────── diagrams generated from data ───────────── */

function buildGpsDiagram() {
  const gh = $("#gps-gh"), hdr = $("#gps-hdr"), wires = $("#gps-wires");
  if (!gh) return;
  // [GH pin, module signal, Pi header pin, colour, short note]
  const W = [
    [1, "VCC 5V", 2, "#ff5b3d"],
    [2, "RX", 8, "#ffb43a"],
    [3, "TX", 10, "#ffe066"],
    [4, "SCL", 5, "#74ff98"],
    [5, "SDA", 3, "#3fd3c6"],
    [6, "SAFETY SW", 16, "#6ea8ff"],
    [7, "SW LED", 18, "#c48bff"],
    [8, "3V3", 1, "#ff8a3d"],
    [9, "BUZZER−", 12, "#9aa088"],
    [10, "GND", 6, "#c9cbbd"],
  ];
  const HDR = { 1: "3V3", 2: "5V", 3: "SDA1 · GPIO2", 4: "5V", 5: "SCL1 · GPIO3", 6: "GND", 7: "GPIO4", 8: "TXD · GPIO14", 9: "GND", 10: "RXD · GPIO15", 11: "GPIO17", 12: "GPIO18", 13: "GPIO27", 14: "GND", 15: "GPIO22", 16: "GPIO23", 17: "3V3", 18: "GPIO24" };
  const ghY = (i) => 58 + (i - 1) * 34.5;
  const rowY = (p) => 58 + Math.floor((p - 1) / 2) * 38.5;
  const pinX = (p) => (p % 2 ? 458 : 492);
  const used = new Map(W.map((w) => [w[2], w]));
  for (const [n, sig, p, col] of W) {
    const y1 = ghY(n), y2 = rowY(p);
    let d;
    if (n === 9) d = `M176 ${y1} C 230 ${y1}, 236 296, 264 296 M308 296 C 360 296, 380 ${y2}, 436 ${y2}`;
    else d = `M176 ${y1} C 300 ${y1}, 320 ${y2}, 436 ${y2}`;
    wires.append(svgEl("path", { d, class: "wire", stroke: col, "stroke-dasharray": n === 9 ? "7 5" : "none", opacity: 0.95 }));
    gh.append(svgEl("text", { x: 34, y: y1 + 4, fill: "#6c715d" }, String(n)));
    gh.append(svgEl("text", { x: 58, y: y1 + 4, fill: "#dadccb" }, sig));
    gh.append(svgEl("rect", { x: 162, y: y1 - 7, width: 14, height: 14, rx: 2, fill: col }));
  }
  for (let p = 1; p <= 18; p++) {
    const y = rowY(p), w = used.get(p);
    hdr.append(svgEl("circle", { cx: pinX(p), cy: y, r: 8, fill: w ? w[3] : "#2a2e24", stroke: w ? "#0b0d09" : "#3a4130", "stroke-width": 1.5 }));
    if (p % 2) hdr.append(svgEl("text", { x: pinX(p), y: y + 3.5, "text-anchor": "middle", "font-size": 9, fill: w ? "#0b0d09" : "#6c715d" }, String(p)));
    else hdr.append(svgEl("text", { x: pinX(p), y: y + 3.5, "text-anchor": "middle", "font-size": 9, fill: w ? "#0b0d09" : "#6c715d" }, String(p)));
  }
  for (let r = 0; r < 9; r++) {
    const odd = 2 * r + 1, even = odd + 1, y = 58 + r * 38.5;
    const t = svgEl("text", { x: 526, y: y - 3, "font-size": 11 });
    const a = svgEl("tspan", { fill: used.has(odd) ? used.get(odd)[3] : "#5b6150" }, `${odd} ${HDR[odd]}`);
    t.append(a);
    hdr.append(t);
    hdr.append(svgEl("text", { x: 526, y: y + 11, "font-size": 11, fill: used.has(even) ? used.get(even)[3] : "#5b6150" }, `${even} ${HDR[even]}`));
  }
}

function buildCrew() {
  const g = $("#crew-agents");
  if (!g) return;
  const A = [
    ["A", "backend", "fnav/core"],
    ["B", "map", "display/map"],
    ["C", "HUD", "display/ui"],
    ["D", "web UI", "web/admin"],
    ["E", "camera", "fnav/camera"],
    ["F", "sensors", "fnav/sensors"],
    ["G", "Pi system", "pi/"],
  ];
  const w = 84, gap = 9.3, x0 = 30, y = 182;
  A.forEach(([k, t, s], i) => {
    const x = x0 + i * (w + gap);
    const cx = x + w / 2;
    g.append(svgEl("path", { class: "c live", d: `M${cx} 142 L${cx} ${y}`, style: `animation-delay:${-i * 0.2}s` }));
    const grp = svgEl("g", { class: "ag" });
    grp.append(svgEl("rect", { x, y, width: w, height: 88, rx: 8, "stroke-width": 1.5 }));
    grp.append(svgEl("text", { class: "k", x: cx, y: y + 30, "text-anchor": "middle" }, k));
    grp.append(svgEl("text", { class: "t", x: cx, y: y + 54, "text-anchor": "middle" }, t));
    grp.append(svgEl("text", { class: "s", x: cx, y: y + 73, "text-anchor": "middle", style: "font-family: var(--f-mono); font-size: 9.5px" }, s));
    g.append(grp);
  });
}

/* ───────────── gallery + lightbox ───────────── */

function setupGallery() {
  const grid = $("#grid");
  const tabs = $$(".tabs button");
  for (const t of tabs) {
    t.addEventListener("click", () => {
      for (const o of tabs) o.setAttribute("aria-pressed", String(o === t));
      const f = t.dataset.f;
      for (const s of $$(".shot", grid)) {
        s.hidden = !(f === "all" || s.dataset.c === f);
        if (!s.hidden) s.classList.add("in");
      }
    });
  }

  const dlg = $("#lightbox"), img = $("#lb-img"), cap = $("#lb-cap");
  let list = [], idx = 0;
  const show = (i) => {
    idx = (i + list.length) % list.length;
    const it = list[idx];
    img.src = it.src;
    img.alt = it.alt;
    cap.innerHTML = "";
    if (it.title) cap.append(Object.assign(document.createElement("b"), { textContent: it.title }));
    cap.append(document.createTextNode(it.text));
    $("#lb-prev").hidden = $("#lb-next").hidden = list.length < 2;
  };
  const open = (items, i) => {
    list = items;
    show(i);
    if (typeof dlg.showModal === "function") dlg.showModal();
    else dlg.setAttribute("open", "");
  };
  const fromShot = (s) => {
    const b = $("figcaption b", s);
    const text = $("figcaption", s).textContent.slice(b ? b.textContent.length : 0);
    return { src: $("img", s).dataset.full, title: b ? b.textContent : "", text, alt: (b ? b.textContent + ": " : "") + text };
  };
  grid.addEventListener("click", (ev) => {
    const s = ev.target.closest(".shot");
    if (!s) return;
    const vis = $$(".shot", grid).filter((x) => !x.hidden);
    open(vis.map(fromShot), vis.indexOf(s));
  });
  for (const f of $$(".story figure[data-full]")) {
    f.tabIndex = 0;
    f.setAttribute("role", "button");
    f.setAttribute("aria-label", "Enlarge image");
    const go = () => open([{ src: f.dataset.full, title: "", text: f.dataset.cap || "", alt: f.dataset.cap || "" }], 0);
    f.addEventListener("click", go);
    f.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
  }
  $("#lb-close").addEventListener("click", () => dlg.close());
  $("#lb-prev").addEventListener("click", () => show(idx - 1));
  $("#lb-next").addEventListener("click", () => show(idx + 1));
  dlg.addEventListener("click", (e) => { if (e.target === dlg || e.target.classList.contains("lb-inner")) dlg.close(); });
  dlg.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft") show(idx - 1);
    else if (e.key === "ArrowRight") show(idx + 1);
  });
  dlg.addEventListener("close", () => { img.removeAttribute("src"); });
}

/* ───────────── reveal on scroll ───────────── */

function setupReveal() {
  const els = $$(".reveal");
  if (reduceMotion || !("IntersectionObserver" in window)) {
    for (const e of els) e.classList.add("in");
    return;
  }
  const io = new IntersectionObserver((es) => {
    for (const e of es) if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
  }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
  for (const e of els) io.observe(e);
}

/* ───────────── van: zoom into the dashboard monitor ───────────── */

function setupVanZoom() {
  const wrap = $("#van-wrap"), btn = $("#zoom-btn");
  if (!wrap || !btn) return;
  let auto = true;
  btn.addEventListener("click", () => { auto = false; wrap.classList.toggle("zoomed"); });
  wrap.addEventListener("click", () => { auto = false; wrap.classList.toggle("zoomed"); });
  if (reduceMotion) return;
  // first time the photo is well in view: hold for a moment, then glide in to the monitor
  const io = new IntersectionObserver((es) => {
    if (es.some((e) => e.isIntersecting)) {
      io.disconnect();
      setTimeout(() => { if (auto) wrap.classList.add("zoomed"); }, 2200);
    }
  }, { threshold: 0.6 });
  io.observe(wrap);
}

/* ───────────── go ───────────── */

setupReveal();
setupConsole();
setupMessaging();
buildGpsDiagram();
buildCrew();
setupGallery();
setupVanZoom();
for (const crt of $$(".crt[data-screen]")) setupCrt(crt);
