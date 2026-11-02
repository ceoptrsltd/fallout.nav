// Хранилище тайлов: LRU в памяти, ограничение параллельных загрузок, отмена ненужных,
// backoff для ошибок. Загрузчик внедряется (в браузере — fetch + createImageBitmap), поэтому
// логика тестируется в node без DOM.

export class TileStore {
  /**
   * @param {object} o
   * @param {(url: string, signal: AbortSignal) => Promise<any>} o.loader — вернуть изображение
   *        (с методом close(), если есть); при ошибке — throw с полем status (404 и т.п.)
   *        и, если сервер прислал Retry-After, retryAfter (с)
   * @param {() => number} o.now — время, с
   */
  constructor({ loader, now, capacity = 300, maxConcurrent = 6, graceS = 1.0, onChange = null } = {}) {
    this.loader = loader;
    this.now = now;
    this.capacity = capacity;
    this.maxConcurrent = maxConcurrent;
    this.graceS = graceS; // сколько держать загрузку, которая перестала быть нужной
    this.onChange = onChange;
    this.cache = new Map(); // key → image (порядок вставки = LRU)
    this.inflight = new Map(); // key → {ctl, lastWanted}
    this.failures = new Map(); // key → {count, retryAt, status}
    this.queue = []; // [{key, url, priority}]
    this.stats = { loaded: 0, failed: 0, aborted: 0, evicted: 0 };
    // сетевые ошибки (сервер недоступен) — общая пауза всех загрузок, а не backoff каждого тайла
    this.pausedUntil = 0;
    this.netBackoff = 0;
  }

  /** Изображение из кэша (с отметкой использования) или null. */
  get(key) {
    const img = this.cache.get(key);
    if (img === undefined) return null;
    this.cache.delete(key);
    this.cache.set(key, img);
    return img;
  }

  has(key) {
    return this.cache.has(key);
  }

  isFailed(key) {
    return this.failures.has(key);
  }

  isLoading(key) {
    return this.inflight.has(key);
  }

  /**
   * Заявить список нужных тайлов (заменяет очередь). Чем меньше priority, тем раньше.
   * Загрузки, не заявленные дольше graceS, отменяются.
   */
  want(reqs) {
    const now = this.now();
    const q = [];
    const seen = new Set();
    for (const r of reqs) {
      if (seen.has(r.key)) continue;
      seen.add(r.key);
      if (this.cache.has(r.key)) continue;
      const fl = this.inflight.get(r.key);
      if (fl) {
        fl.lastWanted = now;
        continue;
      }
      const f = this.failures.get(r.key);
      if (f && now < f.retryAt) continue;
      q.push(r);
    }
    q.sort((a, b) => a.priority - b.priority);
    this.queue = q;
    for (const [key, fl] of this.inflight) {
      if (now - fl.lastWanted > this.graceS) {
        fl.ctl.abort();
        this.inflight.delete(key);
        this.stats.aborted++;
      }
    }
    this.pump();
  }

  pump() {
    if (this.now() < this.pausedUntil) return;
    while (this.inflight.size < this.maxConcurrent && this.queue.length) {
      const r = this.queue.shift();
      if (this.cache.has(r.key) || this.inflight.has(r.key)) continue;
      this._start(r);
    }
  }

  _start(r) {
    const ctl = new AbortController();
    const entry = { ctl, lastWanted: this.now() };
    this.inflight.set(r.key, entry);
    let p;
    try {
      p = Promise.resolve(this.loader(r.url, ctl.signal));
    } catch (e) {
      p = Promise.reject(e);
    }
    p.then(
      (img) => {
        if (this.inflight.get(r.key) !== entry || ctl.signal.aborted) {
          if (img && typeof img.close === "function") img.close();
          return;
        }
        this.inflight.delete(r.key);
        this.failures.delete(r.key);
        this.netBackoff = 0;
        this._put(r.key, img);
        this.stats.loaded++;
        if (this.onChange) this.onChange(r.key);
        this.pump();
      },
      (err) => {
        if (this.inflight.get(r.key) !== entry || ctl.signal.aborted) return;
        this.inflight.delete(r.key);
        if (err && err.status) this._fail(r.key, err);
        else {
          // нет ответа сервера: не помечаем тайл, а приостанавливаем всю загрузку (1 → 30 с)
          if (this.now() >= this.pausedUntil) {
            // одна пауза на «волну» ошибок параллельных загрузок
            this.netBackoff = Math.min(30, this.netBackoff ? this.netBackoff * 2 : 1);
            this.pausedUntil = this.now() + this.netBackoff;
          }
          this.queue.unshift(r);
        }
        this.stats.failed++;
        if (this.onChange) this.onChange(r.key);
        this.pump();
      },
    );
  }

