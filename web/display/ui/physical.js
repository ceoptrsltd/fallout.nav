// Физический размер на экране ноутбука: калибровка «CSS-px на миллиметр».
// Общий модуль для галереи (C) и превью веб-интерфейса (D): import … from "/display/ui/physical.js".

const KEY = "fnav.preview.pxPerMm";
export const DEFAULT_PX_PER_MM = 96 / 25.4; // ≈ 3.78 — номинал CSS
export const CARD_MM = { w: 85.6, h: 53.98, r: 3.18 }; // банковская карта ISO/IEC 7810 ID-1
const MIN = 1.5;
const MAX = 12;
/** Событие на window при изменении калибровки: detail = { pxPerMm }. */
export const EVENT = "fnav:pxpermm";

function clamp(v) {
  return Math.min(MAX, Math.max(MIN, v));
}

/** Текущая калибровка (CSS-px в одном реальном мм). */
export function getPxPerMm() {
  try {
    const v = parseFloat(globalThis.localStorage?.getItem(KEY) ?? "");
    if (Number.isFinite(v) && v >= MIN && v <= MAX) return v;
  } catch {
    /* localStorage недоступен (file://, приватный режим) */
  }
  return DEFAULT_PX_PER_MM;
}

/** Сохранить калибровку и оповестить подписчиков (событие "fnav:pxpermm" на window). */
export function setPxPerMm(v) {
  const x = clamp(Number(v));
  if (!Number.isFinite(x)) return getPxPerMm();
  try {
    globalThis.localStorage?.setItem(KEY, String(Math.round(x * 1000) / 1000));
  } catch {
    /* ignore */
  }
  try {
    globalThis.dispatchEvent?.(new CustomEvent(EVENT, { detail: { pxPerMm: x } }));
  } catch {
    /* ignore */
  }
  return x;
}

/** Масштаб CSS для холста 768 px, чтобы он был шириной screenWmm реальных мм. */
export function canvasScale(screenWmm = 92, canvasW = 768, pxPerMm = getPxPerMm()) {
  return (screenWmm * pxPerMm) / canvasW;
}

/** Подписка на изменения калибровки; возвращает функцию отписки. */
export function onPxPerMmChange(cb) {
  const h = (e) => cb(e.detail?.pxPerMm ?? getPxPerMm());
  globalThis.addEventListener?.(EVENT, h);
  return () => globalThis.removeEventListener?.(EVENT, h);
}

/**
 * Калибратор: прямоугольник банковской карты (85.60 × 53.98 мм) + ползунок.
 * container — DOM-элемент. options.onChange(pxPerMm) — необязательный колбэк.
 * Возвращает { element, destroy() }.
 */
export function renderCalibrator(container, options = {}) {
  const doc = container.ownerDocument;
  const root = doc.createElement("div");
  root.className = "fnav-cal";
  root.innerHTML = `
    <style>
      .fnav-cal { font: 14px/1.4 system-ui, sans-serif; color: inherit; display: flex; gap: 16px;
                  align-items: center; flex-wrap: wrap; }
      .fnav-cal__card { box-sizing: border-box; border: 2px dashed currentColor; border-radius: 3mm;
                        background: repeating-linear-gradient(45deg, rgba(128,128,128,.12) 0 8px, transparent 8px 16px);
                        display: flex; align-items: center; justify-content: center; text-align: center;
                        flex: none; opacity: .9; }
      .fnav-cal__ctl { display: flex; flex-direction: column; gap: 6px; min-width: 240px; }
      .fnav-cal__ctl input[type=range] { width: 100%; }
      .fnav-cal__row { display: flex; gap: 8px; align-items: center; }
      .fnav-cal__row button { font: inherit; padding: 2px 10px; cursor: pointer; }
      .fnav-cal__val { font-variant-numeric: tabular-nums; }
    </style>
    <div class="fnav-cal__card" data-card><span>Hold a bank card against the screen<br>and match this frame to it</span></div>
    <div class="fnav-cal__ctl">
      <label>Screen calibration: <span class="fnav-cal__val" data-val></span></label>
      <input type="range" min="${MIN}" max="${MAX}" step="0.01" data-range aria-label="pixels per millimetre">
      <div class="fnav-cal__row">
        <button type="button" data-minus title="smaller">−</button>
        <button type="button" data-plus title="larger">+</button>
        <button type="button" data-reset>Reset</button>
      </div>
      <small>ID-1 card: 85.60 × 53.98 mm. Browser zoom (Ctrl+wheel) affects the calibration — keep it at 100%.</small>
    </div>`;
  const card = root.querySelector("[data-card]");
  const range = root.querySelector("[data-range]");
  const val = root.querySelector("[data-val]");

  const show = (v) => {
    card.style.width = `${CARD_MM.w * v}px`;
    card.style.height = `${CARD_MM.h * v}px`;
    card.style.borderRadius = `${CARD_MM.r * v}px`;
    range.value = String(v);
    val.textContent = `${v.toFixed(2)} px/mm (${Math.round(v * 25.4)} dpi)`;
  };
  const apply = (v) => {
    const x = setPxPerMm(v);
    show(x);
    options.onChange?.(x);
  };
  range.addEventListener("input", () => apply(parseFloat(range.value)));
  root.querySelector("[data-minus]").addEventListener("click", () => apply(getPxPerMm() - 0.01));
  root.querySelector("[data-plus]").addEventListener("click", () => apply(getPxPerMm() + 0.01));
  root.querySelector("[data-reset]").addEventListener("click", () => apply(DEFAULT_PX_PER_MM));
  // синхронизация, если калибровку изменили в другом месте страницы
  const off = onPxPerMmChange((v) => show(v));

  show(getPxPerMm());
  container.appendChild(root);
  return {
    element: root,
    destroy() {
      off();
      root.remove();
    },
  };
}
