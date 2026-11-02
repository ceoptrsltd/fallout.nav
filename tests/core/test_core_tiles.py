import asyncio
import hashlib
import os
import time
from contextlib import asynccontextmanager

import httpx
import pytest
from core_helpers import FakeUpstream, login, make_opts, running_app, tiny_jpeg

from fnav.core import tiles as tmod
from fnav.core.tiles import ORPHANS_MAX, POOL_SIZE, TileError, TileService
from fnav.models import Settings


def jpeg_handler(delay: float = 0.0, size: int | None = None):
    data = tiny_jpeg() if size is None else b"\xff\xd8" + os.urandom(size - 2)

    async def h(request: httpx.Request) -> httpx.Response:
        if delay:
            await asyncio.sleep(delay)
        return httpx.Response(200, content=data, headers={"content-type": "image/jpeg"})

    return h


class AsyncUpstream(FakeUpstream):
    async def __call__(self, request):  # type: ignore[override]
        self.requests.append(request)
        return await self.handler(request)


class SlowUpstream:
    """Медленный «Esri»: тайлы z=15 отвечают сразу, остальные — через delay с; считает отменённые."""

    def __init__(self, delay: float = 10.0):
        self.delay = delay
        self.started = 0
        self.cancelled = 0
        self.data = tiny_jpeg()

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if "/tile/15/" not in request.url.path:
            self.started += 1
            try:
                await asyncio.sleep(self.delay)
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
        return httpx.Response(200, content=self.data, headers={"content-type": "image/jpeg"})


@asynccontextmanager
async def slow_esri(delay: float):
    """Настоящий HTTP-сервер на 127.0.0.1 (без внешней сети): GET тайла — через delay с, HEAD — сразу.
    Отдаёт шаблон URL для TileSettings."""
    data = tiny_jpeg()
    handlers: set[asyncio.Task] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        handlers.add(asyncio.current_task())
        try:
            while line := await reader.readline():
                method = line.split()[0]
                while (await reader.readline()) not in (b"\r\n", b""):
                    pass
                if method == b"GET":
                    await asyncio.sleep(delay)
                head = b"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(data)
                writer.write(head + (data if method == b"GET" else b""))
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/tile/{{z}}/{{y}}/{{x}}"
    finally:
        srv.close()
        for h in handlers:
            h.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)


async def cancel_all(tasks) -> None:
    for x in tasks:
        x.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def svc(tmp_path, up, **settings_kw) -> TileService:
    s = Settings(**settings_kw)
    return TileService(s, root=tmp_path / "tiles", transport=httpx.MockTransport(up))


async def test_miss_then_hit_and_url(tmp_path):
    up = AsyncUpstream(jpeg_handler())
    t = svc(tmp_path, up)
    await t.start(background=False)
    tile = await t.get("imagery", 17, 79230, 40965)
    assert not tile.cached and tile.content_type == "image/jpeg"
    assert (tmp_path / "tiles/imagery/17/79230/40965.jpg").exists()
    assert len(up.requests) == 1
    req = up.requests[0]
    assert req.url.path.endswith("/World_Imagery/MapServer/tile/17/40965/79230")  # порядок z/y/x
    assert "fallout.nav" in req.headers["user-agent"]
    tile2 = await t.get("imagery", 17, 79230, 40965)
    assert tile2.cached and tile2.data == tile.data
    assert len(up.requests) == 1
    st = t.stats()
    assert st["count"] == 1 and st["hit_rate"] == 0.5
    await t.stop()


async def test_api_key_token(tmp_path):
    up = AsyncUpstream(jpeg_handler())
    t = svc(tmp_path, up, tiles={"esri_api_key": "KEY123"})
    await t.start(background=False)
    await t.get("transport", 3, 1, 2)
    assert up.requests[0].url.params["token"] == "KEY123"
    assert "World_Transportation" in up.requests[0].url.path
    await t.stop()


async def test_concurrent_requests_single_upstream(tmp_path):
    up = AsyncUpstream(jpeg_handler(delay=0.2))
    t = svc(tmp_path, up)
    await t.start(background=False)
    res = await asyncio.gather(*[t.get("imagery", 10, 5, 6) for _ in range(10)])
    assert len(up.requests) == 1
    assert all(r.data == res[0].data for r in res)
    await t.stop()


