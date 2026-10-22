// Modal screen preview: iframe <root>display/?preview=1 (768×576) at real physical size.
import { h, clear } from "./dom.js";
import { modal, segmented, switchEl } from "./ui.js";
import { on, store } from "./store.js";
import { appUrl } from "./api.js";

const OPTS_KEY = "fnav.preview.opts";
const CANVAS_W = 768, CANVAS_H = 576;

let physPromise = null;
/** Shared module from display/ui/physical.js if present; otherwise the local fallback. */
export function loadPhysical() {
  if (!physPromise) {
    physPromise = import(appUrl("display/ui/physical.js"))
      .then((m) => {
        if (typeof m.getPxPerMm === "function" && typeof m.setPxPerMm === "function" && typeof m.renderCalibrator === "function") return m;
        throw new Error("bad physical.js");
      })
      .catch(() => import("./physical.js"));
  }
  return physPromise;
}

function loadOpts() {
  try { return { mode: "physical", safe: true, grid: false, ...JSON.parse(localStorage.getItem(OPTS_KEY) || "{}") }; } catch { return { mode: "physical", safe: true, grid: false }; }
}
function saveOpts(o) { try { localStorage.setItem(OPTS_KEY, JSON.stringify(o)); } catch { /* */ } }

let current = null;

export async function openPreview() {
  if (current) return;
  const phys = await loadPhysical();
  const opts = loadOpts();

  const iframe = h("iframe", { src: appUrl("display/?preview=1"), title: "Device screen", allow: "autoplay", scrolling: "no" });
  const safe = h(".pv-safe");
  const grid = h(".pv-grid");
  const frame = h(".pv-frame", null, iframe, grid, safe);
  const stage = h(".pv-stage", null, frame);
  const info = h(".pv-info");
  const calib = h(".card", { hidden: true, style: { marginBottom: "12px" } });
  let calibDispose = null;
  let lastPpm = 0;

  const modeSeg = segmented({
    name: "pvmode",
    value: opts.mode,
    options: [{ value: "physical", label: "1:1 physical size" }, { value: "pixel", label: "Pixel for pixel" }],
    onchange: (v) => { opts.mode = v; saveOpts(opts); render(); },
  });
  const safeSw = switchEl({ checked: opts.safe, label: "Safe area", onchange: (v) => { opts.safe = v; saveOpts(opts); render(); } });
  const gridSw = switchEl({ checked: opts.grid, label: "10 mm grid", onchange: (v) => { opts.grid = v; saveOpts(opts); render(); } });
  const calBtn = h("button.btn", { type: "button", onclick: () => {
    calib.hidden = !calib.hidden;
    if (!calib.hidden && !calibDispose) { clear(calib); const r = phys.renderCalibrator(calib); calibDispose = typeof r === "function" ? r : r && typeof r.destroy === "function" ? () => r.destroy() : () => {}; }
  } }, "Calibrate");
  const openBtn = h("a.btn", { href: appUrl("display/"), target: "_blank", rel: "noopener" }, "Open in new tab");

  function render() {
    const s = store.settings?.display || {};
    const wmm = s.screen?.w_mm ?? 92, hmm = s.screen?.h_mm ?? 69;
    const pct = s.safe_area_pct ?? 5;
    const ppm = phys.getPxPerMm();
    lastPpm = ppm;
    const scale = opts.mode === "physical" ? (wmm * ppm) / CANVAS_W : 1;
    const w = CANVAS_W * scale, hgt = CANVAS_H * scale;
    frame.style.width = w + "px";
    frame.style.height = hgt + "px";
    iframe.style.transform = `scale(${scale})`;
    safe.hidden = !opts.safe;
    safe.style.inset = `${pct}%`;
    grid.hidden = !opts.grid;
    const stepPx = (10 * w) / wmm; // 10 mm of the device screen in preview pixels
    grid.style.backgroundImage =
      "linear-gradient(to right, rgba(255,255,255,.4) 1px, transparent 1px), linear-gradient(to bottom, rgba(255,255,255,.4) 1px, transparent 1px)";
    grid.style.backgroundSize = `${stepPx}px ${stepPx}px`;

    const physW = w / ppm, physH = hgt / ppm;
    clear(info);
    if (opts.mode === "physical") {
      info.append(
        h("span", null, `Real size on your screen: ${physW.toFixed(1)} × ${physH.toFixed(1)} mm`),
        h("span", null, `scale ×${scale.toFixed(3)} · ${ppm.toFixed(3)} px/mm`),
      );
      if (Math.abs(physH - hmm) > 1.0) info.append(h("span.amber", null, `settings say height ${hmm} mm (the 4:3 canvas gives ${physH.toFixed(1)} mm)`));
    } else {
      info.append(
        h("span", null, `Canvas ${CANVAS_W}×${CANVAS_H} px, 1 canvas pixel = 1 CSS pixel`),
        h("span", null, `on the device: ${wmm} × ${hmm} mm, 1 mm ≈ ${(CANVAS_W / wmm).toFixed(2)} px`),
      );
    }
    info.append(h("span", null, `safe area ${pct}% per side (≈ ${((wmm * pct) / 100).toFixed(1)} mm)`));
  }

  const body = h("div", null,
    h(".pv-toolbar", null, modeSeg, safeSw, gridSw, h(".grow"), calBtn, openBtn),
    calib,
    stage,
    info,
  );

  const m = modal({
    title: "Device screen preview",
    body,
    onClose: () => {
      offSettings();
      clearInterval(poll);
      window.removeEventListener("fnav:pxpermm", render);
      calibDispose && calibDispose();
      iframe.src = "about:blank"; // stop the camera stream
      current = null;
    },
  });
  current = m;
  const offSettings = on("settings", render);
  // the calibrator (incl. the shared module) changes localStorage: watch the value
  const poll = setInterval(() => { if (Math.abs(phys.getPxPerMm() - lastPpm) > 1e-6) render(); }, 250);
  window.addEventListener("fnav:pxpermm", render);
  render();
}
