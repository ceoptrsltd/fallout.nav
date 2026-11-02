"""Прокси тайлов Esri с дисковым LRU-кэшем.

Кэш: {tiles_dir}/{layer}/{z}/{x}/{y}.jpg|.png. Индекс (размер, время доступа) держится в памяти:
строится фоновым обходом при старте, дальше обновляется инкрементально. Размер кэша — сумма
размеров файлов; при превышении лимита фоновая задача удаляет самые давно использованные
тайлы до 90 % лимита. Время доступа переживает перезапуск: при попадании mtime файла
обновляется не чаще раза в час.

Ошибки: 404 — только «такого тайла у Esri нет»; временные (пул занят, таймаут, обрыв, 5xx, нет
интернета) — 503 с Retry-After, экран спросит снова через пару секунд. Загрузку, которую никто больше
не ждёт (экран отменил запрос), отменяем; докачиваем в кэш не больше ORPHANS_MAX таких. Проверка
интернета идёт своим соединением, а не через пул тайлов.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from fnav import __version__, paths
from fnav.core.util import cancel_tasks
from fnav.models import Settings

log = logging.getLogger("fnav.core.tiles")

LAYERS = ("imagery", "transport", "places", "street")
USER_AGENT = f"Mozilla/5.0 (X11; Linux aarch64) fallout.nav/{__version__} (+http://fnav.local)"
FETCH_TIMEOUT_S = 5.0
CHECK_TIMEOUT_S = 4.0
POOL_SIZE = 8  # соединений к Esri для тайлов
ORPHANS_MAX = 2  # брошенных клиентом загрузок, которые всё же докачиваем в кэш
RETRY_SOON_S = 2  # Retry-After для временных ошибок (503)
MAX_Z = 23
EVICT_TO = 0.9
UTIME_EVERY_S = 3600.0
NEG_TTL_S = 600.0  # «такого тайла у Esri нет» — помним 10 минут
CHECK_ONLINE_S = 60.0
CHECK_OFFLINE_S = 15.0

# Где снимка (карты улиц) такого уровня у Esri нет, сервер отвечает 200 и серой картинкой «Map data not yet
# available» (одна и та же по всему миру). Для нас это «тайла нет» (404): экран рисует растянутый тайл уровнем выше.
ESRI_NO_DATA = (2521, "9eafd300d61393184a4abc1d458564cfd1cd9b6f9c4e9c74687045c0a0e5b858")


def is_esri_no_data(data: bytes) -> bool:
    return len(data) == ESRI_NO_DATA[0] and hashlib.sha256(data).hexdigest() == ESRI_NO_DATA[1]


_EXT_BY_CT = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
_CT_BY_EXT = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


class TileError(Exception):
    def __init__(self, status: int, detail: str, retry_after: int | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.retry_after = retry_after  # с: когда спросить снова (заголовок Retry-After у 503)


def _retry_soon(detail: str, after: float = RETRY_SOON_S) -> TileError:
    """Временная ошибка (503): тайл, скорее всего, есть — клиенту стоит повторить скоро."""
    return TileError(503, detail, int(after))


@dataclass
class Tile:
    data: bytes
    content_type: str
    cached: bool


@dataclass
class _Entry:
    size: int
    atime: float
    ext: str
    disk_mtime: float


Key = tuple[str, int, int, int]


@dataclass
class _Fetch:
    task: asyncio.Task
    waiters: int = 0  # сколько запросов ждут результат; 0 — загрузку бросили


class TileService:
    def __init__(self, settings: Settings, *, root: Path | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.root = root or paths.tiles_dir()
        headers = {"User-Agent": USER_AGENT, "Accept": "image/*,*/*;q=0.5"}
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(FETCH_TIMEOUT_S),
            headers=headers,
            limits=httpx.Limits(max_connections=POOL_SIZE, max_keepalive_connections=4),
            follow_redirects=True,
        )
        # проверка интернета — своим соединением: на медленной сети пул занят тайлами,
        # и ожидание свободного слота не должно выглядеть как «нет интернета»
        self._check_client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(CHECK_TIMEOUT_S),
            headers=headers,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            follow_redirects=True,
        )
        self._index: dict[Key, _Entry] = {}
        self._total = 0
        self._scan_done = False
        self._inflight: dict[Key, _Fetch] = {}
        self._neg: OrderedDict[Key, float] = OrderedDict()
        self._evict_task: asyncio.Task | None = None
        self._bg: list[asyncio.Task] = []
        self.hits = 0
        self.misses = 0
        self.internet: bool | None = None
        self._last_check = 0.0
        self._last_ok_fetch = 0.0
        self._check_now = asyncio.Event()

    # ── жизненный цикл ──
    async def start(self, *, background: bool = True) -> None:
        await self.scan()
        if background:
            self._bg.append(asyncio.create_task(self._internet_loop(), name="tiles-internet"))

    async def stop(self) -> None:
        await cancel_tasks(*self._bg, self._evict_task, *(f.task for f in self._inflight.values()))
        self._bg.clear()
        self._inflight.clear()
        await self._client.aclose()
        await self._check_client.aclose()

    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings
        self._maybe_evict()

    @property
    def limit_bytes(self) -> int:
        return int(self.settings.storage.tile_cache_mb) * 1024 * 1024

    # ── индекс ──
    async def scan(self) -> None:
        found = await asyncio.to_thread(_scan_dir, self.root)
        for k, e in found.items():
            self._index.setdefault(k, e)
        self._total = sum(e.size for e in self._index.values())
        self._scan_done = True
        log.info("tile cache: %d files, %.1f MB", len(self._index), self._total / 1048576)
        self._maybe_evict()

    def _path(self, key: Key, ext: str) -> Path:
        layer, z, x, y = key
        return self.root / layer / str(z) / str(x) / f"{y}{ext}"

    def _add(self, key: Key, size: int, ext: str, now: float, disk_mtime: float) -> None:
        old = self._index.get(key)
        if old:
            self._total -= old.size
        self._index[key] = _Entry(size, now, ext, disk_mtime)
        self._total += size
        self._maybe_evict()

    def _drop(self, key: Key) -> None:
        e = self._index.pop(key, None)
        if e:
            self._total -= e.size

    def _maybe_evict(self) -> None:
        if self._total <= self.limit_bytes:
            return
        if self._evict_task and not self._evict_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # вне event loop (тесты/инициализация) — вытеснит следующий вызов
        self._evict_task = loop.create_task(self._evict(), name="tiles-evict")

    async def _evict(self) -> None:
        target = int(self.limit_bytes * EVICT_TO)
        if self._total <= target:
            return
        victims: list[Path] = []
        for key, e in sorted(self._index.items(), key=lambda kv: kv[1].atime):
            if self._total <= target:
                break
            victims.append(self._path(key, e.ext))
            self._drop(key)
        log.info("tile cache: evicting %d files", len(victims))
        await asyncio.to_thread(_unlink_many, victims)

    async def wait_evicted(self) -> None:
        """Для тестов: дождаться окончания фонового вытеснения."""
        while self._evict_task and not self._evict_task.done():
            await asyncio.wait({self._evict_task})

    # ── статистика / очистка ──
    def stats(self) -> dict:
        total_req = self.hits + self.misses
        return {
            "size_mb": round(self._total / 1048576, 2),
            "count": len(self._index),
            "limit_mb": self.settings.storage.tile_cache_mb,
            "hit_rate": round(self.hits / total_req, 3) if total_req else 0.0,
            "hits": self.hits,
            "misses": self.misses,
            "ready": self._scan_done,
            "internet": self.internet,
        }

    @property
    def size_mb(self) -> float:
        return round(self._total / 1048576, 2)

    async def clear(self) -> None:
        if self._evict_task and not self._evict_task.done():
            self._evict_task.cancel()
        self._index.clear()
        self._total = 0
        self._neg.clear()
        self.hits = self.misses = 0
        await asyncio.to_thread(_clear_dir, self.root)

    # ── выдача тайла ──
    def url_for(self, layer: str, z: int, x: int, y: int) -> str:
        ts = self.settings.tiles
        tpl = {
            "imagery": ts.imagery_url,
            "transport": ts.transport_url,
            "places": ts.places_url,
            "street": ts.street_url,
        }[layer]
        url = tpl.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
        if ts.esri_api_key:
            url += ("&" if "?" in url else "?") + "token=" + ts.esri_api_key
        return url

    async def get(self, layer: str, z: int, x: int, y: int) -> Tile:
        if layer not in LAYERS:
            raise TileError(404, "Unknown layer")
        if not (0 <= z <= MAX_Z) or not (0 <= x < 2**z) or not (0 <= y < 2**z):
            raise TileError(400, "Invalid tile coordinates")
        key: Key = (layer, z, x, y)

        cached = await self._read_cached(key)
        if cached:
            self.hits += 1
            return cached
        self.misses += 1

        neg = self._neg.get(key)
        if neg and time.monotonic() - neg < NEG_TTL_S:
            raise TileError(404, "Tile not available from Esri")
        if self.internet is False and time.monotonic() - self._last_check < CHECK_OFFLINE_S:
            raise _retry_soon("No internet and tile not cached", CHECK_OFFLINE_S)

        f = self._inflight.get(key)
        if f is None:
            self._make_room()
            task = asyncio.get_running_loop().create_task(self._fetch_and_store(key),
                                                          name=f"tile-{layer}/{z}/{x}/{y}")
            f = self._inflight[key] = _Fetch(task)
            task.add_done_callback(lambda t, k=key: self._inflight_done(k, t))
        f.waiters += 1
        try:
            return await asyncio.shield(f.task)
        finally:
            f.waiters -= 1
            if not f.waiters and not f.task.done():
                self._abandoned(key)

    def _inflight_done(self, key: Key, task: asyncio.Task) -> None:
        f = self._inflight.get(key)
        if f is not None and f.task is task:
            del self._inflight[key]
        if not task.cancelled():
            task.exception()  # помечаем как полученное: клиент мог уйти раньше

    def _orphans(self) -> list[Key]:
        """Загрузки, которые никто не ждёт (от старых к новым)."""
        return [k for k, f in self._inflight.items() if not f.waiters]

    def _abandoned(self, key: Key) -> None:
        """Последний ждавший ушёл (экран отменил запрос): докачиваем в кэш не больше ORPHANS_MAX
        брошенных загрузок, остальные отменяем — иначе они держат пул, а нужные тайлы ждут слота."""
        if len(self._orphans()) > ORPHANS_MAX:
            self._cancel_fetch(key)

    def _make_room(self) -> None:
        """Нужный тайл важнее докачки брошенных: пул полон — отменяем брошенные, сначала свежие."""
        orphans = self._orphans()
        while orphans and len(self._inflight) >= POOL_SIZE:
            self._cancel_fetch(orphans.pop())

    def _cancel_fetch(self, key: Key) -> None:
        f = self._inflight.pop(key, None)
        if f is not None:
            f.task.cancel()

    async def _read_cached(self, key: Key) -> Tile | None:
        e = self._index.get(key)
        exts = [e.ext] if e else ([".png", ".jpg"] if key[0] in ("transport", "places") else [".jpg", ".png"])
        if not e and self._scan_done:
            return None  # индекс полный — файла точно нет
        now = time.time()
        bump = e is not None and now - e.disk_mtime > UTIME_EVERY_S
        res = await asyncio.to_thread(_read_tile, [self._path(key, x) for x in exts], bump)
        if res is None:
            if e:
                self._drop(key)  # файл удалили снаружи
            return None
        data, ext, mtime = res
        if is_esri_no_data(data):  # закэширована раньше, чем мы научились её узнавать
            self._drop(key)
            await asyncio.to_thread(_unlink_many, [self._path(key, ext)])
            self._remember_missing(key)
            return None
        if e:
            e.atime = now
            if bump:
                e.disk_mtime = now
        else:
            self._add(key, len(data), ext, now, mtime)
        return Tile(data, _CT_BY_EXT.get(ext, "image/jpeg"), True)

    async def _fetch_and_store(self, key: Key) -> Tile:
        layer, z, x, y = key
        url = self.url_for(layer, z, x, y)
        try:
            r = await self._client.get(url)
        except httpx.PoolTimeout:
            # все соединения заняты другими тайлами — очередь, а не «нет сети»
            raise _retry_soon("Tile proxy busy") from None
        except httpx.TransportError as e:  # таймаут, обрыв, нет соединения
            log.debug("tile %s: %r", key, e)
            self._check_now.set()
            raise _retry_soon("Tile server unreachable") from None
        except httpx.HTTPError as e:
            log.debug("tile %s: %r", key, e)
            raise TileError(502, "Tile server error") from None
        self._last_ok_fetch = time.monotonic()
        self.internet = True
        if r.status_code == 404 or r.status_code == 400:
            self._remember_missing(key)
            raise TileError(404, "Tile not available from Esri")
        if r.status_code == 429 or r.status_code >= 500:
            raise _retry_soon(f"Tile server returned {r.status_code}")
        if r.status_code != 200:
            raise TileError(502, f"Tile server returned {r.status_code}")
        ct = r.headers.get("content-type", "").split(";")[0].strip().lower()
        ext = _EXT_BY_CT.get(ct)
        if not ext or not r.content:
            # Esri при ошибке токена отдаёт JSON с кодом 200
            raise TileError(502, "Tile server did not return an image")
        data = r.content
        if is_esri_no_data(data):
            self._remember_missing(key)
            raise TileError(404, "Esri has no imagery at this zoom here")
        path = self._path(key, ext)
        try:
            mtime = await asyncio.to_thread(_write_tile, path, data)
            self._add(key, len(data), ext, time.time(), mtime)
        except OSError as e:
            log.warning("cannot save tile %s: %s", path, e)
        return Tile(data, _CT_BY_EXT[ext], False)

    def _remember_missing(self, key: Key) -> None:
        self._neg[key] = time.monotonic()
        self._neg.move_to_end(key)
        while len(self._neg) > 10000:
            self._neg.popitem(last=False)

    # ── проверка интернета ──
    async def check_internet(self) -> bool:
        # HEAD — без тела: раздача с телефона, трафик экономим
        url = self.url_for("imagery", 1, 0, 0)
        started = time.monotonic()
        try:
            r = await self._check_client.head(url)
            ok = r.status_code < 500
        except Exception as e:  # noqa: BLE001 — любая ошибка = нет связи
            log.debug("internet check (%s): %r", urlsplit(url).netloc, e)
            ok = False
        if not ok and self._last_ok_fetch > started:
            ok = True  # пока ждали HEAD, пришёл тайл — сеть есть, просто медленная
        self.internet = ok
        self._last_check = time.monotonic()
        return ok

    async def _internet_loop(self) -> None:
        while True:
            if time.monotonic() - self._last_ok_fetch > 10 or self.internet is not True:
                prev = self.internet
                ok = await self.check_internet()
                if ok != prev:
                    log.info("internet for tiles: %s", "online" if ok else "offline")
            else:
                self._last_check = time.monotonic()
            delay = CHECK_ONLINE_S if self.internet else CHECK_OFFLINE_S
            self._check_now.clear()
            try:
                await asyncio.wait_for(self._check_now.wait(), timeout=delay)
                await asyncio.sleep(1.0)  # дебаунс: пачка ошибок → одна проверка
                self._last_ok_fetch = 0.0
            except TimeoutError:
                pass


# ── блокирующие операции (в потоке) ──


def _scan_dir(root: Path) -> dict[Key, _Entry]:
    out: dict[Key, _Entry] = {}
    if not root.is_dir():
        return out
    for layer in LAYERS:
        ldir = root / layer
        if not ldir.is_dir():
            continue
        for dirpath, _dirs, files in os.walk(ldir):
            rel = Path(dirpath).relative_to(ldir).parts
            if len(rel) != 2:
                continue
            try:
                z, x = int(rel[0]), int(rel[1])
            except ValueError:
                continue
            for fn in files:
                stem, ext = os.path.splitext(fn)
                if ext not in _CT_BY_EXT:
                    if fn.endswith(".tmp"):
                        try:
                            os.unlink(os.path.join(dirpath, fn))
                        except OSError:
                            pass
                    continue
                try:
                    y = int(stem)
                    st = os.stat(os.path.join(dirpath, fn))
                except (ValueError, OSError):
                    continue
                out[(layer, z, x, y)] = _Entry(st.st_size, st.st_mtime, ext, st.st_mtime)
    return out


def _read_tile(candidates: list[Path], bump: bool) -> tuple[bytes, str, float] | None:
    for p in candidates:
        try:
            data = p.read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            continue
        if not data:
            continue
        mtime = time.time()
        if bump:
            try:
                os.utime(p, None)
            except OSError:
                pass
        else:
            try:
                mtime = p.stat().st_mtime
            except OSError:
                pass
        return data, p.suffix, mtime
    return None


def _write_tile(path: Path, data: bytes) -> float:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return time.time()


def _unlink_many(files: list[Path]) -> None:
    for p in files:
        try:
            p.unlink()
        except OSError:
            pass
    # пустые каталоги x/ — убираем
    for d in {p.parent for p in files}:
        try:
            d.rmdir()
        except OSError:
            pass


def _clear_dir(root: Path) -> None:
    for layer in LAYERS:
        shutil.rmtree(root / layer, ignore_errors=True)