async def test_esri_no_data_tile_is_a_miss(tmp_path, monkeypatch):
    """Esri answers 200 with a grey "Map data not yet available" where it has no imagery at that zoom:
    not cached, 404 to the screen (it draws the level above); one cached earlier is thrown away."""
    grey = bytes([0xFF, 0xD8]) + b"grey" * 50
    monkeypatch.setattr(tmod, "ESRI_NO_DATA", (len(grey), hashlib.sha256(grey).hexdigest()))
    assert tmod.is_esri_no_data(grey) and not tmod.is_esri_no_data(tiny_jpeg())

    async def h(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=grey, headers={"content-type": "image/jpeg"})

    up = AsyncUpstream(h)
    t = svc(tmp_path, up)
    old = tmp_path / "tiles/imagery/20/5/6.jpg"
    old.parent.mkdir(parents=True)
    old.write_bytes(grey)
    await t.start(background=False)
    with pytest.raises(TileError) as e:
        await t.get("imagery", 20, 5, 6)  # the cached grey tile
    assert e.value.status == 404 and not old.exists() and up.requests == []
    with pytest.raises(TileError) as e:
        await t.get("imagery", 20, 7, 8)  # fresh from Esri
    assert e.value.status == 404 and len(up.requests) == 1
    assert not (tmp_path / "tiles/imagery/20/7/8.jpg").exists()
    with pytest.raises(TileError):
        await t.get("imagery", 20, 7, 8)  # remembered for a while: Esri is not asked again
    assert len(up.requests) == 1
    await t.stop()


async def test_errors_and_offline(tmp_path):
    def h(req):
        if "/tile/5/" in req.url.path:
            return httpx.Response(404)
        if "/tile/6/" in req.url.path:
            return httpx.Response(200, json={"error": "token"})
        raise httpx.ConnectError("offline", request=req)

    up = FakeUpstream(h)
    t = TileService(Settings(), root=tmp_path / "tiles", transport=up.transport)
    await t.start(background=False)
    with pytest.raises(TileError) as e:
        await t.get("imagery", 5, 1, 1)
    assert e.value.status == 404 and e.value.retry_after is None
    n = len(up.requests)
    with pytest.raises(TileError):  # отрицательный кэш — повторно не ходим
        await t.get("imagery", 5, 1, 1)
    assert len(up.requests) == n
    with pytest.raises(TileError) as e:  # не картинка → не кэшируем
        await t.get("imagery", 6, 1, 1)
    assert e.value.status == 502
    assert not list((tmp_path / "tiles").rglob("*.jpg"))
    with pytest.raises(TileError) as e:  # нет связи — временно, а не «тайла нет»
        await t.get("imagery", 7, 1, 1)
    assert (e.value.status, e.value.retry_after) == (503, 2)
    assert await t.check_internet() is False
    n = len(up.requests)
    with pytest.raises(TileError) as e:  # офлайн — без сетевых попыток
        await t.get("imagery", 8, 1, 1)
    assert (e.value.status, e.value.retry_after) == (503, 15)
    assert len(up.requests) == n
    with pytest.raises(TileError) as e:
        await t.get("imagery", 3, 9, 0)  # x ≥ 2^z
    assert e.value.status == 400
    with pytest.raises(TileError):
        await t.get("bogus", 3, 1, 1)
    await t.stop()


async def test_transient_upstream_errors_are_503(tmp_path):
    def h(req):
        z = int(req.url.path.split("/tile/")[1].split("/")[0])
        if z == 2:
            raise httpx.PoolTimeout("all connections busy")
        if z == 3:
            raise httpx.ReadTimeout("slow", request=req)
        if z in (4, 5, 6):
            return httpx.Response({4: 500, 5: 503, 6: 429}[z])
        if z == 7:
            return httpx.Response(403)  # например, неверный токен — не «тайла нет»
        return httpx.Response(404)

    up = FakeUpstream(h)
    t = TileService(Settings(), root=tmp_path / "tiles", transport=up.transport)
    await t.start(background=False)

    async def status(z: int) -> tuple[int, int | None]:
        with pytest.raises(TileError) as e:
            await t.get("imagery", z, 1, 1)
        return e.value.status, e.value.retry_after

    assert await status(2) == (503, 2)
    assert not t._check_now.is_set()  # пул занят — это очередь, интернет не перепроверяем
    assert await status(3) == (503, 2)
    assert t._check_now.is_set()
    for z in (4, 5, 6):
        assert await status(z) == (503, 2)
        n = len(up.requests)
        assert await status(z) == (503, 2)  # временное не запоминаем — спрашиваем Esri снова
        assert len(up.requests) == n + 1
    assert await status(7) == (502, None)
    assert await status(8) == (404, None)
    await t.stop()


