// Tracking: only the newest journal request is drawn (web/admin/js/tracking-fmt.js isStale, pages/tracking.js refresh).
import test from "node:test";
import assert from "node:assert/strict";

import { isStale } from "../../web/admin/js/tracking-fmt.js";

test("isStale: an older request's answer is ignored once a newer one was made", () => {
  assert.equal(isStale(1, 1), false);
  assert.equal(isStale(1, 2), true);
});

test("7 days → 24 h while loading: the slower week arriving last is dropped", async () => {
  let reqNo = 0;
  const drawn = [];
  const answer = (range, ms) => new Promise((r) => setTimeout(() => r(range), ms));
  async function refresh(range, ms) {
    const no = ++reqNo;
    const res = await answer(range, ms);
    if (isStale(no, reqNo)) return;
    drawn.push(res);
  }
  await Promise.all([refresh("7d", 40), refresh("24h", 5)]);
  assert.deepEqual(drawn, ["24h"]);
});
