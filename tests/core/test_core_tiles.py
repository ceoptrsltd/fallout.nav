import asyncio
import os
import time

import httpx
import pytest
from core_helpers import FakeUpstream, login, make_opts, running_app, tiny_jpeg

from fnav.core.tiles import TileError, TileService
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
    assert e.value.status == 404
    n = len(up.requests)
    with pytest.raises(TileError):  # отрицательный кэш — повторно не ходим
        await t.get("imagery", 5, 1, 1)
    assert len(up.requests) == n
    with pytest.raises(TileError):  # не картинка → не кэшируем
        await t.get("imagery", 6, 1, 1)
    assert not list((tmp_path / "tiles").rglob("*.jpg"))
    with pytest.raises(TileError):
        await t.get("imagery", 7, 1, 1)
    assert await t.check_internet() is False
    n = len(up.requests)
    with pytest.raises(TileError):  # офлайн — без сетевых попыток
        await t.get("imagery", 8, 1, 1)
    assert len(up.requests) == n
    with pytest.raises(TileError) as e:
        await t.get("imagery", 3, 9, 0)  # x ≥ 2^z
    assert e.value.status == 400
    with pytest.raises(TileError):
        await t.get("bogus", 3, 1, 1)
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
