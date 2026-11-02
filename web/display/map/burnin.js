// Защита кинескопа от выжигания (docs/contracts/display.md «Сдвиг от выжигания»): плашки HUD и иконка машины
// часами стоят на одном месте — раз в BURNIN_STEP_MS всё это сдвигается на 2–3 px, не дальше BURNIN_AMP от
// исходного места. Зона безопасности для HUD уже на BURNIN_AMP с каждой стороны, так что плашки из неё не выходят.
// Экран «на охране» сдвигается сам (ui/standby.js standbyDrift).

export const BURNIN_AMP = 3; // px: дальше исходного места ни по x, ни по y
export const BURNIN_STEP_MS = 6 * 60 * 1000; // шаг раз в 6 минут

// обход по кругу: соседние точки (и последняя с первой) — в 2–3 px друг от друга, всё поле ±3 px
const PATH = [[0, 0], [2, -2], [-1, -3], [-3, 0], [-1, 2], [2, 3], [3, 0], [1, -2], [-2, -1], [-2, 2], [0, 3]];

/** Сдвиг HUD и машины в момент ms (Date.now(): по настенному времени — после перезапуска не с нуля). */
export function burnInOffset(ms) {
  const n = Math.floor((Number.isFinite(ms) ? ms : 0) / BURNIN_STEP_MS);
  const [dx, dy] = PATH[((n % PATH.length) + PATH.length) % PATH.length];
  return { dx, dy };
}

/** Прямоугольник, уменьшенный на d с каждой стороны. */
export function insetRect(r, d) {
  return { x: r.x + d, y: r.y + d, w: r.w - 2 * d, h: r.h - 2 * d };
}
