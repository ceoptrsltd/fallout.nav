// Demo mode controller (?demo=1): telemetry generated in the browser, no fnav-core.
// Feeds the same onMessage() the WebSocket would, so the renderer is exactly the device one.

import { DemoSim } from "./sim.js";
import { NavLogic } from "./navlogic.js";
import { installBridge, settingsPatch } from "./bridge.js";
import { demoRadioStatus } from "./radio.js";

const DEMO_WEATHER = {
  type: "weather", temp_c: 24.4, feels_c: 23, code: 1, is_day: true, wind_ms: 5.5, gust_ms: 9, wind_from_deg: 250,
  days: [{ weekday: "MON", code: 0, t_max: 29, t_min: 17 }, { weekday: "TUE", code: 2, t_max: 27, t_min: 16 }], stale: false,
};

// the demo has no /tiles proxy: Esri tiles directly (shared with remote access)
export { ESRI_ATTRIBUTION, esriTileUrl } from "../map/esri.js";

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
  const radioT0 = now();
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
    tel.radio = demoRadioStatus(t - radioT0); // on-screen radio panel: a made-up station schedule
    tel.speed_limit = { kmh: 50, src: "osm" }; // the sign next to the speedometer (blinks above 53 km/h)
    onMessage(tel);
    if (!readySent) {
      readySent = true;
      bridge.ready();
    }
  }
  tick();
  setInterval(tick, 100);
  onMessage(DEMO_WEATHER); // made-up weather: the wind streaks around the car (the panel shows when the radio is off)

  return {
    sim,
    nav,
    connected: () => true,
    get playing() {
      return playing;
    },
  };
}
