// Модель движения: телеметрия 10 Гц → плавные позиция/курс/скорость/зум на каждый кадр.
// Чистая логика без DOM: время передаётся явно (секунды, монотонные).

import { angleDiff, approachAngle, expAlpha, norm360, springStep } from "./angles.js";
import { clamp, lonLatToMerc, mercToLonLat, metersPerMercUnit } from "./mercator.js";

const DEG = Math.PI / 180;

export const MOTION_DEFAULTS = {
  maxExtrapolateS: 1.0, // дольше без пакетов — не экстраполируем (машина «замирает»)
  snapDistanceM: 300, // скачок больше — телепортируемся без анимации
  posTauMovingS: 0.25, // постоянная времени коррекции позиции в движении
  posTauStillS: 0.8, // … на стоянке
  stillSpeedMps: 0.5, // ниже — считаем, что стоим
  stillDeadbandM: 1.2, // на стоянке ошибки меньше этого игнорируем (шум GPS)
  gpsHeadingMps: 3.0, // от этой скорости курс точный (GPS) — минимум фильтрации
  headingGainStill: 0.2, // доля шага фильтра цели курса на стоянке (компас шумит)
  headingDeadbandStillDeg: 1.0, // на стоянке изменения курса меньше этого игнорируем
  headingTauMovingS: 0.15, // сглаживание отображаемого курса в движении
  headingTauStillS: 0.6, // … на стоянке
  speedTauS: 0.3,
};

export class MotionModel {
  constructor(opts = {}) {
    this.o = { ...MOTION_DEFAULTS, ...opts };
    this.reset();
  }

  reset() {
    this.hasPos = false;
    this.mx = 0;
    this.my = 0; // отображаемая позиция (merc)
    this.meas = null; // {mx, my, vx, vy, t} — последний замер (v — merc/с)
    this.speedMeas = null;
    this.speed = null; // сглаженная скорость, м/с
    this.headingTarget = null; // отфильтрованная цель курса
    this.heading = null; // отображаемый курс
    this.headingKnown = false;
    this.headingSource = "none";
    this.lastUpdate = null;
  }

  /** Принять пакет телеметрии (как пришёл по WS). now — локальное время приёма, с. */
  ingest(tel, now) {
    const gps = tel.gps || {};
    const hd = tel.heading || {};
    const speed = Number.isFinite(gps.speed_mps) ? gps.speed_mps : null;
    this.speedMeas = speed;
    const v = speed ?? 0;
    const hasFix = gps.fix && gps.fix !== "none" && Number.isFinite(gps.lat) && Number.isFinite(gps.lon);

    // ── курс ──
    this.headingSource = hd.source || "none";
    if (Number.isFinite(hd.deg)) {
      const meas = norm360(hd.deg);
      if (this.headingTarget === null) {
        this.headingTarget = meas;
        if (this.heading === null) this.heading = meas;
      } else {
        const diff = angleDiff(this.headingTarget, meas);
        const k = clamp(v / this.o.gpsHeadingMps, 0, 1);
        const gain = this.o.headingGainStill + (1 - this.o.headingGainStill) * k;
        const deadband = this.o.headingDeadbandStillDeg * (1 - k);
        if (Math.abs(diff) > deadband) this.headingTarget = norm360(this.headingTarget + diff * gain);
      }
      this.headingKnown = true;
    } else {
      this.headingKnown = false; // держим последний отображаемый курс, но помечаем неизвестным
    }

    // ── позиция ──
    if (!hasFix) {
      if (this.meas) this.meas = { ...this.meas, vx: 0, vy: 0 };
      return;
    }
    const m = lonLatToMerc(gps.lon, gps.lat);
    let vx = 0, vy = 0;
    if (v >= this.o.stillSpeedMps) {
      const course = Number.isFinite(gps.course_deg) ? gps.course_deg : Number.isFinite(hd.deg) ? hd.deg : null;
      if (course !== null) {
        const k = v / metersPerMercUnit(gps.lat);
        vx = Math.sin(course * DEG) * k;
        vy = -Math.cos(course * DEG) * k;
      }
    }
    this.meas = { mx: m.x, my: m.y, vx, vy, t: now, lat: gps.lat };
    if (!this.hasPos || this._errM(now) > this.o.snapDistanceM) {
      this.mx = m.x;
      this.my = m.y;
      this.hasPos = true;
    }
    if (this.speed === null && speed !== null) this.speed = speed;
  }

  _target(now) {
    const s = this.meas;
    const age = clamp(now - s.t, 0, this.o.maxExtrapolateS);
    return { x: s.mx + s.vx * age, y: s.my + s.vy * age, age: now - s.t };
  }

