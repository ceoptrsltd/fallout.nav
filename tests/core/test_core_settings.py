import json

import httpx
from core_helpers import FakeUpstream, login, make_opts, running_app

from fnav import paths
from fnav.core.config import deep_merge, load_settings_file
from fnav.models import Settings


def test_deep_merge():
    base = {"a": {"b": 1, "c": [1, 2]}, "d": 1}
    out = deep_merge(base, {"a": {"c": [3]}, "e": {"f": 1}})
    assert out == {"a": {"b": 1, "c": [3]}, "d": 1, "e": {"f": 1}}
    assert base["a"]["c"] == [1, 2]  # исходник не изменён


def test_load_corrupted_and_partial(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{not json", encoding="utf-8")
    s, warns = load_settings_file(p)
    assert s == Settings() and warns
    assert list(tmp_path.glob("settings.json.bad-*"))

    p.write_text(json.dumps({"display": {"orientation": "north_up"}, "nav": {"camera_below_mps": -5}}),
                 encoding="utf-8")
    s, warns = load_settings_file(p)
    assert s.display.orientation == "north_up"  # валидная секция сохранена
    assert s.nav.camera_below_mps == 2.0  # битая сброшена
    assert any("nav" in w for w in warns)


async def test_settings_api(data_dir):
    cam = FakeUpstream(lambda r: httpx.Response(200, json={"ok": True}))
    async with running_app(make_opts(local_transport=cam.transport)) as c:
        await login(c)
        r = await c.get("/api/settings")
        assert r.json() == Settings().model_dump(mode="json")
        assert (await c.get("/api/settings/defaults")).json() == Settings().model_dump(mode="json")

        cam.requests.clear()
        r = await c.patch("/api/settings", json={"display": {"orientation": "north_up"},
                                                 "storage": {"tile_cache_mb": 200}})
        assert r.status_code == 200
        body = r.json()
        assert body["display"]["orientation"] == "north_up"
        assert body["display"]["compass_style"] == "v1"  # остальное не тронуто
        on_disk = json.loads(paths.settings_file().read_text(encoding="utf-8"))
        assert on_disk["display"]["orientation"] == "north_up"
        assert not list(paths.settings_file().parent.glob(".settings.json.*"))  # tmp убран
        core = c.app.state.core
        assert core.tiles.settings.storage.tile_cache_mb == 200
        assert core.nav.display.orientation == "north_up"
        assert core.hub.settings.display.orientation == "north_up"  # hub.apply_settings
        await _drain()
        assert any(req.url.path == "/reload" and req.method == "POST" for req in cam.requests)

        r = await c.patch("/api/settings", json={"display": {"orientation": "sideways"}})
        assert r.status_code == 422
        assert "display.orientation" in r.json()["detail"]
        r = await c.patch("/api/settings", content=b"{oops", headers={"content-type": "application/json"})
        assert r.status_code == 400
        r = await c.patch("/api/settings", json={"nav": {"zoom_table": []}})
        assert r.status_code == 422
        assert (await c.get("/api/settings")).json()["display"]["orientation"] == "north_up"

        full = Settings().model_dump(mode="json")
        full["camera"]["fps"] = 10
        r = await c.put("/api/settings", json=full)
        assert r.status_code == 200 and r.json()["camera"]["fps"] == 10
        assert r.json()["display"]["orientation"] == "heading_up"

        r = await c.post("/api/settings/reset")
        assert r.json() == Settings().model_dump(mode="json")

        r = await c.post("/api/display/force", json={"mode": "camera"})
        assert r.status_code == 200
        assert (await c.get("/api/settings")).json()["display"]["force_mode"] == "camera"
        tel = (await c.get("/api/telemetry")).json()
        assert tel["display"]["reason"] == "forced"
        assert (await c.post("/api/display/force", json={"mode": "bogus"})).status_code == 422


async def test_camera_reload_failure_ignored(data_dir):
    async with running_app() as c:  # камера недоступна (ConnectError)
        await login(c)
        r = await c.patch("/api/settings", json={"camera": {"hflip": True}})
        assert r.status_code == 200
        await _drain()


async def test_calibration_finish_saves(data_dir):
    async with running_app() as c:
        await login(c)
        assert (await c.post("/api/compass/calibration/start")).json()["active"] is True
        r = await c.post("/api/compass/calibration/finish")
        assert r.status_code == 200
        cal = r.json()
        assert (await c.get("/api/settings")).json()["compass"]["calibration"] == cal
        assert (await c.get("/api/gps/satellites")).status_code == 200
        assert (await c.post("/api/gps/configure")).json()["ok"] is True


async def _drain():
    import asyncio

    for _ in range(5):
        await asyncio.sleep(0.01)
