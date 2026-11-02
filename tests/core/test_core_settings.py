import json
import os
import stat
from pathlib import Path

import httpx
from core_helpers import FakeUpstream, login, make_opts, running_app

from fnav import paths
from fnav.core.config import BAD_KEEP, SettingsStore, deep_merge, lkg_path, load_settings, load_settings_file
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


async def test_broken_settings_restored_from_last_good_copy(tmp_path):
    """B26: битый settings.json → настройки из settings.json.lkg, файл сразу переписан (следующий запуск читает целый
    файл и не плодит .bad), копий битого — не больше BAD_KEEP, одинаковые не дублируются."""
    p = tmp_path / "settings.json"
    st = SettingsStore(p)
    assert st.load_state == "missing"
    st.ensure_file()
    await st.patch({"display": {"orientation": "north_up"}, "camera": {"fps": 12}})
    assert json.loads(lkg_path(p).read_text(encoding="utf-8"))["camera"]["fps"] == 12  # копия — после сохранения

    for n in range(5):  # пять отключений питания подряд с битым файлом
        p.write_text("{half-written" + "x" * n, encoding="utf-8")
        st = SettingsStore(p)
        assert st.load_state == "recovered"
        assert st.get().display.orientation == "north_up" and st.get().camera.fps == 12
        assert any("restored from settings.json.lkg" in w for w in st.warnings)
        st.ensure_file()
        assert json.loads(p.read_text(encoding="utf-8"))["camera"]["fps"] == 12  # переписан восстановленным
        again = SettingsStore(p)
        assert again.load_state == "ok" and not again.warnings
    bads = sorted(tmp_path.glob("settings.json.bad-*"))
    assert 1 <= len(bads) <= BAD_KEEP
    # тот же битый файл ещё раз — новой копии нет
    p.write_text("{half-writtenxxxx", encoding="utf-8")
    SettingsStore(p)
    assert len(list(tmp_path.glob("settings.json.bad-*"))) == len(bads)
    if os.name == "posix":  # на устройстве: 0600, как сам settings.json (в нём бывают ключи)
        for f in (p, lkg_path(p), *tmp_path.glob("settings.json.bad-*")):
            assert stat.S_IMODE(f.stat().st_mode) == 0o600, f


def test_bad_copies_capped_newest_kept(tmp_path):
    p = tmp_path / "settings.json"
    for i in range(5):  # копии от прежних запусков (до этой версии их не ограничивали)
        old = tmp_path / f"settings.json.bad-2026010{i}-000000"
        old.write_text(f"old {i}", encoding="utf-8")
        os.utime(old, (1_700_000_000 + i, 1_700_000_000 + i))
    p.write_text("[1, 2", encoding="utf-8")
    load_settings(p)
    left = sorted(f.read_text(encoding="utf-8") for f in tmp_path.glob("settings.json.bad-*"))
    assert len(left) == BAD_KEEP and left == ["[1, 2", "old 3", "old 4"]


def test_broken_section_taken_from_last_good_copy(tmp_path):
    p = tmp_path / "settings.json"
    good = Settings().model_dump(mode="json")
    good["nav"]["camera_below_mps"] = 3.5
    good["display"]["orientation"] = "north_up"
    lkg_path(p).write_text(json.dumps(good), encoding="utf-8")
    p.write_text(json.dumps({"display": {"orientation": "heading_up"}, "nav": {"camera_below_mps": -5}}),
                 encoding="utf-8")
    s, warns, state = load_settings(p)
    assert state == "recovered"
    assert s.display.orientation == "heading_up"  # целая секция — из файла
    assert s.nav.camera_below_mps == 3.5  # битая — из последней удачной копии
    assert any("the last good copy" in w for w in warns)


def test_unreadable_settings_without_good_copy_left_alone(tmp_path, monkeypatch):
    """Файл не читается (права: как с root-владельцем на устройстве), копии нет → умолчания в памяти, файл не трогаем
    (владелец чинит права и перезапускает). С копией — восстановление из неё и перезапись (файл снова fnav:fnav)."""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"camera": {"fps": 7}}), encoding="utf-8")
    real = Path.read_text

    def deny(self, *a, **kw):
        if self == p:
            raise PermissionError(13, "Permission denied")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", deny)
    st = SettingsStore(p)
    assert st.load_state == "degraded" and st.get() == Settings()
    st.ensure_file()
    assert json.loads(real(p, encoding="utf-8")) == {"camera": {"fps": 7}}  # не перезаписан
    assert not list(tmp_path.glob("settings.json.bad-*"))

    good = Settings().model_dump(mode="json")
    good["camera"]["fps"] = 9
    lkg_path(p).write_text(json.dumps(good), encoding="utf-8")
    st = SettingsStore(p)
    assert st.load_state == "recovered" and st.get().camera.fps == 9
    st.ensure_file()
    monkeypatch.undo()
    assert SettingsStore(p).get().camera.fps == 9


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


async def test_vehicle_settings(data_dir):
    """Размеры машины и антенна GPS (display.md «Антенна GPS»): антенна — в пределах кузова, иначе 422."""
    v = Settings().vehicle
    assert (v.length_m, v.width_m, v.gps_from_front_m, v.gps_right_m) == (6.0, 2.05, 0.95, 0.25)
    async with running_app() as c:
        await login(c)
        r = await c.patch("/api/settings", json={"vehicle": {"length_m": 5.4, "gps_right_m": -0.3}})
        assert r.status_code == 200
        assert r.json()["vehicle"] == {"length_m": 5.4, "width_m": 2.05, "gps_from_front_m": 0.95,
                                       "gps_right_m": -0.3}
        for bad in ({"gps_from_front_m": 7.0}, {"gps_right_m": 1.2}, {"length_m": 0.5}, {"width_m": 9}):
            r = await c.patch("/api/settings", json={"vehicle": bad})
            assert r.status_code == 422, bad
        assert (await c.get("/api/settings")).json()["vehicle"]["length_m"] == 5.4


async def _drain():
    import asyncio

    for _ in range(5):
        await asyncio.sleep(0.01)