async def test_abandoned_fetches_cancelled(tmp_path):
    up = SlowUpstream()
    t = svc(tmp_path, up)
    await t.start(background=False)
    wave = [asyncio.create_task(t.get("imagery", 12, i, 0)) for i in range(10)]
    await asyncio.sleep(0.05)
    assert up.started == 10
    await cancel_all(wave)  # экран бросил все запросы
    await asyncio.sleep(0.05)
    assert up.cancelled == 10 - ORPHANS_MAX
    assert len(t._inflight) == ORPHANS_MAX  # пара брошенных докачивается в кэш
    # нужный тайл не ждёт брошенных
    assert not (await asyncio.wait_for(t.get("imagery", 15, 1, 1), 1.0)).cached
    # докачиваемый тайл снова нужен — та же загрузка, без нового запроса к Esri
    layer, z, x, y = next(iter(t._inflight))
    again = asyncio.create_task(t.get(layer, z, x, y))
    await asyncio.sleep(0.05)
    assert up.started == 10 and not again.done()
    await cancel_all([again])
    await t.stop()
    assert not t._inflight


async def test_new_tile_preempts_abandoned(tmp_path):
    up = SlowUpstream()
    t = svc(tmp_path, up)
    await t.start(background=False)
    gone = [asyncio.create_task(t.get("imagery", 12, i, 0)) for i in range(ORPHANS_MAX)]
    await asyncio.sleep(0.05)
    await cancel_all(gone)
    assert len(t._orphans()) == ORPHANS_MAX and up.cancelled == 0
    # пул заполняют нужные тайлы — брошенные уступают им соединения
    wanted = [asyncio.create_task(t.get("imagery", 13, i, 0)) for i in range(POOL_SIZE - 1)]
    await asyncio.sleep(0.05)
    assert len(t._inflight) == POOL_SIZE
    assert len(t._orphans()) == ORPHANS_MAX - 1 and up.cancelled == 1
    await cancel_all(wanted)
    await t.stop()


async def test_slow_network_abandoned_wave(tmp_path):
    """Регрессия: медленная сеть, экран бросил волну запросов. Раньше брошенные загрузки докачивались
    и держали пул: нужный тайл получал 404 через 5 с, а проверка интернета — «нет сети»."""
    async with slow_esri(1.5) as url:
        t = TileService(Settings(tiles={"imagery_url": url}), root=tmp_path / "tiles")
        await t.start(background=False)
        wave = [asyncio.create_task(t.get("imagery", 15, 100 + i, 200)) for i in range(24)]
        await asyncio.sleep(0.3)
        await cancel_all(wave)
        t0 = time.monotonic()
        tile = await t.get("imagery", 15, 5000, 200)
        assert not tile.cached and time.monotonic() - t0 < 3.5
        # проверка интернета — своим соединением, даже когда пул тайлов занят целиком
        busy = [asyncio.create_task(t.get("imagery", 16, i, 0)) for i in range(2 * POOL_SIZE)]
        await asyncio.sleep(0.2)
        t0 = time.monotonic()
        assert await t.check_internet() is True
        assert time.monotonic() - t0 < 1.0
        await cancel_all(busy)
        await t.stop()


