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
      heading: this.heading, // сглаженный курс (цифры компаса); последний известный
      headingTarget: this.headingTarget, // отфильтрованный от шума курс без задержки (вход RotationRig)
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
 * Положение машины на экране при переключении ориентации: f = 0 (north_up, центр) …
 * 1 (heading_up, нижняя треть) за durationS со smoothstep.
 */
export class OrientationBlend {
  constructor(durationS = 1.2) {
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

/** Точка машины на холсте для доли f (0 = north_up, 1 = heading_up). */
export function vehicleAnchor(f, w, h, anchorY) {
  return { ax: w / 2, ay: h * (0.5 + f * (anchorY - 0.5)) };
}

/**
 * «Эффект поворота» (docs/contracts/display.md):
 *   * vehicle — курс иконки: быстрое экспоненциальное следование (vehicleLagS);
 *   * bearing — поворот карты: критически демпфированная пружина (без перелёта, мягкий старт
 *     и торможение), ω = 3 / mapLagS → за mapLagS карта проходит ≈ 80 % поворота, за 1.6·mapLagS ≈ 95 %;
 *     в heading_up цель — vehicle, в north_up — 0 (переключение ориентации через ту же пружину).
 *   * deadband: отклонение < deadbandDeg не запускает поворот карты (шум, дрожь руля); если
 *     отклонение держится дольше holdS — карта мягко досинхронизируется до нуля.
 *   * на малой скорости (курс по компасу) всё мягче: иконка до 3× медленнее, карта до 1.7×,
 *     deadband до 2× шире.
 * Все углы — по кратчайшей дуге; bearing хранится «развёрнутым» и нормализуется на выходе.
 */
export const ROTATION_DEFAULTS = {
  vehicleLagS: 0.25,
  mapLagS: 1.5,
  deadbandDeg: 2.0,
  holdS: 1.0,
  slowMps: 3.0, // ниже — курс по компасу, сглаживаем сильнее
  settleDeg: 0.3, // карта «догнала»: ошибка и скорость поворота меньше этого (°, °/с)
};

export class RotationRig {
  constructor(opts = {}) {
    this.o = { ...ROTATION_DEFAULTS };
    this.configure(opts);
    this.orientation = "heading_up";
    this.vehicle = null; // курс иконки, 0..360
    this.bearing = null; // поворот карты (развёрнутый), °
    this.vel = 0; // скорость поворота карты, °/с
    this.tracking = true; // карта догоняет цель (иначе — стоит в пределах deadband)
    this.offTime = 0; // сколько держится отклонение внутри deadband
  }

  configure(opts = {}) {
    for (const [k, v] of Object.entries(opts)) if (Number.isFinite(v)) this.o[k] = v;
  }

  setOrientation(o) {
    const next = o === "north_up" ? "north_up" : "heading_up";
    if (next !== this.orientation) this.tracking = true;
    this.orientation = next;
  }

  /** dt — с; heading — входной курс (null = неизвестен, держим последний); speed — м/с. */
  update(dt, heading, speed = 0) {
    dt = clamp(dt, 0, 0.25);
    const o = this.o;
    const slow = 1 - clamp((speed ?? 0) / o.slowMps, 0, 1); // 1 на стоянке, 0 в движении

    if (Number.isFinite(heading)) {
      if (this.vehicle === null) this.vehicle = norm360(heading);
      else this.vehicle = approachAngle(this.vehicle, heading, dt, o.vehicleLagS * (1 + 2 * slow));
    }
    const veh = this.vehicle ?? 0;
    const target = this.orientation === "heading_up" ? veh : 0;
    if (this.bearing === null) {
      this.bearing = target;
      this.vel = 0;
    }

    const err = angleDiff(this.bearing, target);
    const deadband = o.deadbandDeg * (1 + slow);
    if (this.tracking) {
      if (Math.abs(err) < o.settleDeg && Math.abs(this.vel) < o.settleDeg) {
        this.tracking = false;
        this.offTime = 0;
      }
    } else if (Math.abs(err) >= deadband) {
      this.tracking = true;
    } else if (Math.abs(err) > o.settleDeg) {
      this.offTime += dt;
      if (this.offTime > o.holdS) this.tracking = true; // постоянный перекос — досинхронизировать
    } else {
      this.offTime = 0;
    }

    const lag = o.mapLagS * (1 + 0.7 * slow);
    if (lag <= 0) {
      this.bearing = this.bearing + err;
      this.vel = 0;
    } else {
      // в покое цель = текущее положение: пружина плавно гасит остаточную скорость
      const goal = this.tracking ? this.bearing + err : this.bearing;
      [this.bearing, this.vel] = springStep(this.bearing, this.vel, goal, 3 / lag, dt);
    }
    // нормализация без разрыва: цель всегда берётся относительно текущего значения
    if (this.bearing >= 360 || this.bearing < 0) this.bearing = norm360(this.bearing);

    return { vehicle: veh, bearing: norm360(this.bearing), vehicleRot: norm360(veh - this.bearing) };
  }
}

/**
 * Курс для компаса (view.compassHeadingDeg): минимальная задержка.
 *   * между пакетами 10 Гц — продолжение по устойчивой скорости поворота (два последних шага
 *     в одну сторону; скачок курса не экстраполируется), не дальше одного интервала пакетов;
 *   * экспоненциальное сглаживание с постоянной smoothingS (0 = без сглаживания);
 *   * только защита от дрожания ±jitterDeg, когда поворота нет. Без пружин и deadband.
 */
export class CompassFilter {
  constructor({ smoothingS = 0.1, jitterDeg = 0.3, maxRateDps = 180, minRateDps = 5 } = {}) {
    this.smoothingS = smoothingS;
    this.jitterDeg = jitterDeg;
    this.maxRate = maxRateDps;
    this.minRate = minRateDps;
    this.pk = []; // последние пакеты [{deg, t}] (до 3)
    this.rate = 0; // °/с
    this.value = null; // сглаженный курс
    this.out = null; // выдаваемый курс (с защитой от дрожания)
    this.known = false;
    this.lastUpdate = null;
  }

  configure(smoothingS) {
    if (Number.isFinite(smoothingS) && smoothingS >= 0) this.smoothingS = smoothingS;
  }

  ingest(deg, now) {
    if (!Number.isFinite(deg)) {
      this.known = false;
      this.rate = 0;
      return;
    }
    this.known = true;
    this.pk.push({ deg: norm360(deg), t: now });
    if (this.pk.length > 3) this.pk.shift();
    this.rate = 0;
    if (this.pk.length === 3) {
      const [a, b, c] = this.pk;
      const dt1 = b.t - a.t, dt2 = c.t - b.t;
      if (dt1 > 0.02 && dt1 < 0.5 && dt2 > 0.02 && dt2 < 0.5) {
        const r1 = angleDiff(a.deg, b.deg) / dt1;
        const r2 = angleDiff(b.deg, c.deg) / dt2;
        if (Math.sign(r1) === Math.sign(r2) && Math.abs(r1) > this.minRate && Math.abs(r2) > this.minRate) {
          this.rate = Math.sign(r2) * Math.min(Math.abs(r1), Math.abs(r2), this.maxRate);
        }
      }
    }
    if (this.value === null) this.value = this.out = norm360(deg);
  }

  update(now) {
    const dt = this.lastUpdate === null ? 0 : clamp(now - this.lastUpdate, 0, 0.25);
    this.lastUpdate = now;
    if (!this.pk.length) return null;
    const last = this.pk[this.pk.length - 1];
    const prev = this.pk.length > 1 ? this.pk[this.pk.length - 2] : null;
    const period = prev ? clamp(last.t - prev.t, 0.02, 0.2) : 0.1;
    const target = norm360(last.deg + this.rate * clamp(now - last.t, 0, period));
    this.value = this.smoothingS > 0 ? approachAngle(this.value, target, dt, this.smoothingS) : target;
    if (this.rate !== 0 || Math.abs(angleDiff(this.out, this.value)) >= this.jitterDeg) this.out = this.value;
    return this.out;
  }
}
