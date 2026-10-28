// Задний вид на экране в машине (docs/contracts/camera.md «Задняя камера»): какую камеру показывает слой видео,
// зеркалить ли её и есть ли картинка. Клавиша 4 / кнопка GPS-модуля → fnav-core: display.camera = "rear",
// display.mode = "camera", reason = "rear"; telemetry.camera_rear — статус задней (null — выключена).
//
// Картинку задней камеры прячем (вместо стоп-кадра — надпись HUD), когда:
//  - статус говорит, что видео нет: null / disabled / stopped / error без кадров / recording, но кадров нет
//    дольше stallS (fps_actual = null: сервис заметил бы это сам только через 15 с);
//  - <img> не подключился (ошибка, повтор каждые 2 с) или ещё не получил первый кадр.
// Chromium шлёт load у MJPEG один раз — на первый кадр подключения; оборванный поток ни load, ни error не шлёт
// (остаётся последний кадр), поэтому, когда статус снова «есть видео», поток переподключаем (recovered).

/** Первый кадр после подключения: столько секунд просто чёрный экран, без надписи (не мигать CONNECTING). */
export const REAR_WAIT_S = 1.5;
/** recording, а fps_actual = null (нет кадров ≥ 3 с) дольше этого — нет сигнала; один опрос (1 с) — ещё нет. */
export const REAR_STALL_S = 1.5;

/** Какую камеру показывает слой видео: "rear" — только задний вид в режиме камеры. */
export function viewCamera(telemetry) {
  const d = telemetry?.display;
  return d?.mode === "camera" && d?.camera === "rear" ? "rear" : "front";
}

/** Зеркалить видео задней камеры (display.rear_mirror; нет поля — да, как rear_camera.mirror по умолчанию). */
export function rearMirror(telemetry) {
  return telemetry?.display?.rear_mirror !== false;
}

/**
 * Что статус задней камеры говорит о видео: "ok" | "starting" | "down" | "stall" (recording без кадров — решает
 * RearSignal по времени). error с fps_actual — ошибка записи (нет места, сбой диска), а кадры идут: картинка есть.
 */
export function rearStatusKind(st) {
  if (!st || typeof st !== "object") return "down";
  switch (st.state) {
    case "live":
      return "ok";
    case "recording":
      return st.fps_actual == null ? "stall" : "ok";
    case "starting":
      return "starting";
    case "error":
      return st.fps_actual != null ? "ok" : "down";
    default:
      return "down"; // stopped, disabled, неизвестное
  }
}

/**
 * Сигнал заднего вида для HUD и слоя видео (вызывать каждый кадр, пока показан задний вид).
 * update() → "ok" (картинка) | "wait" (ждём первый кадр: чёрный, без надписи) | "connecting" | "no_signal".
 * После update() recovered = true, если статус только что снова «есть видео» — переподключить поток.
 */
export class RearSignal {
  constructor({ waitS = REAR_WAIT_S, stallS = REAR_STALL_S } = {}) {
    this.waitS = waitS;
    this.stallS = stallS;
    this.reset();
  }

  /** Задний вид закрыт — следующий начнётся с чистого листа. */
  reset() {
    this.stallSince = null;
    this.statusOk = true; // первое «плохо» после входа — тоже переход (потом «хорошо» переподключит)
    this.recovered = false;
  }

  /**
   * status — telemetry.camera_rear; img — {loaded, failed, connectedAt} слоя видео (map/camera.js);
   * now — секунды в тех же часах, что connectedAt (performance.now() / 1000).
   */
  update(status, img, now) {
    let kind = rearStatusKind(status);
    if (kind === "stall") {
      if (this.stallSince === null) this.stallSince = now;
      kind = now - this.stallSince >= this.stallS ? "down" : "ok";
    } else {
      this.stallSince = null;
    }
    const ok = kind === "ok";
    this.recovered = ok && !this.statusOk;
    this.statusOk = ok;
    if (kind === "down" || img?.failed) return "no_signal";
    if (kind === "starting") return "connecting";
    if (this.recovered) return "wait"; // вызывающий переподключает поток — ждём его первый кадр
    if (!img?.loaded) return now - (img?.connectedAt ?? now) < this.waitS ? "wait" : "connecting";
    return "ok";
  }
}
