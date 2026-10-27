// Холст, который перерисовывается, только когда изменилось то, что на нём нарисовано.
//
// Кадр рисуется не на холст, а в RecordingContext: тот лишь записывает вызовы Canvas 2D (состояние —
// трансформация, alpha, шрифт… — ведёт сам, чтобы чтение свойств работало как у настоящего контекста;
// measureText — у настоящего контекста). RetainedLayer.end() сравнивает запись с прошлой: те же вызовы с
// теми же аргументами (картинки — те же неизменяемые спрайты) → холст не трогаем, на нём уже ровно это.
// Иначе — очищаем и проигрываем запись на настоящий контекст: пиксели те же, что при прямом рисовании.
//
// Изменяемые источники (drawImage не из кэша спрайтов, Path2D, объекты в аргументах) сравнить нельзя —
// такой кадр проигрывается всегда. Страховка: раз в refreshS проигрываем в любом случае; загрузка шрифтов
// (document.fonts) — тоже повод перерисовать.

let epoch = 0; // меняется, когда тот же текст может нарисоваться иначе (догрузились шрифты)
if (typeof document !== "undefined" && document.fonts && typeof document.fonts.addEventListener === "function") {
  document.fonts.addEventListener("loadingdone", () => epoch++);
}
/** Сбросить все сравнения (например, после загрузки шрифтов HUD). */
export function bumpEpoch() {
  epoch++;
}

/** Картинка не меняется после создания: ImageBitmap или холст спрайта с пометкой fnavStatic (ui/theme.js). */
export function isStaticImage(img) {
  if (!img || typeof img !== "object") return false;
  if (img.fnavStatic === true) return true;
  return typeof ImageBitmap !== "undefined" && img instanceof ImageBitmap;
}

const PROPS = {
  globalAlpha: 1, globalCompositeOperation: "source-over", fillStyle: "#000000", strokeStyle: "#000000",
  lineWidth: 1, lineCap: "butt", lineJoin: "miter", miterLimit: 10, lineDashOffset: 0,
  font: "10px sans-serif", textAlign: "start", textBaseline: "alphabetic", direction: "inherit",
  letterSpacing: "0px", wordSpacing: "0px", fontKerning: "auto", fontStretch: "normal", fontVariantCaps: "normal",
  textRendering: "auto", lang: "inherit", imageSmoothingEnabled: true, imageSmoothingQuality: "low",
  shadowBlur: 0, shadowColor: "rgba(0, 0, 0, 0)", shadowOffsetX: 0, shadowOffsetY: 0, filter: "none",
};
// числовые свойства: недопустимое значение настоящий контекст молча игнорирует — так же и здесь
const NUM_OK = {
  globalAlpha: (v) => v >= 0 && v <= 1,
  lineWidth: (v) => v > 0,
  miterLimit: (v) => v > 0,
  shadowBlur: (v) => v >= 0,
  lineDashOffset: () => true,
  shadowOffsetX: () => true,
  shadowOffsetY: () => true,
};
const TEXT_PROPS = ["font", "letterSpacing", "wordSpacing", "fontKerning", "fontStretch", "fontVariantCaps",
  "textRendering", "direction", "lang", "textAlign", "textBaseline"];
const SET = {}; // имя свойства → код операции
for (const p of Object.keys(PROPS)) SET[p] = "=" + p;
const PROP_OF = new Map(Object.keys(PROPS).map((p) => ["=" + p, p]));

function freshState() {
  return { ...PROPS, m: [1, 0, 0, 1, 0, 0], dash: [] };
}

class RecGradient {
  constructor(rec, code, args) {
    this.rec = rec;
    this.code = code;
    this.args = args;
    this.stops = [];
    this.frame = -1;
    this.idx = -1;
  }
  addColorStop(offset, color) {
    this.stops.push(offset, color);
    if (this.frame === this.rec.frame) this.rec._push3("stop", this.idx, offset, color);
  }
}

