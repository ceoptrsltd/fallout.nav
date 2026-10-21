// postMessage API of the demo (docs/contracts/demo.md, section 1).
//   page -> iframe: {type: "fnav-demo", play?, speedOverride?, orientation?, compassStyle?, forceMode?, layer?}
//   iframe -> page: {type: "fnav-demo-ready"} once; {type: "fnav-demo-state", ...} ~4 Hz.

const ORIENTATIONS = new Set(["heading_up", "north_up"]);
const FORCE_MODES = new Set(["auto", "map", "camera"]);
const LAYERS = new Set(["imagery", "street"]);

/**
 * Validate a command message into a normalized patch (unknown/invalid fields are dropped).
 * Returns null if the message is not a demo command.
 */
export function parseCommand(data) {
  if (!data || typeof data !== "object" || data.type !== "fnav-demo") return null;
  const out = {};
  if (typeof data.play === "boolean") out.play = data.play;
  if ("speedOverride" in data) {
    const v = data.speedOverride;
    if (v === null) out.speedOverride = null;
    else if (typeof v === "number" && Number.isFinite(v)) out.speedOverride = Math.max(0, Math.min(60, v));
  }
  if (ORIENTATIONS.has(data.orientation)) out.orientation = data.orientation;
  if (typeof data.compassStyle === "string" && /^v\d{1,2}$/.test(data.compassStyle)) out.compassStyle = data.compassStyle;
  if (FORCE_MODES.has(data.forceMode)) out.forceMode = data.forceMode;
  if (LAYERS.has(data.layer)) out.layer = data.layer;
  return out;
}

/** Settings patch (Settings shape) for the display-related commands. */
export function settingsPatch(cmd) {
  const display = {};
  if (cmd.orientation) display.orientation = cmd.orientation;
  if (cmd.compassStyle) display.compass_style = cmd.compassStyle;
  if (cmd.forceMode) display.force_mode = cmd.forceMode;
  if (cmd.layer) display.layers = cmd.layer === "street" ? { street_map_instead: true } : { street_map_instead: false, imagery: true };
  return Object.keys(display).length ? { display } : null;
}

/**
 * Listen for commands and publish state. The target is the embedding page (window.parent);
 * when the page is opened directly, messages go to the window itself (handy from the console).
 */
export function installBridge({ onCommand, getState, intervalMs = 250, win = window }) {
  const target = win.parent && win.parent !== win ? win.parent : win;
  const post = (msg) => {
    try {
      target.postMessage(msg, "*"); // non-sensitive demo state; the embedding origin is not known in advance
    } catch {}
  };
  win.addEventListener("message", (ev) => {
    const cmd = parseCommand(ev.data);
    if (cmd) onCommand(cmd);
  });
  let timer = null;
  return {
    ready() {
      post({ type: "fnav-demo-ready" });
      if (!timer) {
        timer = setInterval(() => {
          const s = getState();
          if (s) post({ type: "fnav-demo-state", ...s });
        }, intervalMs);
      }
    },
  };
}
