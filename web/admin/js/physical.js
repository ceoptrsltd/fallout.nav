// Fallback "physical size" module (used if /display/ui/physical.js is missing).
// Same interface and localStorage key as the shared module:
//   getPxPerMm(), setPxPerMm(v), renderCalibrator(container)
import { h } from "./dom.js";

export const KEY = "fnav.preview.pxPerMm";
export const DEFAULT_PX_PER_MM = 96 / 25.4;
export const CARD_W_MM = 85.6;
export const CARD_H_MM = 53.98;

export function getPxPerMm() {
  let v = NaN;
  try { v = parseFloat(localStorage.getItem(KEY)); } catch { /* */ }
  return Number.isFinite(v) && v > 0.5 && v < 40 ? v : DEFAULT_PX_PER_MM;
}

export function setPxPerMm(v) {
  v = Math.min(40, Math.max(0.5, Number(v)));
  try { localStorage.setItem(KEY, String(v)); } catch { /* */ }
  window.dispatchEvent(new CustomEvent("fnav:pxpermm", { detail: v }));
}

/** Draws the calibrator: bank-card sized frame + slider. Returns a cleanup function. */
export function renderCalibrator(container) {
  const card = h(".pv-card", { style: { height: "0px" } }, h("div", { style: { position: "absolute", inset: "0", display: "flex", alignItems: "center", justifyContent: "center", color: "#333", fontFamily: "var(--mono)", fontSize: "12px", textAlign: "center", padding: "6px" } }, "Hold a bank card to the screen: 85.60 × 53.98 mm"));
  const range = h("input", { type: "range", min: "2", max: "8", step: "0.005", style: { flex: "1 1 260px" } });
  const num = h("input", { type: "number", min: "0.5", max: "40", step: "0.01", style: { width: "90px" } });
  const info = h("span.dim", { style: { fontFamily: "var(--mono)", fontSize: "12.5px" } });
  const upd = (v) => {
    card.style.width = CARD_W_MM * v + "px";
    card.style.height = CARD_H_MM * v + "px";
    card.style.borderRadius = 3.18 * v + "px";
    range.value = v;
    num.value = v.toFixed(3);
    info.textContent = `${(v * 25.4).toFixed(1)} px/inch · 1 mm = ${v.toFixed(2)} px`;
  };
  const apply = (v) => { setPxPerMm(v); upd(getPxPerMm()); };
  range.addEventListener("input", () => apply(Number(range.value)));
  num.addEventListener("change", () => { if (Number(num.value) > 0) apply(Number(num.value)); });
  const step = (d) => h("button.btn.sm", { type: "button", onclick: () => apply(getPxPerMm() + d) }, (d > 0 ? "+" : "−") + Math.abs(d));
  container.append(
    h("div", { style: { display: "flex", flexDirection: "column", gap: "10px" } },
      h("div.dim", { style: { fontSize: "13px" } }, "Hold a bank card against your laptop screen and move the slider until the frame matches the card width. The value is remembered in this browser."),
      card,
      h("div", { style: { display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap" } },
        step(-0.05), step(-0.01), range, step(0.01), step(0.05), num, "px/mm",
        h("button.btn.sm", { type: "button", onclick: () => apply(DEFAULT_PX_PER_MM) }, "96 dpi"),
      ),
      info,
    ),
  );
  upd(getPxPerMm());
  return () => {};
}