class RecPattern {
  constructor(rec, image, repetition) {
    this.rec = rec;
    this.image = image;
    this.repetition = repetition;
    this.frame = -1;
    this.idx = -1;
  }
  setTransform() {
    this.rec.volatile = true; // трансформация шаблона не записывается — сравнивать нельзя
  }
}

/** Контекст-«магнитофон» с API CanvasRenderingContext2D. real — настоящий контекст (canvas, measureText). */
export class RecordingContext {
  constructor(real) {
    this.real = real;
    this.ops = [];
    this.volatile = false;
    this.frame = 0;
    this.nobj = 0; // градиенты/шаблоны этого кадра
    this.s = freshState();
    this.stack = [];
  }

  _begin() {
    this.ops.length = 0;
    this.volatile = false;
    this.frame++;
    this.nobj = 0;
    this.s = freshState();
    this.stack.length = 0;
  }

  get canvas() {
    return this.real ? this.real.canvas : null;
  }

  // ── запись ──
  _push0(code) {
    this.ops.push(code, 0);
  }
  _push1(code, a) {
    this.ops.push(code, 1, a);
  }
  _push2(code, a, b) {
    this.ops.push(code, 2, a, b);
  }
  _push3(code, a, b, c) {
    this.ops.push(code, 3, a, b, c);
  }
  _push4(code, a, b, c, d) {
    this.ops.push(code, 4, a, b, c, d);
  }
  _pushN(code, args) {
    const o = this.ops;
    o.push(code, args.length);
    for (let i = 0; i < args.length; i++) {
      const a = args[i];
      if (a !== null && (typeof a === "object" || typeof a === "function")) this.volatile = true;
      o.push(a);
    }
  }

  _objIndex(g, kind) {
    if (g.frame !== this.frame) {
      // создан в другом кадре (кэш модуля) — заново описать его в этом
      g.frame = this.frame;
      g.idx = this.nobj++;
      if (kind === "grad") {
        this._pushN(g.code, g.args);
        for (let i = 0; i < g.stops.length; i += 2) this._push3("stop", g.idx, g.stops[i], g.stops[i + 1]);
      } else {
        if (!isStaticImage(g.image)) this.volatile = true;
        this.ops.push("PAT", 2, g.image, g.repetition);
      }
    }
    return g.idx;
  }

  _setStyle(prop, v) {
    if (v instanceof RecGradient && v.rec === this) {
      this._push1(prop === "fillStyle" ? "=fillStyleG" : "=strokeStyleG", this._objIndex(v, "grad"));
    } else if (v instanceof RecPattern && v.rec === this) {
      this._push1(prop === "fillStyle" ? "=fillStyleG" : "=strokeStyleG", this._objIndex(v, "pat"));
    } else {
      if (v !== null && typeof v === "object") this.volatile = true;
      this._push1(SET[prop], v);
    }
    this.s[prop] = v;
  }

  // ── состояние ──
  save() {
    this.stack.push({ ...this.s, m: this.s.m.slice(), dash: this.s.dash.slice() });
    this._push0("save");
  }
  restore() {
    const st = this.stack.pop();
    if (st) this.s = st;
    this._push0("restore");
  }
  reset() {
    this.s = freshState();
    this.stack.length = 0;
    this._push0("reset");
  }
  isContextLost() {
    return this.real && typeof this.real.isContextLost === "function" ? this.real.isContextLost() : false;
  }
  getContextAttributes() {
    return this.real && typeof this.real.getContextAttributes === "function" ? this.real.getContextAttributes() : {};
  }

