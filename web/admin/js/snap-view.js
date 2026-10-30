// Event snapshots in event lists (dashboard, alarm log): a small thumbnail — the server's ~5 KB copy
// (api/alarm/snapshot/<id>.jpg?thumb=1), loaded only when it scrolls into view — and the full snapshots in a modal.
import { h, isNum } from "./dom.js";
import { appUrl } from "./api.js";
import { modal } from "./ui.js";
import { eventExtras, snapPath, thumbPath } from "./dash-fmt.js";

/** Thumbnail button of an event (the front camera's first; the modal shows both); null — no snapshot. */
export function eventThumb(ev) {
  const x = eventExtras(ev);
  const cam = x.snapshot ? "front" : x.snapshotRear ? "rear" : null;
  if (!cam) return null;
  // width / height: the row keeps its size before the picture comes; lazy + async: off-screen ones are not fetched
  const img = h("img", { src: appUrl(thumbPath(ev.id, cam)), alt: "", loading: "lazy", decoding: "async", width: 72, height: 54 });
  const what = x.snapshot && x.snapshotRear ? "Show the snapshots" : "Show the snapshot";
  const b = h("button.db-thumb", { type: "button", title: what, "aria-label": what, onclick: () => showEventSnapshots(ev) }, img);
  img.addEventListener("error", () => { b.hidden = true; }); // the snapshot is gone (the log keeps the last 100)
  return b;
}

/** The event's snapshots, full size: the front camera's and / or the rear's (named when the rear is among them). */
export function showEventSnapshots(ev) {
  const x = eventExtras(ev);
  const cams = [x.snapshot ? "front" : null, x.snapshotRear ? "rear" : null].filter(Boolean);
  const named = cams.length > 1 || cams[0] === "rear";
  const body = cams.map((cam) => {
    const img = h("img.db-snapbig", { src: appUrl(snapPath(ev.id, cam)), alt: (cam === "rear" ? "Rear camera: " : "") + (ev.title || "Snapshot") });
    img.addEventListener("error", () => img.replaceWith(h(".dim", null, "The snapshot is gone (the log keeps the last 100).")));
    return named ? h(".db-snapcam", null, h(".db-snaplbl", null, cam === "rear" ? "Rear camera" : "Front camera"), img) : img;
  });
  body.push(h(".dim.db-snapwhen", null, isNum(ev.ts) ? new Date(ev.ts * 1000).toLocaleString() : ""));
  const m = modal({ title: ev.title || "Snapshot", width: 800, body });
  m.el.classList.add("db-snapmodal");
}
