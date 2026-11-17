// Slide-to-switch: a device command only happens when the thumb is dragged all the way to the other end (a tap or a
// short drag snaps back). Used for the power stations' outputs (docs/contracts/power.md «Управление») — the slide
// itself is the confirmation, no extra dialog.
// A switch with ontimer also takes a timer: letting go in the middle of the track sets «do it in N min» instead of
// doing it now — 60 min near the start down to 5 min near the end, in 5 min steps; a bubble above the finger says
// what letting go here will do. The track's start and the stretch before its end are empty: letting go there does
// nothing, so a timer is never set by a slip of the finger.
import { h, put, setAttr } from "./dom.js";

const THUMB = 46; // px: the thumb with its borders (app.css .sl-thumb) plus the 2 px inset on each side
export const COMMIT_AT = 0.85; // dragged this far along the track — switch

/** Where the thumb ends after a drag: fraction (0…1) of the track from its start side → commit? */
export function slideCommits(frac) {
  return Number.isFinite(frac) && frac >= COMMIT_AT;
}

export const TIMER_FROM = 0.14; // the timer stretch of the track; before it and from TIMER_TO to COMMIT_AT — nothing
export const TIMER_TO = 0.74;
export const TIMER_STEP_MIN = 5;
export const TIMER_STEPS = 12; // 60 … 5 min

/** What letting go at this point of the track does: {kind: "none" | "timer" | "now", minutes?}. */
export function slideZone(frac, timers = true) {
  if (slideCommits(frac)) return { kind: "now" };
  if (!timers || !(frac >= TIMER_FROM && frac < TIMER_TO)) return { kind: "none" };
  const i = Math.min(TIMER_STEPS - 1, Math.floor((frac - TIMER_FROM) / (TIMER_TO - TIMER_FROM) * TIMER_STEPS));
  return { kind: "timer", minutes: (TIMER_STEPS - i) * TIMER_STEP_MIN };
}

/** «24 min» / «40 s» — the time left on a timer. */
export function leftText(inS) {
  return inS >= 60 ? `${Math.ceil(inS / 60)} min` : `${Math.max(0, Math.round(inS))} s`;
}

/** What a switch shows: telemetry, or the slide just made until telemetry catches up (or expect.until passes). */
export function shownState(reported, expect, now = Date.now()) {
  if (expect && now < expect.until && reported !== expect.on) return expect.on;
  return reported;
}

/**
 * slideSwitch({label, on, onslide, ontimer}) → element with .set({on, busy, disabled, unknown, timer}).
 * on — current state (thumb right = on); onslide(newOn) — called once the slide commits (may return a promise).
 * ontimer(newOn, minutes) — the thumb was let go in the timer stretch; minutes 0 — the timer was tapped away.
 * timer — {on, in_s} of a timer that is running (shown in the track; a tap on it removes it), or null.
 */
export function slideSwitch({ label, on = false, onslide, ontimer }) {
  const thumb = h("span.sl-thumb", { "aria-hidden": "true" });
  const hint = h("span.sl-hint");
  const track = h("span.sl-track", null, hint, thumb);
  const state = h("span.sl-state");
  const tip = h("span.sl-tip", { hidden: true });
  const el = h(".sl", { role: "switch", "aria-label": label }, h("span.sl-label", null, label), track, state, tip);
  let cur = { on: !!on, busy: false, disabled: false, unknown: false, timer: null };
  let drag = null;
  let shownLeft = null; // thumb.style.left as last written
  const flag = (node, name, v) => { if (node.classList.contains(name) !== !!v) node.classList.toggle(name, !!v); };

  // the bubble: above the finger, moves with the thumb
  function showTip(zone) {
    const word = cur.on ? "OFF" : "ON";
    tip.hidden = zone.kind === "none";
    if (tip.hidden) return;
    tip.textContent = zone.kind === "now" ? `${word} now` : `${word} in ${zone.minutes} min`;
    tip.classList.toggle("now", zone.kind === "now");
    const box = el.getBoundingClientRect();
    const t = thumb.getBoundingClientRect();
    const half = tip.offsetWidth / 2;
    const x = Math.max(half, Math.min(box.width - half, t.left + t.width / 2 - box.left));
    tip.style.left = `${x.toFixed(1)}px`;
    if (drag && drag.tip !== tip.textContent) {
      if (drag.tip !== undefined) navigator.vibrate?.(8);
      drag.tip = tip.textContent;
    }
  }

  function paint(frac = null) {
    const start = cur.on ? 1 : 0;
    const pos = frac === null ? start : cur.on ? 1 - frac : frac;
    // set() comes with every telemetry packet (10 a second) and nearly always repeats: only what changed is written
    const left = `calc(${(pos * 100).toFixed(1)}% - ${(pos * (THUMB - 2) - (1 - 2 * pos) * 3).toFixed(1)}px)`; // 3 px from either end
    if (left !== shownLeft) thumb.style.left = shownLeft = left;
    flag(el, "on", cur.on && !cur.unknown);
    flag(el, "busy", cur.busy);
    flag(el, "disabled", cur.disabled);
    setAttr(el, "aria-checked", cur.unknown ? "mixed" : String(cur.on));
    const tm = cur.unknown || cur.disabled ? null : cur.timer;
    put(hint, cur.busy ? "…" : cur.disabled ? "" : tm ? `${tm.on ? "on" : "off"} in ${leftText(tm.in_s)} ✕`
      : cur.on ? "‹ slide off" : "slide on ›");
    flag(hint, "timer", !!tm); // a running timer sits in the track; a tap on it removes the timer
    setAttr(hint, "title", tm ? "Tap to remove the timer" : "");
    flag(hint, "left", !cur.on);
    put(state, cur.unknown ? "—" : cur.on ? "ON" : "OFF");
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
    const f = frac(ev);
    paint(f);
    showTip(slideZone(f, !!ontimer));
  });
  const end = async (ev) => {
    if (!drag || ev.pointerId !== drag.id) return;
    const f = frac(ev);
    drag = null;
    el.classList.remove("drag");
    tip.hidden = true;
    const zone = slideZone(f, !!ontimer);
    if (zone.kind === "timer") {
      paint();
      ontimer(!cur.on, zone.minutes);
      return;
    }
    if (zone.kind !== "now") {
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
      tip.hidden = true;
      paint();
    }
  });
  hint.addEventListener("click", () => {
    if (cur.timer && !cur.unknown && !cur.disabled) ontimer?.(cur.timer.on, 0);
  });

  el.set = (s) => {
    if (drag) return; // not under the user's finger
    cur = { ...cur, ...s };
    paint();
  };
  paint();
  return el;
}
