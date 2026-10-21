// Browser demo: synthetic drive around the Las Vegas Strip (JS take on fnav/mock/sensors.py).
// Pure logic, no DOM: call step(dt) to advance and telemetry(unixTime) to get a Telemetry-like object
// (without "display" — that comes from the NavLogic port).

import { LV_LEG_SPEEDS, LV_LEG_STARTS, LV_ROUTE, LV_STOPS } from "./route.js";

const R = 6371000;
const DEG = Math.PI / 180;

function distM(a, b) {
  const la = ((a[0] + b[0]) / 2) * DEG;
  return Math.hypot((b[0] - a[0]) * DEG * R, (b[1] - a[1]) * DEG * R * Math.cos(la));
}

function bearingDeg(a, b) {
  const la1 = a[0] * DEG, la2 = b[0] * DEG, dlo = (b[1] - a[1]) * DEG;
  const y = Math.sin(dlo) * Math.cos(la2);
  const x = Math.cos(la1) * Math.sin(la2) - Math.sin(la1) * Math.cos(la2) * Math.cos(dlo);
  return ((Math.atan2(y, x) / DEG) + 360) % 360;
}

function wrap180(a) {
  const r = (((a % 360) + 540) % 360) - 180;
  return r === -180 ? 180 : r;
}

/** Small seeded PRNG (mulberry32) + Gaussian — deterministic demo and tests. */
function rng(seed) {
  let s = seed >>> 0;
  const u = () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  u.gauss = (sd = 1) => {
    const a = Math.max(1e-12, u()), b = u();
    return Math.sqrt(-2 * Math.log(a)) * Math.cos(2 * Math.PI * b) * sd;
  };
  return u;
}

/** Closed polyline with distance lookups. */
export class LoopRoute {
  constructor(pts) {
    this.pts = pts;
    const n = pts.length;
    this.cum = [0];
    this.brg = [];
    for (let i = 0; i < n; i++) {
      const a = pts[i], b = pts[(i + 1) % n];
      this.brg.push(bearingDeg(a, b));
      this.cum.push(this.cum[i] + distM(a, b));
    }
    this.total = this.cum[n];
  }

  segAt(dist) {
    const d = ((dist % this.total) + this.total) % this.total;
    let lo = 0, hi = this.pts.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (this.cum[mid] <= d) lo = mid;
      else hi = mid - 1;
    }
    return { i: lo, d };
  }

  /** {lat, lon, course, seg} at distance along the loop. */
  at(dist) {
    const { i, d } = this.segAt(dist);
    const a = this.pts[i], b = this.pts[(i + 1) % this.pts.length];
    const len = this.cum[i + 1] - this.cum[i];
    const f = len > 0 ? (d - this.cum[i]) / len : 0;
    return { lat: a[0] + (b[0] - a[0]) * f, lon: a[1] + (b[1] - a[1]) * f, course: this.brg[i], seg: i };
  }

  /** Distance of the route point nearest to [lat, lon] (projection onto segments). */
  project(lat, lon) {
    let best = { err: Infinity, dist: 0 };
    const n = this.pts.length;
    for (let i = 0; i < n; i++) {
      const a = this.pts[i], b = this.pts[(i + 1) % n];
      const k = Math.cos(a[0] * DEG);
      const ax = 0, ay = 0, bx = (b[1] - a[1]) * k, by = b[0] - a[0];
      const px = (lon - a[1]) * k, py = lat - a[0];
      const l2 = bx * bx + by * by;
      const f = l2 > 0 ? Math.max(0, Math.min(1, (px * bx + py * by) / l2)) : 0;
      const err = Math.hypot(px - (ax + bx * f), py - (ay + by * f));
      if (err < best.err) best = { err, dist: this.cum[i] + f * (this.cum[i + 1] - this.cum[i]) };
    }
    return best.dist;
  }

  /** Distance from `from` forward to `to` along the loop, [0, total). */
  ahead(from, to) {
    return (((to - from) % this.total) + this.total) % this.total;
  }
}

export const SIM_DEFAULTS = {
  accel: 1.8, // m/s²
  brake: 1.8, // planned braking for lights and corners, m/s²
  maxBrake: 3.5, // hard limit
  cornerSpeed: 6.5, // m/s through a right-angle turn
  cornerDeg: 35, // heading change that counts as a corner
  stopGapM: 1.2, // stop this far before the light point
  headingRate: 2.5, // 1/s — how fast the car body follows the road
  seed: 42,
};

export class DemoSim {
  constructor({
    route = LV_ROUTE,
    legStarts = LV_LEG_STARTS,
    legSpeeds = LV_LEG_SPEEDS,
    stops = LV_STOPS,
    startDist = null,
    startSpeed = 12,
    ...opts
  } = {}) {
    this.o = { ...SIM_DEFAULTS, ...opts };
    this.route = new LoopRoute(route);
    this.legStarts = legStarts;
    this.legSpeeds = legSpeeds;
    this.rnd = rng(this.o.seed);
    this.stops = stops
      .map(([lat, lon, wait]) => ({ dist: this.route.project(lat, lon), wait }))
      .sort((a, b) => a.dist - b.dist);
    // corners: route vertices where the course turns sharply
    const n = route.length;
    this.corners = [];
    for (let i = 0; i < n; i++) {
      const turn = Math.abs(wrap180(this.route.brg[i] - this.route.brg[(i - 1 + n) % n]));
      if (turn >= this.o.cornerDeg) this.corners.push(this.route.cum[i]);
    }
    // start: rolling north on the Strip, ~35 s before the first red light (camera mode shows up soon)
    this.dist = startDist ?? (this.stops.length > 1 ? this.stops[1].dist - 450 : 0);
    this.speed = startSpeed;
    this.simT = 0;
    this.k = this._nextStopIndex(this.dist);
    this.waitUntil = null;
    this.speedOverride = null;
    this.heading = this.route.at(this.dist).course;
    this.stats = { stopsServed: 0 };
  }

