// Dialogs and the Back button: pure helpers (no DOM) for ui.js. Every dismissable dialog gets a history entry of its own
// (same URL, state {fnavModal: id, fnavDepth: n}; n — the dialog entries under it plus one), so Back closes the dialog
// instead of leaving the page. Tests: tests/admin/hist.test.mjs.

/** How many dialog entries the history entry with `state` stands for (0 — a page, or a state of someone else). */
export function entryDepth(state) {
  if (!state || typeof state !== "object" || typeof state.fnavModal !== "string" || !state.fnavModal) return 0;
  return Number.isInteger(state.fnavDepth) && state.fnavDepth > 0 ? state.fnavDepth : 0;
}

/** History state of a new dialog entry over the current entry's `state`. */
export function dialogState(id, state) {
  return { fnavModal: String(id), fnavDepth: entryDepth(state) + 1 };
}

/**
 * The history moved to an entry with `state` (popstate): which open dialogs close (ids, the top one first), and
 * whether that entry is a dead dialog entry to step over — its dialog is gone (the page was reloaded, a route change
 * or a lower dialog closed first left it behind). open: [{id, depth}] — the dialogs with an entry, bottom first.
 */
export function popPlan(open, state) {
  const d = entryDepth(state);
  const list = Array.isArray(open) ? open : [];
  const close = list.filter((m) => m.depth > d).map((m) => m.id).reverse();
  const dead = d > 0 && !list.some((m) => m.depth === d && m.id === state.fnavModal);
  return { close, dead };
}