async def test_lru_eviction(tmp_path):
    # лимит 1 МБ, тайлы по 100 КБ → после 11-го вытесняются самые старые до 90 %
    up = AsyncUpstream(jpeg_handler(size=100 * 1024))
    s = Settings()
    s.storage.__dict__["tile_cache_mb"] = 1  # обходим ge=50 только для теста
    t = TileService(s, root=tmp_path / "tiles", transport=httpx.MockTransport(up))
    await t.start(background=False)
    for i in range(10):
        await t.get("imagery", 12, i, 0)
        await asyncio.sleep(0.001)
    # «освежаем» тайл 0 — он не должен быть вытеснен
    await asyncio.sleep(0.01)
    assert (await t.get("imagery", 12, 0, 0)).cached
    for i in range(10, 13):
        await t.get("imagery", 12, i, 0)
        await t.wait_evicted()
    assert t._total <= t.limit_bytes
    files = {p.parent.name for p in (tmp_path / "tiles").rglob("*.jpg")}
    assert "0" in files  # недавно использованный остался
    assert "1" not in files  # самый старый ушёл
    assert t.stats()["count"] == len(files)
    await t.stop()


async def test_index_rebuilt_on_restart(tmp_path):
    up = AsyncUpstream(jpeg_handler())
    t = svc(tmp_path, up)
    await t.start(background=False)
    for i in range(3):
        await t.get("places", 4, i, 1)
    await t.stop()
    old = time.time() - 7200
    for p in (tmp_path / "tiles").rglob("*.jpg"):
        os.utime(p, (old, old))
    t2 = svc(tmp_path, up)
    await t2.start(background=False)
    assert t2.stats()["count"] == 3
    assert (await t2.get("places", 4, 1, 1)).cached
    # mtime обновлён (LRU переживает перезапуск)
    assert (tmp_path / "tiles/places/4/1/1.jpg").stat().st_mtime > old + 3600
    await t2.clear()
    assert t2.stats()["count"] == 0 and not (tmp_path / "tiles/places").exists()
    await t2.stop()


async def test_tiles_http(data_dir):
    up = AsyncUpstream(jpeg_handler())
    async with running_app(make_opts(tile_transport=httpx.MockTransport(up))) as c:
        assert (await c.get("/tiles/imagery/3/1/2")).status_code == 401
        await login(c)
        r = await c.get("/tiles/imagery/3/1/2")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
        assert r.headers["cache-control"] == "max-age=86400" and r.headers["x-cache"] == "MISS"
        r = await c.get("/tiles/imagery/3/1/2")
        assert r.headers["x-cache"] == "HIT"
        assert (await c.get("/tiles/imagery/3/1/zz")).status_code == 400
        assert (await c.get("/tiles/nope/3/1/2")).status_code == 404
        st = (await c.get("/api/tiles/stats")).json()
        assert st["count"] == 1 and st["limit_mb"] == 1024 and st["hit_rate"] == 0.5
        assert (await c.delete("/api/tiles")).json() == {"ok": True}
        assert (await c.get("/api/tiles/stats")).json()["count"] == 0


async def test_tiles_http_errors(data_dir):
    def h(req):
        if "/tile/5/" in req.url.path:
            return httpx.Response(404)
        raise httpx.ConnectError("offline", request=req)

    async with running_app(make_opts(tile_transport=FakeUpstream(h).transport)) as c:
        await login(c)
        r = await c.get("/tiles/imagery/5/1/1")
        assert r.status_code == 404 and "retry-after" not in r.headers
        r = await c.get("/tiles/imagery/6/1/1")  # временно — экран спросит снова через 2 с
        assert r.status_code == 503 and r.headers["retry-after"] == "2"
        assert r.headers["cache-control"] == "no-store"


async def test_tiles_http_client_gone_cancels_fetch(live_server):
    """Настоящий uvicorn: экран закрыл соединение — загрузка с Esri отменяется (кроме ORPHANS_MAX)."""
    up = SlowUpstream()
    srv = live_server(tile_transport=httpx.MockTransport(up), background=False)

    async def abandon(path: str) -> None:
        _r, w = await asyncio.open_connection("127.0.0.1", srv.port)
        w.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
        await w.drain()
        await asyncio.sleep(0.3)
        w.close()

    n = ORPHANS_MAX + 3
    await asyncio.gather(*[abandon(f"/tiles/imagery/12/{i}/0") for i in range(n)])
    for _ in range(100):
        if up.cancelled >= n - ORPHANS_MAX:
            break
        await asyncio.sleep(0.05)
    assert up.started == n and up.cancelled == n - ORPHANS_MAX
    async with httpx.AsyncClient(base_url=srv.url) as c:  # с 127.0.0.1 — без входа
        assert (await c.get("/tiles/imagery/15/1/1")).status_code == 200
