// Slide-to-switch: a device command only happens when the thumb is dragged all the way to the other end (a tap or a
// short drag snaps back). Used for the power stations' outputs (docs/contracts/power.md «Управление») — the slide
// itself is the confirmation, no extra dialog.
import { h } from "./dom.js";

const THUMB = 46; // px: the thumb with its borders (app.css .sl-thumb) plus the 2 px inset on each side
export const COMMIT_AT = 0.85; // dragged this far along the track — switch

/** Where the thumb ends after a drag: fraction (0…1) of the track from its start side → commit? */
export function slideCommits(frac) {
  return Number.isFinite(frac) && frac >= COMMIT_AT;
}

/** What a switch shows: telemetry, or the slide just made until telemetry catches up (or expect.until passes). */
export function shownState(reported, expect, now = Date.now()) {
  if (expect && now < expect.until && reported !== expect.on) return expect.on;
  return reported;
}

/**
 * slideSwitch({label, on, onslide}) → element with .set({on, busy, disabled, unknown}).
 * on — current state (thumb right = on); onslide(newOn) — called once the slide commits (may return a promise).
 */
export function slideSwitch({ label, on = false, onslide }) {
  const thumb = h("span.sl-thumb", { "aria-hidden": "true" });
  const hint = h("span.sl-hint");
  const track = h("span.sl-track", null, hint, thumb);
  const state = h("span.sl-state");
  const el = h(".sl", { role: "switch", "aria-label": label }, h("span.sl-label", null, label), track, state);
  let cur = { on: !!on, busy: false, disabled: false, unknown: false };
  let drag = null;

  function paint(frac = null) {
    const start = cur.on ? 1 : 0;
    const pos = frac === null ? start : cur.on ? 1 - frac : frac;
    thumb.style.left = `calc(${(pos * 100).toFixed(1)}% - ${(pos * (THUMB - 2) - (1 - 2 * pos) * 3).toFixed(1)}px)`; // 3 px from either end
    el.classList.toggle("on", cur.on && !cur.unknown);
    el.classList.toggle("busy", cur.busy);
    el.classList.toggle("disabled", cur.disabled);
    el.setAttribute("aria-checked", cur.unknown ? "mixed" : String(cur.on));
    hint.textContent = cur.busy ? "…" : cur.disabled ? "" : cur.on ? "‹ slide off" : "slide on ›";
    hint.classList.toggle("left", !cur.on);
    state.textContent = cur.unknown ? "—" : cur.on ? "ON" : "OFF";
  }

  function frac(ev) {
    const r = track.getBoundingClientRect();
    const x = Math.max(0, Math.min(1, (ev.clientX - r.left - THUMB / 2) / Math.max(1, r.width - THUMB)));
    return cur.on ? 1 - x : x;
  }

  thumb.addEventListener("pointerdown", (ev) => {
    if (cur.busy || cur.disabled) return;
    ev.preventDefault();
    thumb.setPointerCapture(ev.pointerId);
    drag = { id: ev.pointerId };
    el.classList.add("drag");
  });
  thumb.addEventListener("pointermove", (ev) => {
    if (!drag || ev.pointerId !== drag.id) return;
    paint(frac(ev));
  });
  const end = async (ev) => {
    if (!drag || ev.pointerId !== drag.id) return;
    const f = frac(ev);
    drag = null;
    el.classList.remove("drag");
    if (!slideCommits(f)) {
      paint();
      return;
    }
    const want = !cur.on;
    cur = { ...cur, on: want, busy: true };
    paint();
    try {
      await onslide?.(want);
    } finally {
      cur = { ...cur, busy: false };
      paint();
    }
  };
  thumb.addEventListener("pointerup", end);
  thumb.addEventListener("pointercancel", (ev) => {
    if (drag && ev.pointerId === drag.id) {
      drag = null;
      el.classList.remove("drag");
      paint();
    }
  });

  el.set = (s) => {
    if (drag) return; // not under the user's finger
    cur = { ...cur, ...s };
    paint();
  };
  paint();
  return el;
}
