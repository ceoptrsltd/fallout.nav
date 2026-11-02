// Dialogs and the Back button (web/admin/js/hist-fmt.js; ui.js keeps the dialog stack and the history in step).
import test from "node:test";
import assert from "node:assert/strict";

import { entryDepth, dialogState, popPlan } from "../../web/admin/js/hist-fmt.js";

test("entryDepth: dialog entries only; pages and other states are 0", () => {
  assert.equal(entryDepth(null), 0);
  assert.equal(entryDepth(undefined), 0);
  assert.equal(entryDepth({}), 0);
  assert.equal(entryDepth({ fnavModal: "a:1", fnavDepth: 1 }), 1);
  assert.equal(entryDepth({ fnavModal: "a:2", fnavDepth: 3 }), 3);
  assert.equal(entryDepth({ fnavModal: "", fnavDepth: 2 }), 0);
  assert.equal(entryDepth({ fnavModal: "a:1", fnavDepth: 0 }), 0);
  assert.equal(entryDepth({ fnavModal: "a:1", fnavDepth: "2" }), 0);
  assert.equal(entryDepth("x"), 0);
});

test("dialogState: one deeper than the entry it is pushed over", () => {
  assert.deepEqual(dialogState("a:1", null), { fnavModal: "a:1", fnavDepth: 1 });
  assert.deepEqual(dialogState("a:2", { fnavModal: "a:1", fnavDepth: 1 }), { fnavModal: "a:2", fnavDepth: 2 });
});

test("popPlan: Back from a dialog closes it; from two dialogs — the top one", () => {
  const player = { id: "a:1", depth: 1 };
  const confirm = { id: "a:2", depth: 2 };
  assert.deepEqual(popPlan([player], null), { close: ["a:1"], dead: false });
  assert.deepEqual(popPlan([player, confirm], { fnavModal: "a:1", fnavDepth: 1 }), { close: ["a:2"], dead: false });
  // history.go(-2) or a page link: everything, the top first
  assert.deepEqual(popPlan([player, confirm], null), { close: ["a:2", "a:1"], dead: false });
});

test("popPlan: our own back after ✕ lands on the page — nothing more to close", () => {
  assert.deepEqual(popPlan([], null), { close: [], dead: false });
  assert.deepEqual(popPlan([{ id: "a:1", depth: 1 }], { fnavModal: "a:1", fnavDepth: 1 }), { close: [], dead: false });
});

test("popPlan: a dialog entry nobody owns is dead — stepped over", () => {
  // reloaded with the More sheet open, or a route change left the entry under the new page
  assert.deepEqual(popPlan([], { fnavModal: "old:3", fnavDepth: 1 }), { close: [], dead: true });
  // the More sheet closed under the "Install app" steps: back from the steps lands on the sheet's entry
  assert.deepEqual(popPlan([{ id: "a:2", depth: 2 }], { fnavModal: "a:1", fnavDepth: 1 }), { close: ["a:2"], dead: true });
  // a live dialog below the dead entry stays open
  assert.deepEqual(popPlan([{ id: "a:1", depth: 1 }], { fnavModal: "a:5", fnavDepth: 2 }), { close: [], dead: true });
});
