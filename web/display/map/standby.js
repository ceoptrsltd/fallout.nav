// Экран «на охране» (settings.display.armed_standby, docs/contracts/display.md): когда его показывать.
// Только «armed»: «arming» (отсчёт), «triggered» (сработала — нужна плашка) и «off» — обычный экран.

/** Показывать экран «на охране» вместо карты и HUD. Задний вид, включённый кнопкой (display.camera = "rear"), —
 *  просьба показать, что за фургоном (ночью внутри): экран просыпается; кнопка ещё раз — снова «на охране». */
export function armedStandby(telemetry, settings) {
  return settings?.display?.armed_standby !== false && telemetry?.alarm?.state === "armed"
    && telemetry?.display?.camera !== "rear";
}

/** Через сколько мс перерисовать экран «на охране»: к началу следующей секунды (часы меняются ровно в минуту). */
export function standbyDelayMs(nowMs) {
  return 1000 - (((nowMs % 1000) + 1000) % 1000) + 15;
}