  _errM(now) {
    const t = this._target(now);
    return Math.hypot(t.x - this.mx, t.y - this.my) * metersPerMercUnit(this.meas.lat);
  }

  /** Продвинуть модель к моменту now (вызывать каждый кадр). */
  update(now) {
    const dt = this.lastUpdate === null ? 0 : clamp(now - this.lastUpdate, 0, 0.25);
    this.lastUpdate = now;
    const v = this.speedMeas ?? 0;
    const moving = v >= this.o.stillSpeedMps;

    if (this.hasPos && this.meas) {
      const t = this._target(now);
      // предсказание (экстраполяция по скорости и курсу) + мягкая коррекция к замеру
      if (t.age < this.o.maxExtrapolateS) {
        this.mx += this.meas.vx * dt;
        this.my += this.meas.vy * dt;
      }
      const mpu = metersPerMercUnit(this.meas.lat);
      const errM = Math.hypot(t.x - this.mx, t.y - this.my) * mpu;
      if (moving || errM > this.o.stillDeadbandM) {
        const a = expAlpha(dt, moving ? this.o.posTauMovingS : this.o.posTauStillS);
        this.mx += (t.x - this.mx) * a;
        this.my += (t.y - this.my) * a;
      }
    }

    if (this.headingTarget !== null) {
      const k = clamp(v / this.o.gpsHeadingMps, 0, 1);
      const tau = this.o.headingTauStillS + (this.o.headingTauMovingS - this.o.headingTauStillS) * k;
      this.heading = approachAngle(this.heading ?? this.headingTarget, this.headingTarget, dt, tau);
    }

    if (this.speedMeas !== null) {
      this.speed = this.speed === null ? this.speedMeas : this.speed + (this.speedMeas - this.speed) * expAlpha(dt, this.o.speedTauS);
    } else {
      this.speed = null;
    }

    const ll = this.hasPos ? mercToLonLat(this.mx, this.my) : null;
    return {
      hasPos: this.hasPos,
      mx: this.mx,
      my: this.my,
      lat: ll ? ll.lat : null,
      lon: ll ? ll.lon : null,
      heading: this.heading, // последний известный (для поворота карты)
      headingKnown: this.headingKnown && this.heading !== null,
      headingSource: this.headingSource,
      speed: this.speed,
    };
  }
}

/** Плавный зум: критически демпфированная пружина к цели (~1.5 с до установления). */
export class ZoomAnimator {
  constructor(omega = 4.0) {
    this.omega = omega;
    this.z = null;
    this.v = 0;
    this.target = null;
  }

  setTarget(z) {
    this.target = z;
    if (this.z === null) this.z = z;
  }

  update(dt) {
    if (this.z === null || this.target === null) return this.z;
    const [z, v] = springStep(this.z, this.v, this.target, this.omega, clamp(dt, 0, 0.25));
    this.z = z;
    this.v = v;
    if (Math.abs(this.z - this.target) < 1e-4 && Math.abs(this.v) < 1e-4) {
      this.z = this.target;
      this.v = 0;
    }
    return this.z;
  }

  get settled() {
    return this.z === this.target && this.v === 0;
  }
}

/**
 * Плавное переключение ориентации: f = 0 (north_up) … 1 (heading_up) за durationS со smoothstep.
 */
export class OrientationBlend {
  constructor(durationS = 0.8) {
    this.dur = durationS;
    this.raw = null; // линейный прогресс 0..1
    this.goal = 1;
  }

  set(orientation) {
    this.goal = orientation === "north_up" ? 0 : 1;
    if (this.raw === null) this.raw = this.goal;
  }

  update(dt) {
    if (this.raw === null) this.raw = this.goal;
    const step = dt / this.dur;
    if (this.raw < this.goal) this.raw = Math.min(this.goal, this.raw + step);
    else if (this.raw > this.goal) this.raw = Math.max(this.goal, this.raw - step);
    const x = this.raw;
    return x * x * (3 - 2 * x);
  }
}

/**
 * Поворот карты и положение машины для доли f (0 = north_up, 1 = heading_up).
 * bearing — на сколько повёрнута карта; vehicleRot — поворот иконки на экране (0 = вверх).
 */
export function orientationPose(f, headingDeg, w, h, anchorY) {
  const hdg = headingDeg ?? 0;
  const bearing = norm360(f * angleDiff(0, hdg));
  return {
    bearing,
    vehicleRot: norm360(hdg - bearing),
    ax: w / 2,
    ay: h * (0.5 + f * (anchorY - 0.5)),
  };
}
