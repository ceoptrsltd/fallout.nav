// Tracking: where the vehicle is now and where it has been. The journal (places + visits, one GPS point a
// minute, positions within 50 m merged) is kept on the server: api/tracking/* (docs/contracts/remote.md).
// Map: Leaflet + Esri hybrid (imagery + boundaries/places + transportation) straight from arcgisonline.
import { h, icon, clear, kmh, isNum, compassWord } from "../dom.js";
import { api, ApiError, errText } from "../api.js";
import { store, on } from "../store.js";
import { modal, segmented, switchEl, banner, poll } from "../ui.js";
import { loadVendorCss, loadVendorScript } from "../vendor.js";
import { RANGES, rangeQuery, fmtStay, fmtVisit, fmtWhen, agoText, sortVisits, esriTemplates, googleMapsUrl } from "../tracking-fmt.js";

const OPTS_KEY = "fnav.tracking.opts";
const REFRESH_MS = 60000;
const LIVE_STALE_MS = 5000;
const ESRI_ATTR = 'Powered by <a href="https://www.esri.com" target="_blank" rel="noopener">Esri</a> | Esri, Maxar, Earthstar Geographics, and the GIS User Community';

const nowS = () => Date.now() / 1000;

function loadOpts() {
  const def = { range: "7d", line: true, follow: false };
  try { return { ...def, ...JSON.parse(localStorage.getItem(OPTS_KEY) || "{}") }; } catch { return def; }
}
function saveOpts(o) { try { localStorage.setItem(OPTS_KEY, JSON.stringify(o)); } catch { /* */ } }

const STOP_SVG = (n) => `
<svg viewBox="0 0 30 40" width="30" height="40" aria-hidden="true">
  <path d="M15 39 9.5 29h11z" fill="#ffb62e"/>
  <rect x="2" y="2" width="26" height="27" rx="5" fill="#141813" stroke="#ffb62e" stroke-width="2.5"/>
  <text x="15" y="23.5" text-anchor="middle" font-family="Segoe UI, Arial, sans-serif" font-size="20" font-weight="800" fill="#ffb62e">P</text>
</svg>${n > 1 ? `<span class="n">${n > 99 ? "99+" : n}</span>` : ""}`;

const VEH_HTML = `
<div class="rot"><svg viewBox="0 0 36 36" width="36" height="36" aria-hidden="true">
  <circle class="halo" cx="18" cy="18" r="16"/>
  <path class="arrow" d="M18 4 27.5 29 18 23.5 8.5 29z"/>
  <circle class="dot" cx="18" cy="18" r="6"/>
</svg></div>`;

/** Shortest-way angle interpolation, degrees. */
function lerpDeg(a, b, k) {
  const d = ((((b - a) % 360) + 540) % 360) - 180;
  return (a + d * k + 360) % 360;
}

