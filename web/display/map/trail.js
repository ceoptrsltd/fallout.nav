// След пройденного пути: точки в гео-координатах (merc), проецируются на экран каждый кадр.
//
// Шаг между точками — по экрану (stepPx при текущем масштабе: на трассе с зумом 15 это ~7 м, во дворе на 19 —
// полметра), но не меньше, чем нужно, чтобы maxPoints хватило на весь display.trail_seconds при текущей
// скорости: прежние 2 м при 4000 точках — 320 с на 90 км/ч, и каждый кадр проецировал все 4000.

import { mercDistanceM, mercToScreen } from "./mercator.js";

export class Trail {
  constructor({ stepPx = 2.5, minStepM = 0.5, maxPoints = 4000, fill = 0.9 } = {}) {
    this.stepPx = stepPx; // шаг на экране, px
    this.minStepM = minStepM; // и не меньше (шум позиции на стоянке)
    this.maxPoints = maxPoints;
    this.fill = fill; // на trail_seconds рассчитываем столько от maxPoints (запас на разгоны)
    this.pts = []; // {mx, my, t}
  }

  clear() {
    this.pts.length = 0;
  }

  /**
   * Шаг, м: stepPx экрана при mpp (метров в px), но такой, чтобы за keepS секунд на скорости speed
   * набралось не больше fill·maxPoints точек.
   */
  stepM({ mpp = 0, speed = 0, keepS = 0 } = {}) {
    const byScreen = Number.isFinite(mpp) && mpp > 0 ? this.stepPx * mpp : 0;
    const byCap = Number.isFinite(speed) && speed > 0 && keepS > 0 ? (speed * keepS) / (this.maxPoints * this.fill) : 0;
    return Math.max(this.minStepM, byScreen, byCap);
  }

  /** Добавить текущую позицию (если сдвинулись на шаг). t — секунды; opts — {mpp, speed, keepS} для шага. */
  add(mx, my, t, opts = {}) {
    const last = this.pts[this.pts.length - 1];
    if (last) {
      const d = mercDistanceM(last.mx, last.my, mx, my);
      if (d > 2000) this.clear(); // телепорт — старый след не связываем
      else if (d < this.stepM(opts)) return;
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
