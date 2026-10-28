// Слой видео с камеры: <img id="camera" src="<root>api/camera/mjpeg"> поверх карты.
// Переход 300 мс (CSS), повтор каждые 2 с при ошибке, надпись "NO CAMERA".
// Поток держится открытым, пока режим «камера», и ещё lingerS после возврата к карте
// (частые переключения на светофорах не переподключают MJPEG), затем отключается — экономия CPU.
//
// Задний вид (map/rearview.js): setCamera("rear") — тот же <img> на api/camera/rear/mjpeg, кадр целиком
// (object-fit: contain — у IP-камеры обычно 16:9), зеркально по display.rear_mirror (CSS только у видео, HUD
// не зеркалится; общее display.mirror_x переворачивает весь #root и складывается с этим само). Надписи заднего
// вида (REAR, нет сигнала) рисует HUD, а не #camera-msg. Пока нового кадра нет, картинка спрятана (.blank):
// после переключения не видно кадра другой камеры, без сигнала — стоп-кадра (setHold).

import { appUrl } from "./root.js";

const nowS = () => (typeof performance !== "undefined" ? performance.now() / 1000 : Date.now() / 1000);

export class CameraLayer {
  constructor(layerEl, imgEl, msgEl, { src = appUrl("api/camera/mjpeg"), rearSrc = null, retryS = 2, lingerS = 30, cacheBust = true } = {}) {
    this.cacheBust = cacheBust; // false: static picture (demo) — let the browser cache it
    this.layer = layerEl;
    this.img = imgEl;
    this.msg = msgEl;
    this.srcs = { front: src, rear: rearSrc }; // rearSrc = null — заднего вида нет (демо): всегда передняя
    this.which = "front";
    this.src = src;
    this.retryS = retryS;
    this.lingerS = lingerS;
    this.shown = false;
    this.shownAt = 0;
    this.suspended = false; // экран «на охране»: поток не нужен вовсе
    this.streaming = false;
    this.ok = false;
    this.loaded = false; // первый кадр этого подключения пришёл (load у MJPEG — один раз, на первый кадр)
    this.failed = false; // подключение не удалось, ждём повтора
    this.connectedAt = 0; // nowS() последнего подключения
    this.mirror = false;
    this.hold = false; // задний вид без сигнала: картинку спрятать
    this.blank = false; // что сейчас стоит в классе .blank
    this.retryTimer = null;
    this.lingerTimer = null;
    this.img.addEventListener("load", () => {
      this.ok = true;
      this.loaded = true;
      this.failed = false;
      this.msg.hidden = true;
      this._syncBlank();
    });
    this.img.addEventListener("error", () => this._onError());
  }

  _connect() {
    clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this.streaming = true;
    this.loaded = false;
    this.connectedAt = nowS();
    this._syncBlank();
    this.img.src = this.cacheBust ? this.src + (this.src.includes("?") ? "&" : "?") + "_=" + Date.now() : this.src;
  }

  _disconnect() {
    clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this.streaming = false;
    this.ok = false;
    this.loaded = false;
    this.failed = false;
    this._syncBlank();
    this.img.removeAttribute("src");
  }

  _onError() {
    if (!this.streaming) return;
    this.ok = false;
    this.loaded = false;
    this.failed = true;
    this.msg.hidden = this.which !== "front"; // задний вид: «нет сигнала» пишет HUD
    this._syncBlank();
    clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(() => {
      if (this.streaming) this._connect();
    }, this.retryS * 1000);
  }

  /** Картинка видна, только когда есть кадр этого подключения и задний вид не «без сигнала». */
  _syncBlank() {
    const b = this.hold || !this.loaded;
    if (b === this.blank) return;
    this.blank = b;
    this.img.classList.toggle("blank", b);
  }

  /**
   * Какая камера в слое: "front" | "rear" (mirror — зеркалить заднюю). Вызывать при каждой телеметрии ДО
   * setVisible (повторные вызовы дёшевы). Смена камеры — сразу другой поток, если слой показан; если поток только
   * держится после возврата к карте — закрыть его (это уже не та камера).
   */
  setCamera(which, mirror = false) {
    const cam = which === "rear" && this.srcs.rear ? "rear" : "front";
    const m = cam === "rear" && !!mirror;
    if (m !== this.mirror) {
      this.mirror = m;
      this.img.classList.toggle("mirror", m);
    }
    if (cam === this.which) return;
    this.which = cam;
    this.src = this.srcs[cam];
    this.img.classList.toggle("rear", cam === "rear");
    this.msg.hidden = true;
    this.failed = false;
    this.hold = false;
    if (this.shown) this._connect();
    else if (this.streaming) this._disconnect();
    else this._syncBlank();
  }

  /** Задний вид без сигнала (map/rearview.js): спрятать картинку, чтобы не висел стоп-кадр. */
  setHold(on) {
    on = !!on;
    if (on === this.hold) return;
    this.hold = on;
    this._syncBlank();
  }

  /** Переподключить поток (статус задней камеры снова «есть видео», а старый поток мог оборваться молча). */
  reconnect() {
    if (this.streaming) this._connect();
  }

  /** Показать/скрыть (вызывать при каждой телеметрии — повторные вызовы дёшевы). */
  setVisible(on, nowS) {
    if (this.suspended) return;
    if (on === this.shown) return;
    this.shown = on;
    this.shownAt = nowS;
    this.layer.classList.toggle("on", on);
    if (on) {
      clearTimeout(this.lingerTimer);
      this.lingerTimer = null;
      if (!this.streaming) this._connect();
    } else {
      clearTimeout(this.lingerTimer);
      this.lingerTimer = setTimeout(() => {
        if (!this.shown) this._disconnect();
      }, this.lingerS * 1000);
    }
  }

  /**
   * Экран «на охране»: спрятать слой и сразу закрыть поток (без задержки lingerS) — ни браузер не декодирует
   * MJPEG, ни fnav-camera его не кодирует. После снятия очередной setVisible() включит камеру как обычно.
   */
  suspend(on) {
    if (on === this.suspended) return;
    this.suspended = on;
    if (!on) return;
    clearTimeout(this.lingerTimer);
    this.lingerTimer = null;
    this.shown = false;
    this.layer.classList.remove("on");
    this._disconnect();
  }

  /** Камера полностью закрыла карту (переход завершён) — карту можно не рисовать. */
  coversMap(nowS) {
    return this.shown && nowS - this.shownAt > 0.4;
  }
}
