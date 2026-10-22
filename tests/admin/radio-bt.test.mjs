// Radio and Bluetooth pages: pure helpers (web/admin/js/radio-fmt.js), docs/contracts/radio.md.
import test from "node:test";
import assert from "node:assert/strict";

import {
  groupGenres, codecText, stationTags, sameStation, radioStateText, radioStateHint,
  rssiBars, btIconName, btName, sortDevices, splitDevices, debounce,
} from "../../web/admin/js/radio-fmt.js";

test("groupGenres: moods → genres → eras, order kept, empty groups dropped", () => {
  const g = groupGenres([
    { tag: "pop", label: "Pop", kind: "genre" }, { tag: "relax", label: "Calm", kind: "mood" },
    { tag: "rock", label: "Rock", kind: "genre" }, { tag: "x", label: "X", kind: "other" },
  ]);
  assert.deepEqual(g.map((x) => [x.title, x.items.map((i) => i.tag)]), [["Moods", ["relax"]], ["Genres", ["pop", "rock"]]]);
  assert.deepEqual(groupGenres(null), []);
});

test("codecText and stationTags", () => {
  assert.equal(codecText({ codec: "mp3", bitrate: 128 }), "MP3 · 128 kbps");
  assert.equal(codecText({ codec: "AAC", bitrate: 0 }), "AAC");
  assert.equal(codecText(null), "");
  assert.deepEqual(stationTags({ tags: "Jazz, jazz,smooth jazz,,lounge,chill" }), ["jazz", "smooth jazz", "lounge"]);
  assert.deepEqual(stationTags({ tags: "" }), []);
});

test("sameStation: uuid first, otherwise the stream URL", () => {
  assert.ok(sameStation({ uuid: "a", url: "u1" }, { uuid: "a", url: "u2" }));
  assert.ok(!sameStation({ uuid: "a", url: "u" }, { uuid: "b", url: "u" }));
  assert.ok(sameStation({ url: "u" }, { uuid: "b", url: "u" }));
  assert.ok(!sameStation(null, { url: "u" }));
});

test("radio state wording", () => {
  assert.deepEqual(radioStateText("playing"), { text: "On air", kind: "ok" });
  assert.equal(radioStateText("waiting_output").kind, "warn");
  assert.equal(radioStateText("weird").text, "weird");
  assert.match(radioStateHint({ state: "waiting_output", output: { name: "JBL Go 3" } }), /JBL Go 3/);
  assert.match(radioStateHint({ state: "waiting_output", output: null }), /Connect a Bluetooth speaker/);
  assert.equal(radioStateHint({ state: "error", error: "HTTP 404" }), "HTTP 404");
  assert.equal(radioStateHint({ state: "playing" }), "");
});

test("rssiBars and icons", () => {
  assert.deepEqual([-40, -60, -70, -85, -99, null].map(rssiBars), [4, 3, 2, 1, 0, null]);
  assert.equal(btIconName({ icon: "audio-card" }), "speaker");
  assert.equal(btIconName({ icon: "audio-headset" }), "headset");
  assert.equal(btIconName({ icon: "phone" }), "phone");
  assert.equal(btIconName({ icon: null, audio: true }), "speaker");
  assert.equal(btIconName({}), "bt");
  assert.equal(btName({ name: "  ", address: "AA:BB:CC:DD:EE:FF" }), "AA:BB:CC:DD:EE:FF");
});

test("sortDevices: radio speaker, connected, paired, audio, then signal; splitDevices folds nameless", () => {
  const devs = [
    { address: "01", name: "Far phone", rssi: -90 },
    { address: "02", name: "Near phone", rssi: -50 },
    { address: "03", name: "Paired", paired: true, rssi: -80 },
    { address: "04", name: "Speaker", audio: true, paired: true },
    { address: "05", name: null, rssi: -40 },
    { address: "06", name: "Connected", connected: true, paired: true },
  ];
  assert.deepEqual(sortDevices(devs, "04").map((d) => d.address), ["04", "06", "03", "05", "02", "01"]);
  const { shown, folded } = splitDevices(devs);
  assert.deepEqual(folded.map((d) => d.address), ["05"]);
  assert.equal(shown.length, 5);
});

test("debounce: one trailing call with the last value; flush and cancel", async () => {
  const calls = [];
  const d = debounce((v) => calls.push(v), 20);
  d(1); d(2); d(3);
  await new Promise((r) => setTimeout(r, 40));
  assert.deepEqual(calls, [3]);
  d(4); d.flush();
  assert.deepEqual(calls, [3, 4]);
  d(5); d.cancel();
  await new Promise((r) => setTimeout(r, 40));
  assert.deepEqual(calls, [3, 4]);
});