export async function mount(root) {
  const opts = loadOpts();
  if (!RANGES.some((r) => r.value === opts.range)) opts.range = "7d";

  // ── layout ──
  const rangeSeg = segmented({ name: "trkrange", value: opts.range, options: RANGES.map((r) => ({ value: r.value, label: r.label })), onchange: (v) => { opts.range = v; saveOpts(opts); fitPending = true; refresh(); } });
  const lineSw = switchEl({ checked: opts.line, label: "Track line", onchange: (v) => { opts.line = v; saveOpts(opts); refresh(); } });
  const followSw = switchEl({ checked: opts.follow, label: "Follow vehicle", onchange: (v) => { opts.follow = v; saveOpts(opts); if (v) centerVehicle(); } });
  const fitBtn = h("button.btn.sm", { type: "button", title: "Show all places of the period", onclick: () => fitAll() }, icon("fit"), "Fit");
  const vehBtn = h("button.btn.sm", { type: "button", title: "Center on the vehicle", onclick: () => centerVehicle(true) }, icon("gps"), "Vehicle");
  const posEl = h("span.trk-pos", null, "No position yet");
  const countEl = h("span.dim", null, "");
  const updEl = h("span.faint", null, "");
  const errBox = h("div", { hidden: true });
  const mapEl = h(".trk-map");
  const legend = h(".trk-legend", null,
    h("span", null, h("i.lg-stop", null, "P"), "parking (stayed 5 min or longer)"),
    h("span", null, h("i.lg-dot"), "waypoint"),
    h("span", null, h("i.lg-veh"), "vehicle"),
  );

  root.append(
    h(".page-head", null, h("h1", null, "Tracking"), rangeSeg, lineSw, followSw, fitBtn, vehBtn),
    errBox,
    h(".card.trk-card", null,
      h(".trk-info", null, posEl, h(".grow"), countEl, updEl),
      mapEl,
      legend,
    ),
  );

  // ── Leaflet ──
  let L;
  try {
    await loadVendorCss("leaflet/leaflet.css");
    L = await loadVendorScript("leaflet/leaflet.js", "L");
  } catch (e) {
    clear(mapEl);
    mapEl.append(h(".overlay-msg", null, "Map library failed to load: " + errText(e)));
    return () => {};
  }
  if (!mapEl.isConnected) return () => {}; // left the page while loading

  const map = L.map(mapEl, { preferCanvas: true, worldCopyJump: true, maxZoom: 20, zoomControl: true });
  map.attributionControl.setPrefix('<a href="https://leafletjs.com" target="_blank" rel="noopener">Leaflet</a>');
  const tpl = esriTemplates(store.settings);
  const tileOpts = { maxZoom: 20, maxNativeZoom: 19, crossOrigin: true };
  L.tileLayer(tpl.imagery, { ...tileOpts, attribution: ESRI_ATTR }).addTo(map);
  L.tileLayer(tpl.transport, tileOpts).addTo(map);
  L.tileLayer(tpl.places, tileOpts).addTo(map);
  map.setView([50, 15], 4);

  const lineLayer = L.layerGroup().addTo(map);
  const dotLayer = L.layerGroup().addTo(map); // waypoints (canvas circle markers)
  const stopLayer = L.layerGroup().addTo(map); // parking pins (DOM markers, above the dots)
  const stopIconCache = new Map();
  const stopIcon = (n) => {
    const k = Math.min(n, 100);
    if (!stopIconCache.has(k)) stopIconCache.set(k, L.divIcon({ className: "trk-stop", html: STOP_SVG(n), iconSize: [30, 40], iconAnchor: [15, 39], tooltipAnchor: [0, -34] }));
    return stopIconCache.get(k);
  };

  // ── data ──
  let places = [];
  let lastSeen = null; // api/tracking/last
  let fitPending = false; // range changed: fit the new set of places
  let loaded = false; // the first journal request has finished (successfully or not)
  let viewSet = false; // the initial view (places or the vehicle) has been chosen
  let destroyed = false;

  function tipHtml(p) {
    const kind = p.kind === "stop" ? "Parking" : "Waypoint";
    const n = p.visits || 1;
    const last = isNum(p.last_ts) ? fmtWhen(p.last_ts, nowS()) : "—";
    const more = p.kind === "stop" ? ` · longest ${fmtStay(p.longest_s)}` : "";
    return `<b>${kind}</b> · last here ${last}<br>${n} visit${n === 1 ? "" : "s"}${more}`;
  }

  function renderPlaces() {
    dotLayer.clearLayers();
    stopLayer.clearLayers();
    let stops = 0;
    for (const p of places) {
      if (!isNum(p.lat) || !isNum(p.lon)) continue;
      let m;
      if (p.kind === "stop") {
        stops++;
        m = L.marker([p.lat, p.lon], { icon: stopIcon(p.visits || 1), riseOnHover: true, keyboard: false });
        m.addTo(stopLayer);
      } else {
        // amber on Esri's orange roads needs a dark rim to stay visible
        m = L.circleMarker([p.lat, p.lon], { radius: 4.5, weight: 2, color: "#0a0c0a", opacity: 0.9, fillColor: "#ffd24a", fillOpacity: 1 });
        m.addTo(dotLayer);
      }
      m.bindTooltip(tipHtml(p), { direction: "top", className: "trk-tip", offset: p.kind === "stop" ? [0, 0] : [0, -5] });
      m.on("click", () => openPlace(p));
    }
    countEl.textContent = places.length
      ? `${places.length} place${places.length === 1 ? "" : "s"} · ${stops} parking`
      : "No places in this period";
  }

  function renderLine(points) {
    lineLayer.clearLayers();
    if (!opts.line || !points || points.length < 2) return;
    const ll = points.filter((p) => isNum(p[1]) && isNum(p[2])).map((p) => [p[1], p[2]]);
    L.polyline(ll, { color: "#0a0c0a", weight: 4, opacity: 0.35, interactive: false }).addTo(lineLayer);
    L.polyline(ll, { color: "#fff1b8", weight: 2, opacity: 0.75, dashArray: "6 6", interactive: false }).addTo(lineLayer);
  }

  function showError(e) {
    clear(errBox);
    if (!e) { errBox.hidden = true; return; }
    errBox.hidden = false;
    const unreachable = e instanceof ApiError && (e.status === 503 || e.status === 502 || e.status === 504 || e.status === 0);
    const detail = e instanceof ApiError && typeof e.detail === "string" ? e.detail : errText(e);
    errBox.append(unreachable
      ? banner("warn", ["Tracking server is unreachable", detail && detail !== "Tracking server is unreachable" ? h("div.dim", null, detail) : null, h("div.dim", null, "The journal is kept on the server; the live position still works while the device is connected.")])
      : banner("bad", "Could not load the journal: " + detail));
  }

  async function refresh() {
    const q = rangeQuery(opts.range, nowS());
    try {
      const [pl, tr] = await Promise.all([
        api.get("/api/tracking/places" + q),
        opts.line ? api.get("/api/tracking/track" + q) : Promise.resolve(null),
      ]);
      if (destroyed) return;
      places = Array.isArray(pl && pl.places) ? pl.places : [];
      renderPlaces();
      renderLine(tr && tr.points);
      showError(null);
      updEl.textContent = "updated " + fmtWhen(nowS(), nowS()).replace("today ", "");
      if (fitPending) { fitPending = false; fitAll(); }
    } catch (e) {
      if (destroyed) return;
      showError(e);
    }
    if (!loaded) {
      loaded = true;
      const v = vehPos();
      if (places.length) { fitAll(true); viewSet = true; }
      else if (v) { map.setView([v.lat, v.lon], 16, { animate: false }); viewSet = true; }
    }
    if (!liveFresh()) {
      try {
        const l = await api.get("/api/tracking/last");
        if (destroyed) return;
        lastSeen = l && isNum(l.lat) && isNum(l.lon) ? l : null;
      } catch { /* the error banner above already tells the story */ }
      showVehicle();
    }
  }

  /** The place of the newest visit in the journal (where the vehicle is now or was last). */
  function currentPlaceId() {
    let best = null;
    for (const q of places) if (isNum(q.last_ts) && (!best || q.last_ts > best.last_ts)) best = q;
    return best ? best.id : null;
  }

  function fitAll(initial = false) {
    const pts = places.filter((p) => isNum(p.lat) && isNum(p.lon)).map((p) => [p.lat, p.lon]);
    const v = vehPos();
    if (v) pts.push([v.lat, v.lon]);
    const o = { animate: !initial };
    if (pts.length === 1) map.setView(pts[0], 16, o);
    else if (pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.08), { maxZoom: 17, ...o });
    else if (!initial) map.setView([50, 15], 4);
  }

  // ── place modal ──
  function openPlace(p) {
    const body = h(".trk-modal");
    const kind = p.kind === "stop" ? "Parking place" : "Waypoint";
    const coords = `${p.lat.toFixed(5)}, ${p.lon.toFixed(5)}`;
    const sum = h(".trk-sum");
    // first the numbers of the selected period, then all-time ones from api/tracking/places/{id}
    const renderSum = (q, allTime) => {
      clear(sum);
      sum.append(
        h("span", null, h("span.k", null, "Coordinates "), h("span.mono", null, coords)),
        h("span", null, h("span.k", null, allTime ? "Visits " : "Visits in period "), String(q.visits ?? "—")),
        isNum(q.total_s) ? h("span", null, h("span.k", null, "Total time "), fmtStay(q.total_s)) : null,
        isNum(q.longest_s) ? h("span", null, h("span.k", null, "Longest "), fmtStay(q.longest_s)) : null,
      );
    };
    renderSum(p, false);
    const list = h("div", null, h(".dim", null, "Loading visits…"));
    body.append(sum, list);
    const m = modal({
      title: kind,
      body,
      width: 600,
      foot: [
        h("a.btn", { href: googleMapsUrl(p.lat, p.lon), target: "_blank", rel: "noopener noreferrer", style: { textDecoration: "none" } }, icon("pin"), "Open in Google Maps"),
        h("button.btn", { onclick: () => m.close() }, "Close"),
      ],
    });
    api.get("/api/tracking/places/" + encodeURIComponent(p.id)).then((d) => {
      clear(list);
      if (d && d.place) renderSum({ ...p, ...d.place }, true);
      const visits = sortVisits(d && d.visits);
      if (!visits.length) { list.append(h(".dim", null, "No visits recorded.")); return; }
      const rows = visits.map((v, i) => {
        const f = fmtVisit(v);
        // the newest visit whose last point is a couple of minutes old: the vehicle is still there
        const here = i === 0 && isNum(v.end_ts) && nowS() - v.end_ts < 150 && currentPlaceId() === p.id;
        return h("tr", null,
          h("td.nowrap", null, f.date),
          h("td.num", null, f.arrived),
          h("td.num" + (here ? ".green" : ""), null, here ? "still here" : f.left),
          h("td.num" + (f.passing ? ".dim" : ""), null, f.passing ? "passing" : f.duration),
        );
      });
      list.append(
        h(".dim", { style: { marginBottom: "8px" } }, visits.length === 1 ? "Visited once:" : `Visited ${visits.length} times, newest first:`),
        h(".tbl-wrap.trk-visits", null, h("table", null,
          h("thead", null, h("tr", null, h("th", null, "Date"), h("th.num", null, "Arrived"), h("th.num", null, "Left"), h("th.num", null, "Stayed"))),
          h("tbody", null, rows),
        )),
      );
    }).catch((e) => {
      clear(list);
      list.append(banner("bad", "Could not load visits: " + errText(e)));
    });
  }

  // ── vehicle ──
  const vehMarker = L.marker([0, 0], { icon: L.divIcon({ className: "trk-veh", html: VEH_HTML, iconSize: [36, 36], iconAnchor: [18, 18] }), interactive: false, keyboard: false, zIndexOffset: 1000 });
  let vehOnMap = false;
  let live = null; // {lat, lon, heading, speed, t}
  // shown position glides from where it is to each new fix over the interval between fixes
  // (10 Hz on the device, 2 Hz through the tunnel): smooth motion with a delay of one update
  let cur = null; // {lat, lon, heading}
  let seg = null; // {from, to, t0, dur}
  let lastFixAt = 0;
  let raf = 0;

  const liveFresh = () => !!live && Date.now() - live.t < LIVE_STALE_MS;
  const vehPos = () => (liveFresh() ? live : lastSeen ? { lat: lastSeen.lat, lon: lastSeen.lon, heading: lastSeen.course_deg } : null);

  function setVehClass(stale, hasHeading) {
    const el = vehMarker.getElement();
    if (!el) return;
    el.classList.toggle("stale", stale);
    el.classList.toggle("nohead", !hasHeading);
  }

  function retarget() {
    const target = vehPos();
    if (!target) return;
    const t = performance.now();
    const dur = lastFixAt ? Math.min(1000, Math.max(80, t - lastFixAt)) : 0;
    lastFixAt = t;
    const to = { lat: target.lat, lon: target.lon, heading: isNum(target.heading) ? target.heading : cur ? cur.heading : 0 };
    const far = !cur || Math.abs(cur.lat - to.lat) > 0.005 || Math.abs(cur.lon - to.lon) > 0.005;
    if (far || !dur) { cur = { ...to }; seg = null; place(); return; }
    seg = { from: { ...cur }, to, t0: t, dur };
    if (!raf && !destroyed) raf = requestAnimationFrame(frame);
  }

  function place() {
    if (!cur) return;
    vehMarker.setLatLng([cur.lat, cur.lon]);
    const el = vehMarker.getElement();
    const rot = el && el.querySelector(".rot");
    if (rot) rot.style.transform = `rotate(${cur.heading.toFixed(1)}deg)`;
  }

  function frame(ts) {
    raf = 0;
    if (!seg || destroyed) return;
    const k = Math.min(1, (ts - seg.t0) / seg.dur);
    cur = {
      lat: seg.from.lat + (seg.to.lat - seg.from.lat) * k,
      lon: seg.from.lon + (seg.to.lon - seg.from.lon) * k,
      heading: lerpDeg(seg.from.heading, seg.to.heading, k),
    };
    place();
    if (k < 1) raf = requestAnimationFrame(frame);
    else seg = null;
  }

  function showVehicle() {
    const v = vehPos();
    const fresh = liveFresh();
    if (!v) {
      if (vehOnMap) { vehMarker.remove(); vehOnMap = false; }
      posEl.className = "trk-pos";
      posEl.textContent = store.online ? "Live: no GPS fix" : "No position yet";
      return;
    }
    if (!vehOnMap) { vehMarker.addTo(map); vehOnMap = true; }
    setVehClass(!fresh, isNum(v.heading));
    if (fresh) {
      posEl.className = "trk-pos live";
      const sp = isNum(live.speed) ? `${Math.round(kmh(live.speed))} km/h` : "— km/h";
      posEl.textContent = `● Live · ${sp}${isNum(live.heading) ? " · " + compassWord(live.heading) : ""}`;
    } else {
      posEl.className = "trk-pos stale";
      const ts = lastSeen && (lastSeen.ts ?? lastSeen.received_ts);
      posEl.textContent = isNum(ts) ? `Last seen ${fmtWhen(ts, nowS())} (${agoText(ts, nowS())})` : "Last known position";
    }
    retarget();
    // nothing in the journal yet: start at the vehicle
    if (loaded && !viewSet) { viewSet = true; map.setView([v.lat, v.lon], 16, { animate: false }); }
  }

  function centerVehicle(force = false) {
    const v = vehPos();
    if (!v) return;
    if (force) map.setView([v.lat, v.lon], Math.max(map.getZoom(), 16));
    else map.panTo([v.lat, v.lon]);
  }

  const offTel = on("telemetry", (t) => {
    const g = t && t.gps;
    if (!g || g.fix === "none" || !isNum(g.lat) || !isNum(g.lon)) return;
    const wasFresh = liveFresh();
    const hd = isNum(t.heading && t.heading.deg) ? t.heading.deg : isNum(g.course_deg) ? g.course_deg : null;
    live = { lat: g.lat, lon: g.lon, heading: hd, speed: g.speed_mps, t: Date.now() };
    if (!wasFresh) showVehicle();
    else {
      retarget();
      const sp = isNum(live.speed) ? `${Math.round(kmh(live.speed))} km/h` : "— km/h";
      posEl.textContent = `● Live · ${sp}${isNum(hd) ? " · " + compassWord(hd) : ""}`;
    }
    if (opts.follow && !map.getBounds().pad(-0.25).contains([g.lat, g.lon])) map.panTo([g.lat, g.lon]);
  });
  // live telemetry stopped (device offline / no fix): fall back to the server's last point
  const staleTimer = setInterval(() => {
    if (live && !liveFresh() && posEl.classList.contains("live")) { showVehicle(); refresh(); }
  }, 1000);

  const stopPoll = poll(refresh, REFRESH_MS);
  showVehicle();

  return () => {
    destroyed = true;
    stopPoll();
    offTel();
    clearInterval(staleTimer);
    if (raf) cancelAnimationFrame(raf);
    map.remove();
  };
}
