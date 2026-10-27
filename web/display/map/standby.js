// Экран «на охране» (settings.display.armed_standby, docs/contracts/display.md): когда его показывать.
// Только «armed»: «arming» (отсчёт), «triggered» (сработала — нужна плашка) и «off» — обычный экран.

/** Показывать экран «на охране» вместо карты и HUD. */
export function armedStandby(telemetry, settings) {
  return settings?.display?.armed_standby !== false && telemetry?.alarm?.state === "armed";
}

/** Через сколько мс перерисовать экран «на охране»: к началу следующей секунды (часы меняются ровно в минуту). */
export function standbyDelayMs(nowMs) {
  return 1000 - (((nowMs % 1000) + 1000) % 1000) + 15;
}
