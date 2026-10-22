// Меню игр на экране машины: геймпад (Gamepad API Chromium), долгое нажатие PS — открыть,
// крестовина/стик — выбрать, ✕ — играть, ○ / PS — закрыть. Запуск — POST api/games/launch: fnav-core
// останавливает этот экран и запускает RetroArch (служба fnav-games); после выхода из игры экран вернётся сам.
// Только на самом устройстве: не в демо и не в превью через сервер.

const HOLD_OPEN_S = 0.8; // столько держать PS, чтобы открыть меню
const REPEAT_FIRST_S = 0.35;
const REPEAT_S = 0.15;
const STICK = 0.6;
const MAX_SPEED_MPS = 2.0;

/** Кнопки геймпада в одном виде для «standard» и «сырой» раскладки (PS4-совместимый, порядок DualShock 4). */
export function readPad(p) {
  const b = (i) => !!(p.buttons[i] && p.buttons[i].pressed);
  const ax = (i) => (Number.isFinite(p.axes[i]) ? p.axes[i] : 0);
  if (p.mapping === "standard") {
    return {
      ok: b(0), back: b(1), home: b(16),
      up: b(12) || ax(1) < -STICK, down: b(13) || ax(1) > STICK,
    };
  }
  // сырая раскладка: кнопки по порядку evdev (0 □, 1 ✕, 2 ○, 3 △, … 12 PS); крестовина — оси-хатки 6/7
  return {
    ok: b(1), back: b(2), home: b(12),
    up: ax(7) < -0.5 || ax(1) < -STICK, down: ax(7) > 0.5 || ax(1) > STICK,
  };
}

export class GamesMenu {
  /**
   * @param {HTMLElement} root — #root экрана (768×576, с PAL-сжатием и зеркалом)
   * @param {(path: string) => string} url — адрес от корня приложения
   */
  constructor(root, url) {
    this.url = url;
    this.open = false;
    this.games = [];
    this.sel = 0;
    this.busy = false;
    this.holdSince = null;
    this.prev = {};
    this.nextRepeat = 0;
    this.el = document.createElement("div");
    this.el.id = "games-menu";
    this.el.hidden = true;
    root.appendChild(this.el);
  }

  /** Каждый кадр: опрос геймпадов. t — секунды, speed — м/с из телеметрии. */
  poll(t, speed) {
    const pads = navigator.getGamepads ? [...navigator.getGamepads()].filter(Boolean) : [];
    if (!pads.length) { this.holdSince = null; return; }
    const s = pads.map(readPad).reduce((a, c) => {
      for (const k in c) a[k] = a[k] || c[k];
      return a;
    }, {});
    const pressed = (k) => s[k] && !this.prev[k];

    if (!this.open) {
      if (s.home) {
        this.holdSince ??= t;
        if (t - this.holdSince >= HOLD_OPEN_S) {
          this.holdSince = Infinity; // до отпускания — не повторять
          this.show(speed);
        }
      } else {
        this.holdSince = null;
      }
    } else if (!this.busy) {
      if (pressed("back") || pressed("home")) this.hide();
      else if (pressed("ok")) this.launch();
      else {
        const dir = s.up ? -1 : s.down ? 1 : 0;
        const fresh = (s.up && !this.prev.up) || (s.down && !this.prev.down);
        if (dir && (fresh || t >= this.nextRepeat)) {
          this.nextRepeat = t + (fresh ? REPEAT_FIRST_S : REPEAT_S);
          this.move(dir);
        }
      }
    }
    if (!s.home && this.holdSince === Infinity) this.holdSince = null;
    this.prev = s;
  }

  async show(speed) {
    this.open = true;
    this.busy = false;
    this.el.hidden = false;
    if (Number.isFinite(speed) && speed > MAX_SPEED_MPS) {
      this.render({ message: "STOP THE VEHICLE TO PLAY", warn: true });
      setTimeout(() => this.hide(), 2500);
      this.busy = true;
      return;
    }
    this.render({ message: "READING CARTRIDGES…" });
    try {
      const r = await fetch(this.url("api/games"), { cache: "no-store" });
      const d = await r.json();
      this.games = d.games || [];
      this.sel = Math.min(this.sel, Math.max(0, this.games.length - 1));
      this.render(this.games.length ? {} : { message: "NO GAMES INSTALLED", warn: true });
    } catch {
      this.render({ message: "NO CONNECTION TO THE CORE", warn: true });
    }
  }

  hide() {
    this.open = false;
    this.busy = false;
    this.el.hidden = true;
  }

  move(dir) {
    if (!this.games.length) return;
    this.sel = (this.sel + dir + this.games.length) % this.games.length;
    this.render({});
  }

  async launch() {
    const g = this.games[this.sel];
    if (!g) return;
    this.busy = true;
    this.render({ message: `LOADING ${g.title.toUpperCase()}…` });
    try {
      const r = await fetch(this.url("api/games/launch"), {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: g.id }),
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        throw new Error(d.detail || `HTTP ${r.status}`);
      }
      // дальше экран остановит systemd: на его месте запустится игра
    } catch (e) {
      this.busy = false;
      this.render({ message: String(e.message || e).toUpperCase(), warn: true });
    }
  }

  render({ message = "", warn = false }) {
    const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
    const rows = this.games.map((g, i) =>
      `<li class="${i === this.sel ? "sel" : ""}"><span class="mark">${i === this.sel ? "▶" : ""}</span>` +
      `<span class="title">${esc(g.title)}</span><span class="sys">${esc(g.system)}</span></li>`).join("");
    this.el.innerHTML =
      `<div class="gm-panel"><div class="gm-head"><span class="gm-title">GAMES</span><span class="gm-sub">HOLOTAPE LIBRARY</span></div>` +
      (message ? `<div class="gm-msg${warn ? " warn" : ""}">${esc(message)}</div>` : `<ul class="gm-list">${rows}</ul>`) +
      `<div class="gm-foot"><span>✕ PLAY</span><span>○ BACK</span><span>SHARE + OPTIONS = EXIT GAME</span></div></div>`;
  }
}
