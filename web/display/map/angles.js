// Углы и сглаживание — чистые функции, без DOM.

export function norm360(a) {
  const r = a % 360;
  return r < 0 ? r + 360 : r + 0; // +0: без «-0»
}

/** В диапазон (-180, 180]. */
export function wrap180(a) {
  let r = norm360(a);
  if (r > 180) r -= 360;
  return r;
}

/** Кратчайшая разность to - from, (-180, 180]. */
export function angleDiff(from, to) {
  return wrap180(to - from);
}

/** Линейная интерполяция угла по кратчайшей дуге. */
export function lerpAngle(a, b, t) {
  return norm360(a + angleDiff(a, b) * t);
}

/** Доля приближения экспоненциального фильтра за dt при постоянной времени tau. */
export function expAlpha(dt, tau) {
  if (tau <= 0) return 1;
  return 1 - Math.exp(-dt / tau);
}

/** Шаг экспоненциального приближения угла к цели по кратчайшей дуге. */
export function approachAngle(cur, target, dt, tau) {
  return norm360(cur + angleDiff(cur, target) * expAlpha(dt, tau));
}

export function lerp(a, b, t) {
  return a + (b - a) * t;
}

export function smoothstep(t) {
  const x = t < 0 ? 0 : t > 1 ? 1 : t;
  return x * x * (3 - 2 * x);
}

/**
 * Критически демпфированная пружина (точное решение): плавный старт и мягкая остановка без
 * перелёта. omega — собственная частота, 1/с (время установления ≈ 6/omega).
 * Возвращает [x, v].
 */
export function springStep(x, v, target, omega, dt) {
  const d = x - target;
  const e = Math.exp(-omega * dt);
  const c = v + omega * d;
  const nd = (d + c * dt) * e;
  const nv = (v - omega * c * dt) * e;
  return [target + nd, nv];
}
