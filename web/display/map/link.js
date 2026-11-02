// WebSocket <root>ws с переподключением (backoff 0.5 → 5 с) и сторожем тишины.
//
// Сторож не ждёт onclose от ws.close(): на мёртвом соединении Chromium присылает его только по таймауту
// закрывающего рукопожатия (~60 с), и всё это время экран висел бы на NO LINK (превью админки через сервер).
// Старый сокет отцепляется и бросается, новый открывается сразу.

import { appUrl } from "./root.js";

export class Link {
  constructor(url, { onMessage, onOpen = null, now = () => performance.now() / 1000, silenceS = 5, connectS = 10 } = {}) {
    this.url = url;
    this.onMessage = onMessage;
    this.onOpen = onOpen;
    this.now = now;
    this.silenceS = silenceS;
    this.connectS = connectS; // подключение не открылось за столько — бросить и пробовать снова
    this.ws = null;
    this.delay = 0.5;
    this.lastMsg = now(); // стартовая «фора»: "NO LINK" не раньше чем через 3 с
    this.since = 0; // когда начато подключение / открыт сокет
    this.open = false;
    this.timer = null;
    this.watchdog = null;
    this.drops = 0; // сколько раз сторож бросал соединение
  }

  start() {
    this._connect();
    this.watchdog = setInterval(() => this.check(), 1000);
  }

  /** Сторож (раз в секунду): соединение «висит» без данных (обрыв Wi‑Fi без FIN) или не открывается — заново. */
  check() {
    if (!this.ws) return;
    const now = this.now();
    if (this.open ? now - Math.max(this.lastMsg, this.since) > this.silenceS : now - this.since > this.connectS) {
      this._drop();
    }
  }

  /** Бросить текущий сокет, не дожидаясь onclose, и сразу подключиться заново. */
  _drop() {
    const ws = this.ws;
    this.ws = null;
    this.open = false;
    this.drops++;
    ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
    try {
      ws.close();
    } catch {}
    clearTimeout(this.timer);
    this._connect();
  }

  _connect() {
    this.timer = null;
    let ws;
    try {
      ws = new WebSocket(this.url);
    } catch (e) {
      this._schedule();
      return;
    }
    this.ws = ws;
    this.since = this.now();
    ws.onopen = () => {
      if (this.ws !== ws) return;
      this.open = true;
      this.since = this.now();
      this.delay = 0.5;
      if (this.onOpen) this.onOpen();
    };
    ws.onmessage = (ev) => {
      if (this.ws !== ws) return;
      this.lastMsg = this.now();
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      try {
        this.onMessage(msg);
      } catch (e) {
        console.error("fnav: message handler failed", e);
      }
    };
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this.open = false;
      this.ws = null;
      this._schedule();
    };
    ws.onerror = () => {}; // onclose придёт следом
  }

  _schedule() {
    if (this.timer) return;
    const d = this.delay;
    this.delay = Math.min(5, this.delay * 2);
    this.timer = setTimeout(() => this._connect(), d * 1000);
  }

  /** Есть связь: сокет открыт и данные приходили не дольше 3 с назад. */
  connected(lossS = 3) {
    return this.now() - this.lastMsg < lossS;
  }

  send(obj) {
    if (this.ws && this.open) this.ws.send(JSON.stringify(obj));
  }
}

/** Адрес WebSocket от корня приложения: "ws" → ws(s)://host<root>ws. */
export function wsUrl(path = "ws") {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}${appUrl(path)}`;
}