  // ── трансформация (ведём матрицу для getTransform) ──
  _mul(a, b, c, d, e, f) {
    const m = this.s.m;
    const [A, B, C, D, E, F] = m;
    m[0] = A * a + C * b;
    m[1] = B * a + D * b;
    m[2] = A * c + C * d;
    m[3] = B * c + D * d;
    m[4] = A * e + C * f + E;
    m[5] = B * e + D * f + F;
  }
  translate(x, y) {
    this._mul(1, 0, 0, 1, x, y);
    this._push2("translate", x, y);
  }
  rotate(a) {
    const c = Math.cos(a), s = Math.sin(a);
    this._mul(c, s, -s, c, 0, 0);
    this._push1("rotate", a);
  }
  scale(x, y) {
    this._mul(x, 0, 0, y, 0, 0);
    this._push2("scale", x, y);
  }
  transform(a, b, c, d, e, f) {
    this._mul(a, b, c, d, e, f);
    this.ops.push("transform", 6, a, b, c, d, e, f);
  }
  setTransform(a, b, c, d, e, f) {
    if (a !== null && typeof a === "object") {
      const m = a;
      this.s.m = [m.a ?? m.m11 ?? 1, m.b ?? m.m12 ?? 0, m.c ?? m.m21 ?? 0, m.d ?? m.m22 ?? 1, m.e ?? m.m41 ?? 0, m.f ?? m.m42 ?? 0];
      this._pushN("setTransform", [a]);
      return;
    }
    if (arguments.length === 0) {
      this.s.m = [1, 0, 0, 1, 0, 0];
      this._push0("resetTransform");
      return;
    }
    this.s.m = [a, b, c, d, e, f];
    this.ops.push("setTransform", 6, a, b, c, d, e, f);
  }
  resetTransform() {
    this.s.m = [1, 0, 0, 1, 0, 0];
    this._push0("resetTransform");
  }
  getTransform() {
    const m = this.s.m;
    return typeof DOMMatrix === "function" ? new DOMMatrix(m) : { a: m[0], b: m[1], c: m[2], d: m[3], e: m[4], f: m[5] };
  }

  // ── линии ──
  setLineDash(seg) {
    const arr = Array.from(seg ?? [], Number);
    if (arr.every((v) => Number.isFinite(v) && v >= 0)) this.s.dash = arr.length % 2 ? arr.concat(arr) : arr;
    const o = this.ops;
    o.push("dash", arr.length);
    for (const v of arr) o.push(v);
  }
  getLineDash() {
    return this.s.dash.slice();
  }

  // ── стили ──
  createLinearGradient(x0, y0, x1, y1) {
    return this._grad("LG", [x0, y0, x1, y1]);
  }
  createRadialGradient(x0, y0, r0, x1, y1, r1) {
    return this._grad("RG", [x0, y0, r0, x1, y1, r1]);
  }
  createConicGradient(a, x, y) {
    return this._grad("CG", [a, x, y]);
  }
  _grad(code, args) {
    const g = new RecGradient(this, code, args);
    this._objIndex(g, "grad");
    return g;
  }
  createPattern(image, repetition) {
    const p = new RecPattern(this, image, repetition);
    this._objIndex(p, "pat");
    return p;
  }

  // ── пути ──
  beginPath() {
    this._push0("beginPath");
  }
  closePath() {
    this._push0("closePath");
  }
  moveTo(x, y) {
    this._push2("moveTo", x, y);
  }
  lineTo(x, y) {
    this._push2("lineTo", x, y);
  }
  quadraticCurveTo(cx, cy, x, y) {
    this._push4("quadraticCurveTo", cx, cy, x, y);
  }
  bezierCurveTo(...a) {
    this._pushN("bezierCurveTo", a);
  }
  arcTo(...a) {
    this._pushN("arcTo", a);
  }
  rect(x, y, w, h) {
    this._push4("rect", x, y, w, h);
  }
  roundRect(x, y, w, h, radii) {
    if (Array.isArray(radii)) {
      // радиусы-числа сравнимы: разворачиваем массив в аргументы
      if (radii.every((r) => typeof r === "number")) {
        this._pushN("roundRect*", [x, y, w, h, ...radii]);
        return;
      }
    }
    this._pushN("roundRect", radii === undefined ? [x, y, w, h] : [x, y, w, h, radii]);
  }
  arc(...a) {
    this._pushN("arc", a);
  }
  ellipse(...a) {
    this._pushN("ellipse", a);
  }
  fill(...a) {
    this._pushN("fill", a);
  }
  stroke(...a) {
    this._pushN("stroke", a);
  }
  clip(...a) {
    this._pushN("clip", a);
  }

