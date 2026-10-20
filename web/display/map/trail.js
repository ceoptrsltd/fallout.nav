// След пройденного пути: точки в гео-координатах (merc), проецируются на экран каждый кадр.

import { mercDistanceM, mercToScreen } from "./mercator.js";

export class Trail {
  constructor({ minStepM = 2.0, minStepSlowM = 0.5, slowIntervalS = 1.0, maxPoints = 4000 } = {}) {
    this.minStepM = minStepM;
    this.minStepSlowM = minStepSlowM;
    this.slowIntervalS = slowIntervalS;
    this.maxPoints = maxPoints;
    this.pts = []; // {mx, my, t}
  }

  clear() {
    this.pts.length = 0;
  }

  /** Добавить текущую позицию (если сдвинулись достаточно). t — секунды. */
  add(mx, my, t) {
    const last = this.pts[this.pts.length - 1];
    if (last) {
      const d = mercDistanceM(last.mx, last.my, mx, my);
      if (d > 2000) this.clear(); // телепорт — старый след не связываем
      else if (d < this.minStepM && !(t - last.t >= this.slowIntervalS && d >= this.minStepSlowM)) return;
    }
    this.pts.push({ mx, my, t });
    if (this.pts.length > this.maxPoints) this.pts.splice(0, this.pts.length - this.maxPoints);
  }

  /** Удалить точки старше maxAgeS. */
  prune(t, maxAgeS) {
    let i = 0;
    while (i < this.pts.length && t - this.pts[i].t > maxAgeS) i++;
    if (i) this.pts.splice(0, i);
  }

  /** Экранные точки [{x, y, age_s}] для вида view; head — текущая позиция машины (последняя точка). */
  project(view, t, head) {
    const out = new Array(this.pts.length);
    for (let i = 0; i < this.pts.length; i++) {
      const p = this.pts[i];
      const s = mercToScreen(view, p.mx, p.my);
      out[i] = { x: s.x, y: s.y, age_s: t - p.t };
    }
    if (head) {
      const s = mercToScreen(view, head.mx, head.my);
      out.push({ x: s.x, y: s.y, age_s: 0 });
    }
    return out;
  }
}
