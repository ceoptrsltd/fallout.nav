// Слой видео с камеры: <img id="camera" src="/api/camera/mjpeg"> поверх карты.
// Переход 300 мс (CSS), повтор каждые 2 с при ошибке, надпись "NO CAMERA".
// Поток держится открытым, пока режим «камера», и ещё lingerS после возврата к карте
// (частые переключения на светофорах не переподключают MJPEG), затем отключается — экономия CPU.

export class CameraLayer {
  constructor(layerEl, imgEl, msgEl, { src = "/api/camera/mjpeg", retryS = 2, lingerS = 30 } = {}) {
    this.layer = layerEl;
    this.img = imgEl;
    this.msg = msgEl;
    this.src = src;
    this.retryS = retryS;
    this.lingerS = lingerS;
    this.shown = false;
    this.shownAt = 0;
    this.streaming = false;
    this.ok = false;
    this.retryTimer = null;
    this.lingerTimer = null;
    this.img.addEventListener("load", () => {
      this.ok = true;
      this.msg.hidden = true;
    });
    this.img.addEventListener("error", () => this._onError());
  }

  _connect() {
    clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this.streaming = true;
    this.img.src = this.src + (this.src.includes("?") ? "&" : "?") + "_=" + Date.now();
  }

  _disconnect() {
    clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this.streaming = false;
    this.ok = false;
    this.img.removeAttribute("src");
  }

  _onError() {
    if (!this.streaming) return;
    this.ok = false;
    this.msg.hidden = false;
    clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(() => {
      if (this.streaming) this._connect();
    }, this.retryS * 1000);
  }

  /** Показать/скрыть (вызывать при каждой телеметрии — повторные вызовы дёшевы). */
  setVisible(on, nowS) {
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

  /** Камера полностью закрыла карту (переход завершён) — карту можно не рисовать. */
  coversMap(nowS) {
    return this.shown && nowS - this.shownAt > 0.4;
  }
}
