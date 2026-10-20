// WebSocket /ws с переподключением (backoff 0.5 → 5 с) и сторожем тишины.

export class Link {
  constructor(url, { onMessage, onOpen = null, now = () => performance.now() / 1000, silenceS = 5 } = {}) {
    this.url = url;
    this.onMessage = onMessage;
    this.onOpen = onOpen;
    this.now = now;
    this.silenceS = silenceS;
    this.ws = null;
    this.delay = 0.5;
    this.lastMsg = now(); // стартовая «фора»: "NO LINK" не раньше чем через 3 с
    this.open = false;
    this.timer = null;
    this.watchdog = null;
  }

  start() {
    this._connect();
    this.watchdog = setInterval(() => {
      // соединение «висит» без данных (обрыв Wi‑Fi без FIN) — переподключаемся
      if (this.ws && this.open && this.now() - this.lastMsg > this.silenceS) {
        try {
          this.ws.close();
        } catch {}
      }
    }, 1000);
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
    ws.onopen = () => {
      this.open = true;
      this.delay = 0.5;
      if (this.onOpen) this.onOpen();
    };
    ws.onmessage = (ev) => {
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

export function wsUrl(path = "/ws") {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}${path}`;
}