  // ── рисование ──
  clearRect(x, y, w, h) {
    this._push4("clearRect", x, y, w, h);
  }
  fillRect(x, y, w, h) {
    this._push4("fillRect", x, y, w, h);
  }
  strokeRect(x, y, w, h) {
    this._push4("strokeRect", x, y, w, h);
  }
  fillText(text, x, y, maxWidth) {
    if (maxWidth === undefined) this._push3("fillText", String(text), x, y);
    else this._push4("fillText", String(text), x, y, maxWidth);
  }
  strokeText(text, x, y, maxWidth) {
    if (maxWidth === undefined) this._push3("strokeText", String(text), x, y);
    else this._push4("strokeText", String(text), x, y, maxWidth);
  }
  drawImage(img, ...a) {
    if (!isStaticImage(img)) this.volatile = true;
    const o = this.ops;
    o.push("drawImage", a.length + 1, img);
    for (let i = 0; i < a.length; i++) o.push(a[i]);
  }
  drawFocusIfNeeded() {}

  measureText(text) {
    const r = this.real;
    r.save();
    for (const p of TEXT_PROPS) if (p in r && r[p] !== this.s[p]) r[p] = this.s[p];
    const m = r.measureText(text);
    r.restore();
    return m;
  }

  // пиксели и попадание в путь записью не воспроизвести
  getImageData() {
    throw new Error("RecordingContext: getImageData is not supported");
  }
  putImageData() {
    throw new Error("RecordingContext: putImageData is not supported");
  }
  createImageData(...a) {
    return this.real.createImageData(...a);
  }
  isPointInPath() {
    throw new Error("RecordingContext: isPointInPath is not supported");
  }
  isPointInStroke() {
    throw new Error("RecordingContext: isPointInStroke is not supported");
  }
}

// свойства: запись + своё состояние
for (const p of Object.keys(PROPS)) {
  const check = NUM_OK[p];
  const style = p === "fillStyle" || p === "strokeStyle";
  Object.defineProperty(RecordingContext.prototype, p, {
    get() {
      return this.s[p];
    },
    set(v) {
      if (style) {
        this._setStyle(p, v);
        return;
      }
      this._push1(SET[p], v);
      if (check) {
        const n = Number(v);
        if (Number.isFinite(n) && check(n)) this.s[p] = n;
      } else {
        this.s[p] = p === "imageSmoothingEnabled" ? !!v : v;
      }
    },
  });
}

/** Проиграть запись ops на настоящий контекст. */
export function replay(ctx, ops) {
  const objs = [];
  const o = ops;
  for (let i = 0; i < o.length; ) {
    const code = o[i];
    const n = o[i + 1];
    const a = i + 2;
    i = a + n;
    const prop = PROP_OF.get(code);
    if (prop !== undefined) {
      ctx[prop] = o[a];
      continue;
    }
    switch (code) {
      case "LG":
        objs.push(ctx.createLinearGradient(o[a], o[a + 1], o[a + 2], o[a + 3]));
        break;
      case "RG":
        objs.push(ctx.createRadialGradient(o[a], o[a + 1], o[a + 2], o[a + 3], o[a + 4], o[a + 5]));
        break;
      case "CG":
        objs.push(ctx.createConicGradient(o[a], o[a + 1], o[a + 2]));
        break;
      case "PAT":
        objs.push(ctx.createPattern(o[a], o[a + 1]));
        break;
      case "stop":
        objs[o[a]].addColorStop(o[a + 1], o[a + 2]);
        break;
      case "=fillStyleG":
        ctx.fillStyle = objs[o[a]];
        break;
      case "=strokeStyleG":
        ctx.strokeStyle = objs[o[a]];
        break;
      case "dash":
        ctx.setLineDash(o.slice(a, a + n));
        break;
      case "roundRect*":
        ctx.roundRect(o[a], o[a + 1], o[a + 2], o[a + 3], o.slice(a + 4, a + n));
        break;
      default:
        switch (n) {
          case 0: ctx[code](); break;
          case 1: ctx[code](o[a]); break;
          case 2: ctx[code](o[a], o[a + 1]); break;
          case 3: ctx[code](o[a], o[a + 1], o[a + 2]); break;
          case 4: ctx[code](o[a], o[a + 1], o[a + 2], o[a + 3]); break;
          default: ctx[code](...o.slice(a, a + n));
        }
    }
  }
}

