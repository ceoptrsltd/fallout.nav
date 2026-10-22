// Radio: internet stations (radio-browser.info directory) played on the device through the Bluetooth speaker
// (docs/contracts/radio.md). Live state from telemetry `radio`, the rest from api/radio/state.
import { h, icon, clear } from "../dom.js";
import { api, errText } from "../api.js";
import { store, on, applySettings } from "../store.js";
import { toast, banner, badge, switchEl } from "../ui.js";
import {
  groupGenres, codecText, stationTags, sameStation, radioStateText, radioStateHint, debounce,
} from "../radio-fmt.js";

const ACTIVE = new Set(["playing", "connecting", "waiting_output"]);

function eqBars(small) {
  return h(".eq" + (small ? ".sm" : ""), { "aria-hidden": "true" }, h("i"), h("i"), h("i"), h("i"), h("i"));
}

export function mount(root) {
  let stopped = false;
  let st = null; // RadioState from the API
  let genres = [];
  let view = { kind: "recent" }; // recent | genre {tag,label} | search {q} | popular
  let stations = [];
  let listToken = 0;
  let volDragUntil = 0;
  let lastStationName;
  let lastState;
  let refreshTimer = null;

  const unavailable = h("div", { hidden: true });

  // ── now playing ──
  const eq = eqBars(false);
  const stName = h(".np-name", null, "—");
  const stBadge = h("span.badge", null, "—");
  const track = h(".np-track");
  const meta = h(".np-meta");
  const hint = h(".np-hint");
  const playBtn = h("button.btn.primary.np-play", { type: "button", onclick: () => togglePlay() });
  const volIn = h("input", { type: "range", min: 0, max: 100, step: 1, value: 60, "aria-label": "Volume" });
  const volVal = h("span.mono.np-vol-val", null, "60");
  const outEl = h(".np-out");

  const sendVolume = debounce(async (v) => {
    try {
      const r = await api.post("/api/radio/volume", { volume: v });
      if (!stopped && r) st = { ...st, ...r, volume: v };
    } catch (e) { toast("error", "Volume: " + errText(e)); }
  }, 160);
  volIn.addEventListener("input", () => {
    volDragUntil = Date.now() + 1500;
    volVal.textContent = volIn.value;
    sendVolume(Number(volIn.value));
  });
  volIn.addEventListener("change", () => { volDragUntil = Date.now() + 1500; sendVolume.flush(); });

  const npCard = h(".card.np-card", null,
    h(".np", null,
      h(".np-dial", null, eq),
      h(".np-main", null,
        h(".np-top", null, stName, stBadge),
        track, meta, hint),
    ),
    h(".np-ctl", null,
      playBtn,
      h(".np-vol", null, icon("vol"), volIn, volVal),
      outEl,
    ),
  );

  function genreLabel(tag) {
    const g = genres.find((x) => x.tag === tag);
    return g ? g.label : tag || null;
  }

  function renderNow() {
    const s = st || {};
    const state = s.state || "stopped";
    const active = ACTIVE.has(state);
    const station = s.station;
    stName.textContent = station ? station.name : "No station";
    stName.classList.toggle("off", !active);
    const sb = radioStateText(state);
    stBadge.textContent = sb.text;
    stBadge.className = "badge" + (sb.kind ? " " + sb.kind : "");
    eq.classList.toggle("on", state === "playing");
    eq.classList.toggle("wait", state === "connecting" || state === "waiting_output");

    clear(track);
    if (state === "playing" && s.track) track.append(h("span.note", null, "♪"), " ", s.track);
    else if (state === "playing") track.append(h("span.dim", null, "This station does not send track titles"));
    track.hidden = state !== "playing";

    clear(meta);
    if (station) {
      const parts = [];
      const gl = genreLabel(s.genre);
      if (gl) parts.push(gl);
      if (station.country) parts.push(station.country);
      const ct = codecText(station);
      if (ct) parts.push(ct);
      meta.textContent = parts.join("  ·  ");
    }
    const hv = radioStateHint(s);
    hint.textContent = hv;
    hint.hidden = !hv;
    hint.className = "np-hint" + (state === "error" ? " red" : state === "waiting_output" ? " amber" : "");

    clear(playBtn);
    if (active) { playBtn.append(icon("stop"), "Stop"); playBtn.disabled = false; playBtn.title = "Stop the radio"; }
    else { playBtn.append(icon("play"), "Play"); playBtn.disabled = !station; playBtn.title = station ? "Play “" + station.name + "” again" : "Pick a station first"; }

    if (Date.now() > volDragUntil && typeof s.volume === "number") {
      volIn.value = String(s.volume);
      volVal.textContent = String(s.volume);
    }

    clear(outEl);
    const out = s.output;
    if (out && out.connected) outEl.append(icon("speaker"), h("span", null, out.name), badge("connected", "ok"));
    else if (out) outEl.append(icon("speaker"), h("span", null, out.name), badge("not connected", "warn"), h("a", { href: "#/bluetooth" }, "Bluetooth"));
    else outEl.append(icon("speaker"), h("span.dim", null, "No speaker"), h("a", { href: "#/bluetooth" }, "Connect one"));
    renderListHighlight();
  }

  async function loadState() {
    try {
      const r = await api.get("/api/radio/state");
      if (stopped) return;
      st = r;
      unavailable.hidden = true;
      renderNow();
      if (view.kind === "recent") renderList();
    } catch (e) {
      if (stopped) return;
      if (e.status === 404) {
        unavailable.hidden = false;
        clear(unavailable).append(banner("warn", "Radio is not available on this device yet — update the device software."));
      } else if (!st) {
        hint.hidden = false;
        hint.className = "np-hint red";
        hint.textContent = errText(e);
      }
    }
  }
  const scheduleRefresh = () => {
    if (refreshTimer) return;
    refreshTimer = setTimeout(() => { refreshTimer = null; if (!stopped) loadState(); }, 700);
  };

  // telemetry: the light-weight live part (state, track, volume, speaker)
  const offTel = on("telemetry", (t) => {
    const r = t && t.radio;
    if (!r || !st) return;
    if (r.station !== lastStationName || r.state !== lastState) {
      if (lastState !== undefined) scheduleRefresh(); // another station / state: fetch the full record
      lastStationName = r.station;
      lastState = r.state;
    }
    const out = st.output ? { ...st.output, connected: !!r.output_connected, name: r.output || st.output.name } : st.output;
    const next = { ...st, state: r.state, track: r.track, volume: r.volume, output: out };
    if (next.state === st.state && next.track === st.track && next.volume === st.volume
        && (next.output && next.output.connected) === (st.output && st.output.connected)) return;
    st = next;
    renderNow();
  });

  async function togglePlay() {
    const active = st && ACTIVE.has(st.state);
    playBtn.disabled = true;
    try {
      st = await api.post(active ? "/api/radio/stop" : "/api/radio/resume", {});
    } catch (e) { toast("error", errText(e)); }
    if (!stopped) renderNow();
  }

  async function play(station) {
    const genre = view.kind === "genre" ? view.tag : null;
    st = { ...(st || {}), state: "connecting", station, genre, track: null, error: null };
    renderNow();
    try {
      st = await api.post("/api/radio/play", { station, genre });
    } catch (e) {
      toast("error", "Could not start the station: " + errText(e));
      loadState();
      return;
    }
    if (!stopped) { renderNow(); if (view.kind === "recent") renderList(); }
  }

  // ── genres ──
  const tilesBox = h("div");
  function renderGenres() {
    clear(tilesBox);
    for (const g of groupGenres(genres)) {
      tilesBox.append(h(".ggroup", null, h("h3", null, g.title), h(".gtiles", null, g.items.map((x) =>
        h("button.gtile." + x.kind, {
          type: "button",
          title: `${x.label} stations`,
          class: view.kind === "genre" && view.tag === x.tag ? "sel" : null,
          dataset: { tag: x.tag },
          onclick: () => setView({ kind: "genre", tag: x.tag, label: x.label }),
        }, h("span.e", null, x.emoji || "📻"), h("span.l", null, x.label))))));
    }
    if (!genres.length) tilesBox.append(h(".dim", null, "Loading genres…"));
  }
  async function loadGenres() {
    try {
      const r = await api.get("/api/radio/genres");
      if (stopped) return;
      genres = (r && r.genres) || [];
      renderGenres();
      renderNow();
    } catch (e) {
      if (!stopped && e.status !== 404) clear(tilesBox).append(h(".red", null, errText(e)));
    }
  }

  // ── station list ──
  const listTitle = h("h2", null, "Stations");
  const listInfo = h("span.dim.list-info");
  const listBody = h(".st-list", { role: "list" });
  const searchIn = h("input", { type: "text", placeholder: "Search stations by name", autocomplete: "off", "aria-label": "Search stations" });
  const btnPopular = h("button.btn.sm", { type: "button", onclick: () => setView({ kind: "popular" }) }, "🔥 Popular");
  const btnRecent = h("button.btn.sm", { type: "button", onclick: () => setView({ kind: "recent" }) }, "Recent");
  const searchForm = h("form.st-search", { onsubmit: (e) => {
    e.preventDefault();
    const q = searchIn.value.trim();
    if (q.length < 2) { toast("warn", "Type at least 2 letters"); return; }
    setView({ kind: "search", q });
  } }, icon("search"), searchIn, h("button.btn.sm", { type: "submit" }, "Find"));

  function setView(v) {
    view = v;
    for (const b of tilesBox.querySelectorAll(".gtile")) b.classList.toggle("sel", v.kind === "genre" && b.dataset.tag === v.tag);
    btnPopular.classList.toggle("on", v.kind === "popular");
    btnRecent.classList.toggle("on", v.kind === "recent");
    if (v.kind !== "search") searchIn.value = "";
    loadList();
  }

  async function loadList() {
    const my = ++listToken;
    if (view.kind === "recent") {
      listTitle.textContent = "Recent stations";
      renderList();
      return;
    }
    listTitle.textContent = view.kind === "genre" ? view.label : view.kind === "search" ? `“${view.q}”` : "Popular worldwide";
    listInfo.textContent = "";
    clear(listBody).append(h(".st-msg", null, h("span.spin"), " Loading stations…"));
    const qs = view.kind === "genre" ? "tag=" + encodeURIComponent(view.tag)
      : view.kind === "search" ? "q=" + encodeURIComponent(view.q) : "top=world";
    try {
      const r = await api.get("/api/radio/stations?" + qs);
      if (stopped || my !== listToken) return;
      stations = (r && r.stations) || [];
      renderList();
    } catch (e) {
      if (stopped || my !== listToken) return;
      stations = [];
      clear(listBody).append(h(".st-msg", null, banner("bad", e.status === 503
        ? "Radio directory is unreachable — the device has no internet right now, or radio-browser.info is down. Try again in a minute."
        : errText(e))));
    }
  }

  function stationRow(s) {
    const tags = stationTags(s, 3);
    const cur = st && st.station && sameStation(st.station, s);
    const row = h("button.st-row" + (cur ? ".cur" : ""), {
      type: "button", role: "listitem", title: `Play “${s.name}”`, onclick: () => play(s),
    },
      h("span.ic", null, cur && st.state === "playing" ? eqBars(true) : icon("play")),
      h("span.nm", null, s.name),
      h("span.cc", { title: s.country || null }, s.countrycode || ""),
      h("span.sub", null,
        codecText(s) ? h("span.mono", null, codecText(s)) : null,
        tags.map((t) => h("span.tag", null, t))),
    );
    row._station = s;
    return row;
  }

  function renderList() {
    const list = view.kind === "recent" ? ((st && st.recent) || []) : stations;
    clear(listBody);
    listInfo.textContent = list.length ? `${list.length} station${list.length === 1 ? "" : "s"}` : "";
    if (!list.length) {
      listBody.append(h(".st-msg.dim", null, view.kind === "recent"
        ? "Nothing played yet — pick a genre on the left."
        : "No stations found."));
      return;
    }
    for (const s of list) listBody.append(stationRow(s));
  }

  function renderListHighlight() {
    for (const row of listBody.querySelectorAll(".st-row")) {
      const cur = !!(st && st.station && sameStation(st.station, row._station));
      if (cur !== row.classList.contains("cur") || (cur && !!row.querySelector(".eq") !== (st.state === "playing"))) {
        row.replaceWith(stationRow(row._station));
      }
    }
  }

  // ── settings that belong to the radio ──
  const setBox = h(".np-set");
  function renderSettings(s) {
    clear(setBox);
    const add = (sec, key, label) => {
      if (!s || !s[sec] || s[sec][key] === undefined) return;
      setBox.append(switchEl({ checked: !!s[sec][key], label, onchange: async (v) => {
        try { applySettings(await api.patch("/api/settings", { [sec]: { [key]: v } })); toast("ok", "Saved"); }
        catch (e) { toast("error", errText(e)); renderSettings(store.settings); }
      } }));
    };
    add("radio", "resume_on_boot", "Resume after restart");
    add("display", "radio_panel", "Show the station on the car screen");
  }
  const offSet = on("settings", renderSettings);
  renderSettings(store.settings);

  root.append(
    h(".page-head", null, h("h1", null, "Radio"), setBox),
    unavailable,
    npCard,
    h(".radio-layout", null,
      h(".card", null, h("h2", null, "Genres"), tilesBox),
      h(".card", null,
        h(".card-head", null, listTitle, listInfo, btnRecent, btnPopular),
        searchForm,
        listBody,
        h(".hint", null, "Stations come from the community directory radio-browser.info. Playing uses about 60 MB of mobile data per hour (128 kbit/s)."),
      ),
    ),
  );

  renderNow();
  renderGenres();
  loadGenres();
  (async () => {
    await loadState();
    if (stopped) return;
    if (!(st && st.recent && st.recent.length)) setView({ kind: "popular" });
    else setView({ kind: "recent" });
  })();
  const slowPoll = setInterval(() => { if (!document.hidden) loadState(); }, 10000);

  return () => {
    stopped = true;
    offTel();
    offSet();
    clearInterval(slowPoll);
    clearTimeout(refreshTimer);
    sendVolume.flush();
  };
}
