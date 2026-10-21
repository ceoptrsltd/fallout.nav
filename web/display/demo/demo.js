// Demo mode controller (?demo=1): telemetry generated in the browser, no fnav-core.
// Feeds the same onMessage() the WebSocket would, so the renderer is exactly the device one.

import { DemoSim } from "./sim.js";
import { NavLogic } from "./navlogic.js";
import { installBridge, settingsPatch } from "./bridge.js";

export const ESRI_ATTRIBUTION = {
  imagery: "Esri, Maxar, Earthstar Geographics",
  street: "Esri, HERE, Garmin, © OpenStreetMap contributors",
};

/** Direct Esri tile URL from the TileSettings templates (the demo has no /tiles proxy). */
export function esriTileUrl(settings, layer, z, x, y) {
  const t = settings.tiles || {};
  const tpl = t[`${layer}_url`];
  if (!tpl) return null;
  let url = tpl.replace("{z}", z).replace("{x}", x).replace("{y}", y);
  if (t.esri_api_key) url += (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(t.esri_api_key);
  return url;
}

/**
 * @param {object} o
 * @param {(msg: object) => void} o.onMessage — same handler as for WebSocket messages
 * @param {() => object} o.getSettings
 * @param {(patch: object) => void} o.applyPatch — deep-merge a Settings patch
 * @param {() => object|null} o.getState — {speedMps, headingDeg, mode, zoom, lat, lon} for the bridge
 * @param {() => number} o.now — monotonic seconds
 */
export function startDemo({ onMessage, getSettings, applyPatch, getState, now }) {
  const sim = new DemoSim();
  const s0 = getSettings();
  const nav = new NavLogic(s0.nav, s0.display, { startTime: now() });
  let playing = true;
  let lastTick = now();
  let display = null;
  let readySent = false;

  const bridge = installBridge({
    onCommand(cmd) {
      if ("play" in cmd) playing = cmd.play;
      if ("speedOverride" in cmd) sim.speedOverride = cmd.speedOverride;
      const patch = settingsPatch(cmd);
      if (patch) applyPatch(patch);
    },
    getState() {
      const st = getState();
      if (!st) return null;
      const s = getSettings();
      return { ...st, orientation: s.display.orientation, compassStyle: s.display.compass_style };
    },
  });

  function tick() {
    const t = now();
    const dt = Math.min(0.5, Math.max(0, t - lastTick));
    lastTick = t;
    if (playing) sim.step(dt);
    const s = getSettings();
    const tel = sim.telemetry(Date.now() / 1000, { headingSettings: s.heading, paused: !playing });
    nav.applySettings(s.nav, s.display);
    // paused: the car stands, but the screen keeps its mode/zoom (no switch to camera)
    if (playing || !display) display = nav.update(t, tel.gps);
    else display = { ...display, mode: s.display.force_mode !== "auto" ? s.display.force_mode : display.mode, orientation: s.display.orientation };
    tel.display = display;
    onMessage(tel);
    if (!readySent) {
      readySent = true;
      bridge.ready();
    }
  }
  tick();
  setInterval(tick, 100);

  return {
    sim,
    nav,
    connected: () => true,
    get playing() {
      return playing;
    },
  };
}