function sameOps(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

const sameBox = (a, b) => a === b || (!!a && !!b && a.x === b.x && a.y === b.y && a.w === b.w && a.h === b.h);
function union(a, b) {
  if (!a) return b;
  if (!b) return a;
  const x = Math.min(a.x, b.x), y = Math.min(a.y, b.y);
  return { x, y, w: Math.max(a.x + a.w, b.x + b.w) - x, h: Math.max(a.y + a.h, b.y + b.h) - y };
}

/**
 * Слой с перерисовкой по изменению. Кадр: rec = layer.begin(); …рисуем в rec…; layer.end(now, box, pr).
 * box — что этот кадр закрашивает ({x,y,w,h} в логических px холста, null — ничего): перед проигрыванием
 * очищается box вместе с прошлым box (там могло остаться старое). pr — пикселей холста на логический px.
 */
export class RetainedLayer {
  constructor(ctx, { refreshS = 2 } = {}) {
    this.ctx = ctx;
    this.rec = new RecordingContext(ctx);
    this.refreshS = refreshS;
    this.prev = [];
    this.prevBox = null;
    this.drawnBox = null; // что сейчас на холсте
    this.valid = false;
    this.lastDraw = -Infinity;
    this.lastEpoch = -1;
    this.stats = { drawn: 0, skipped: 0 };
  }

  begin() {
    this.rec._begin();
    return this.rec;
  }

  /** Холст изменили или пересоздали снаружи — следующий кадр проиграть обязательно. */
  invalidate() {
    this.valid = false;
  }

  /** Закончить кадр: true — проиграли на холст, false — на холсте уже ровно это. box = null — кадр пустой. */
  end(now, box, pr = 1) {
    const ops = this.rec.ops;
    if (!box) {
      ops.length = 0; // ничего не нарисовано — записанные save/restore не нужны
      this.rec.volatile = false;
    }
    const empty = ops.length === 0;
    const same =
      this.valid &&
      !this.rec.volatile &&
      this.lastEpoch === epoch &&
      sameBox(box, this.prevBox) &&
      sameOps(ops, this.prev) &&
      (empty ? !this.drawnBox : now - this.lastDraw < this.refreshS);
    // два буфера по очереди: запись этого кадра становится «прошлой», старая — под следующий кадр
    this.rec.ops = this.prev;
    this.prev = ops;
    this.prevBox = box;
    if (same) {
      this.stats.skipped++;
      return false;
    }
    const ctx = this.ctx;
    const clear = union(this.valid ? this.drawnBox : { x: 0, y: 0, w: ctx.canvas.width / pr, h: ctx.canvas.height / pr }, box);
    ctx.save();
    if (clear) {
      ctx.setTransform(pr, 0, 0, pr, 0, 0);
      ctx.clearRect(clear.x, clear.y, clear.w, clear.h);
    }
    if (ops.length) replay(ctx, ops);
    ctx.restore();
    this.drawnBox = box;
    this.valid = true;
    this.lastDraw = now;
    this.lastEpoch = epoch;
    this.stats.drawn++;
    return true;
  }
}
