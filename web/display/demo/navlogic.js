// Port of fnav/core/navlogic.py (screen mode + target zoom from speed) for the browser demo.
// Pure logic, no I/O: time is passed explicitly (monotonic seconds). Keep in sync with the Python version.

export const STALE_FIX_S = 5.0;
export const DEFAULT_ZOOM = 17.0;

export function hasFix(gps) {
  if (!gps || (gps.fix !== "2d" && gps.fix !== "3d") || gps.lat == null || gps.lon == null) return false;
  return gps.age_s == null || gps.age_s <= STALE_FIX_S;
}

export function zoomForSpeed(nav, speed) {
  const table = [...(nav.zoom_table || [])].sort((a, b) => a.min_mps - b.min_mps);
  if (!table.length) return DEFAULT_ZOOM;
  let zoom = table[0].zoom; // below every threshold -> the "slowest" step
  for (const step of table) {
    if (step.min_mps <= speed) zoom = step.zoom;
    else break;
  }
  return Number(zoom);
}

export class NavLogic {
  /** nav = settings.nav, display = settings.display */
  constructor(nav, display, { startTime = 0, startupGraceS = 5.0 } = {}) {
    this.nav = nav;
    this.display = display;
    this.startupGraceS = startupGraceS;
    this._t0 = startTime;
    this._mode = nav.no_fix_mode;
    this._reason = "startup";
    this._hadFix = false;
    this._inSpeedRegime = false;
    this._switchSince = null;
    this._zoom = null;
    this._zoomCand = null;
    this._zoomSince = 0;
    this._lastNow = null;
  }

  applySettings(nav, display) {
    this.nav = nav;
    this.display = display;
  }

  get targetZoom() {
    return this._zoom !== null ? this._zoom : zoomForSpeed(this.nav, 0);
  }

  /** Returns a DisplayState: {mode, reason, target_zoom, orientation}. */
  update(now, gps) {
    if (this._lastNow !== null && now < this._lastNow) {
      this._switchSince = null;
      this._zoomCand = null;
    }
    this._lastNow = now;

    if (hasFix(gps)) {
      const speed = Math.max(0, gps.speed_mps || 0);
      this._hadFix = true;
      this._updateZoom(now, speed);
      this._updateMode(now, speed);
    } else {
      this._inSpeedRegime = false;
      this._switchSince = null;
      this._zoomCand = null;
      this._mode = this.nav.no_fix_mode;
      const inGrace = !this._hadFix && now - this._t0 < this.startupGraceS;
      this._reason = inGrace ? "startup" : "no_fix";
    }

    let mode = this._mode, reason = this._reason;
    const force = this.display.force_mode || "auto";
    if (force !== "auto") {
      mode = force;
      reason = "forced";
    }
    return { mode, reason, target_zoom: this.targetZoom, orientation: this.display.orientation };
  }

  _updateZoom(now, speed) {
    const z = zoomForSpeed(this.nav, speed);
    if (this._zoom === null) {
      this._zoom = z;
      this._zoomCand = null;
      return;
    }
    if (z === this._zoom) {
      this._zoomCand = null;
      return;
    }
    if (this._zoomCand !== z) {
      this._zoomCand = z;
      this._zoomSince = now;
    }
    if (now - this._zoomSince >= this.nav.zoom_delay_s) {
      this._zoom = z;
      this._zoomCand = null;
    }
  }

  _updateMode(now, speed) {
    const nav = this.nav;
    this._reason = "speed";
    if (!this._inSpeedRegime) {
      this._inSpeedRegime = true;
      this._switchSince = null;
      if (speed > nav.map_above_mps) this._mode = "map";
      else if (speed < nav.camera_below_mps) this._mode = "camera";
      return;
    }
    let want, target;
    if (this._mode === "map") {
      want = speed < nav.camera_below_mps;
      target = "camera";
    } else {
      want = speed > nav.map_above_mps;
      target = "map";
    }
    if (!want) {
      this._switchSince = null;
      return;
    }
    if (this._switchSince === null) this._switchSince = now;
    if (now - this._switchSince >= nav.switch_delay_s) {
      this._mode = target;
      this._switchSince = null;
    }
  }
}