  _nextStopIndex(dist) {
    if (!this.stops.length) return -1;
    let best = 0, bestD = Infinity;
    this.stops.forEach((s, i) => {
      const d = this.route.ahead(dist, s.dist);
      if (d > 0.5 && d < bestD) {
        bestD = d;
        best = i;
      }
    });
    return best;
  }

  legAt(dist) {
    const { i } = this.route.segAt(dist);
    let leg = 0;
    this.legStarts.forEach((s, k) => {
      if (i >= s) leg = k;
    });
    return leg;
  }

  get waiting() {
    return this.waitUntil !== null;
  }

  /** Target speed right now (before acceleration limits). */
  targetSpeed() {
    if (this.speedOverride !== null) return Math.max(0, this.speedOverride);
    if (this.waiting) return 0;
    const o = this.o;
    const leg = this.legAt(this.dist);
    let v = this.legSpeeds[leg] ?? 12;
    v *= 1 + 0.06 * Math.sin(this.simT / 9); // drivers are not cruise controls
    if (this.k >= 0) {
      const d = this.route.ahead(this.dist, this.stops[this.k].dist) - o.stopGapM;
      v = Math.min(v, Math.sqrt(2 * o.brake * Math.max(0, d)));
      if (d > 0.3 && v < 0.4) v = 0.4; // creep up to the line
    }
    for (const c of this.corners) {
      const d = this.route.ahead(this.dist, c);
      if (d < 250) v = Math.min(v, Math.sqrt(o.cornerSpeed ** 2 + 2 * o.brake * d));
    }
    return v;
  }

  step(dt) {
    if (!(dt > 0)) return;
    const o = this.o;
    this.simT += dt;

    if (this.waiting && this.simT >= this.waitUntil) {
      this.waitUntil = null;
      this.k = (this.k + 1) % this.stops.length;
    }
    const target = this.targetSpeed();
    if (this.speed < target) this.speed = Math.min(target, this.speed + o.accel * dt);
    else this.speed = Math.max(target, this.speed - o.maxBrake * dt);

    const before = this.dist;
    this.dist += this.speed * dt;
    if (this.dist > this.route.total * 1000) this.dist -= this.route.total * 1000;

    if (this.k >= 0 && !this.waiting) {
      const stop = this.stops[this.k];
      const was = this.route.ahead(before, stop.dist);
      const now = this.route.ahead(this.dist, stop.dist);
      if (this.speedOverride !== null) {
        if (now > was) this.k = (this.k + 1) % this.stops.length; // drove through (speed override)
      } else if (now <= o.stopGapM + 0.5 && this.speed < 0.5) {
        this.speed = 0;
        this.waitUntil = this.simT + stop.wait;
        this.stats.stopsServed++;
      } else if (now > was) {
        this.k = (this.k + 1) % this.stops.length; // safety: never get stuck behind a passed light
      }
    }

    const course = this.route.at(this.dist).course;
    const diff = wrap180(course - this.heading);
    this.heading = (this.heading + diff * Math.min(1, dt * o.headingRate) + 360) % 360;
  }

  /** Telemetry-like object (fnav/models.py Telemetry minus "display"). */
  telemetry(unixTime, { headingSettings = { gps_above_mps: 3, blend_band_mps: 1 }, paused = false } = {}) {
    const r = this.rnd;
    const p = this.route.at(this.dist);
    const speed = paused ? 0 : Math.max(0, this.speed + (this.speed > 0.2 ? r.gauss(0.05) : 0));
    const gpsCourse = speed > 0.5 ? (this.heading + r.gauss(0.6) + 360) % 360 : null;
    const mag = (this.heading + 3 + r.gauss(1.5) + 360) % 360; // compass a bit off and noisy
    const hs = headingSettings;
    let heading, source;
    if (gpsCourse !== null && speed >= hs.gps_above_mps + hs.blend_band_mps / 2) [heading, source] = [gpsCourse, "gps"];
    else if (gpsCourse === null || speed <= hs.gps_above_mps - hs.blend_band_mps / 2) [heading, source] = [mag, "mag"];
    else [heading, source] = [gpsCourse, "blend"];
    const round = (v, k = 1) => (v === null ? null : Math.round(v * 10 ** k) / 10 ** k);
    return {
      type: "telemetry",
      t: unixTime,
      gps: {
        fix: "3d",
        lat: p.lat + r.gauss(1.5e-6),
        lon: p.lon + r.gauss(1.5e-6),
        alt_m: round(620 + r.gauss(0.4)),
        speed_mps: round(speed, 2),
        course_deg: round(gpsCourse),
        hdop: round(0.7 + r() * 0.2, 2),
        sats_used: 12,
        sats_visible: 14,
        time_utc: new Date(unixTime * 1000).toISOString(),
        age_s: 0.05,
      },
      heading: { deg: round(heading), source, mag_deg: round(mag), gps_course_deg: round(gpsCourse), declination_deg: 11.2 },
      compass: { x: 0, y: 0, z: -45, chip: "demo", ok: true },
      camera: { state: "recording", recording: true, frames_dropped: 0, recordings_count: 42, recordings_gb: 12.5, time_synced: true },
      disk: {},
      system: { internet: true, time_synced: true },
      errors: [],
    };
  }
}