  _put(key, img) {
    const old = this.cache.get(key);
    if (old && old !== img && typeof old.close === "function") old.close();
    this.cache.delete(key);
    this.cache.set(key, img);
    while (this.cache.size > this.capacity) {
      const [k, v] = this.cache.entries().next().value;
      this.cache.delete(k);
      if (v && typeof v.close === "function") v.close();
      this.stats.evicted++;
    }
  }

  _fail(key, err) {
    const status = err && err.status ? err.status : 0;
    const prev = this.failures.get(key);
    const count = (prev ? prev.count : 0) + 1;
    // 404 — такого тайла у Esri нет: повтор не раньше чем через 15 с … 5 мин;
    // 503 — временно (прокси занят, Esri не ответил, нет интернета): скоро, 2 … 15 с, но не раньше Retry-After;
    // прочее — 2 с … 60 с
    const [base, cap] = status === 404 ? [15, 300] : status === 503 ? [2, 15] : [2, 60];
    let delay = Math.min(cap, base * Math.pow(2, count - 1));
    const ra = err && err.retryAfter;
    if (ra > 0) delay = Math.max(delay, Math.min(ra, 300));
    this.failures.delete(key);
    this.failures.set(key, { count, status, retryAt: this.now() + delay });
    if (this.failures.size > 4000) {
      const first = this.failures.keys().next().value;
      this.failures.delete(first);
    }
  }

  /** Сбросить ошибки (например, при восстановлении интернета) — чтобы тайлы запросились снова. */
  resetFailures() {
    this.failures.clear();
    this.pausedUntil = 0;
    this.netBackoff = 0;
  }

  clear() {
    for (const fl of this.inflight.values()) fl.ctl.abort();
    this.inflight.clear();
    for (const img of this.cache.values()) if (img && typeof img.close === "function") img.close();
    this.cache.clear();
    this.queue = [];
  }
}

// Esri, где снимка такого уровня нет, отдаёт 200 и серую «Map data not yet available» (одну и ту же; fnav-core
// узнаёт её сам — fnav/core/tiles.py). Тайлы напрямую с arcgisonline (демо, удалённо) проверяем здесь.
const ESRI_NO_DATA_SIZE = 2521;
const ESRI_NO_DATA_SHA256 = "9eafd300d61393184a4abc1d458564cfd1cd9b6f9c4e9c74687045c0a0e5b858";

/** Заглушка Esri «нет снимка»? (без crypto.subtle — небезопасная страница — только по размеру) */
export async function isEsriNoData(blob) {
  if (!blob || blob.size !== ESRI_NO_DATA_SIZE) return false;
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) return true;
  const d = new Uint8Array(await subtle.digest("SHA-256", await blob.arrayBuffer()));
  return Array.from(d, (b) => b.toString(16).padStart(2, "0")).join("") === ESRI_NO_DATA_SHA256;
}

/** Браузерный загрузчик: fetch (с отменой) + декодирование вне главного потока. */
export async function browserTileLoader(url, signal) {
  const r = await fetch(url, { signal, credentials: "same-origin" });
  if (!r.ok) {
    const e = new Error("HTTP " + r.status);
    e.status = r.status;
    const ra = Number(r.headers.get("Retry-After")); // у 503 — через сколько секунд спросить снова
    if (ra > 0) e.retryAfter = ra;
    throw e;
  }
  const blob = await r.blob();
  if (signal.aborted) throw new DOMException("aborted", "AbortError");
  if (await isEsriNoData(blob)) {
    const e = new Error("Esri has no imagery at this zoom here");
    e.status = 404; // как «тайла нет»: рисуется растянутый тайл уровнем выше
    throw e;
  }
  try {
    if (typeof createImageBitmap === "function") return await createImageBitmap(blob);
    // запасной путь для старых браузеров
    const u = URL.createObjectURL(blob);
    try {
      const img = new Image();
      img.src = u;
      await img.decode();
      return img;
    } finally {
      URL.revokeObjectURL(u);
    }
  } catch (e) {
    if (signal.aborted) throw e;
    const err = new Error("tile decode failed");
    err.status = 415; // битое изображение — backoff тайла, а не пауза сети
    throw err;
  }
}
