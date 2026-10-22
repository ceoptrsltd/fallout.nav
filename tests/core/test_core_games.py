"""Игры на экране: список по файлам и ядрам, запуск только стоя, радио на паузе на время игры."""

from __future__ import annotations

import httpx
import pytest
from core_helpers import login, make_opts

from fnav.core import games as gm
from fnav.core.app import create_app


def _setup(tmp_path, monkeypatch):
    content, cores, doom = tmp_path / "content", tmp_path / "cores", tmp_path / "doom"
    (content / "gb").mkdir(parents=True)
    cores.mkdir()
    doom.mkdir()
    (content / "gb" / "Deadeus.gb").write_bytes(b"\0" * 16)
    (content / "gb" / "readme.txt").write_text("x")
    (content / "nes").mkdir()
    (content / "nes" / "Nova.nes").write_bytes(b"\0")  # ядра NES нет — не в списке
    (doom / "freedoom2.wad").write_bytes(b"IWAD")
    (cores / "gambatte_libretro.so").write_bytes(b"")
    (cores / "prboom_libretro.so").write_bytes(b"")
    monkeypatch.setattr(gm, "CONTENT_DIR", content)
    monkeypatch.setattr(gm, "DOOM_DIR", doom)
    monkeypatch.setattr(gm, "CORE_DIRS", [cores])
    monkeypatch.setattr(gm, "ARGS_FILE", tmp_path / "run" / "game.args")
    return content, cores, doom


def test_scan_games(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    games = gm.scan_games(gm.CONTENT_DIR, gm.DOOM_DIR, gm.CORE_DIRS)
    assert [(g.id, g.title, g.system) for g in games] == [
        ("deadeus", "Deadeus", "Game Boy"), ("freedoom-phase-2", "Freedoom: Phase 2", "Doom")]


class Radio:
    def __init__(self, playing: bool):
        self.playing = playing
        self.calls: list[str] = []

    async def pause(self) -> bool:
        self.calls.append("pause")
        return self.playing

    async def resume(self) -> None:
        self.calls.append("resume")


async def test_launch_rules(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    speed = {"v": 5.0}
    radio = Radio(playing=True)
    svc = gm.GamesService(lambda: speed["v"], radio.pause, radio.resume, lambda *a: None, mock=True)
    with pytest.raises(gm.GamesError) as e:
        await svc.launch("deadeus")
    assert e.value.status == 409 and "Stop the vehicle" in e.value.detail
    speed["v"] = 0.0
    with pytest.raises(gm.GamesError) as e:
        await svc.launch("nope")
    assert e.value.status == 404
    st = await svc.launch("deadeus")
    assert st["running"] and st["current"]["id"] == "deadeus"
    args = gm.ARGS_FILE.read_text().splitlines()
    assert args[0] == "retroarch" and args[1].endswith("gambatte_libretro.so") and args[2].endswith("Deadeus.gb")
    with pytest.raises(gm.GamesError):
        await svc.launch("freedoom-phase-2")  # уже идёт игра
    st = await svc.stop("test")
    assert not st["running"] and radio.calls == ["pause", "resume"]


async def test_api_local_kiosk_may_launch(tmp_path, monkeypatch, data_dir):
    _setup(tmp_path, monkeypatch)
    app = create_app(make_opts())
    async with app.router.lifespan_context(app):
        local = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 4000)),
                                  base_url="http://testserver")
        remote = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("10.0.0.5", 4000)),
                                   base_url="http://testserver")
        async with local, remote:
            assert (await local.get("/api/games")).json()["games"][0]["id"] == "deadeus"
            assert (await remote.get("/api/games")).status_code == 401
            r = await local.post("/api/games/launch", json={"id": "deadeus"})
            assert r.status_code == 200 and r.json()["running"]
            assert (await local.post("/api/games/stop")).status_code == 401  # закрыть — из админки, со входом
            await login(remote)
            assert (await remote.post("/api/games/stop")).json()["running"] is False


def test_duke3d_native(tmp_path, monkeypatch):
    content, _cores, _doom = _setup(tmp_path, monkeypatch)
    (content / "duke3d").mkdir()
    grp = content / "duke3d" / "DUKE3D.GRP"
    grp.write_bytes(b"x" * 64)
    eduke = tmp_path / "eduke32"
    monkeypatch.setattr(gm, "EDUKE32", eduke)
    assert "duke-nukem-3d" not in [g.id for g in gm.scan_games()]  # нет движка — нет игры
    eduke.write_bytes(b"")
    g = next(g for g in gm.scan_games() if g.id == "duke-nukem-3d")
    assert g.kind == "native" and g.argv[0] == str(eduke) and g.argv[-3:] == ("-gamegrp", "DUKE3D.GRP", "-nosetup")


def test_pad_exit_combo():
    w = gm.PadWatcher(pattern="/nonexistent/*")

    def ev(code: int, value: int) -> bytes:
        return gm.EVENT.pack(0, 0, gm.EV_KEY, code, value)

    w.feed(ev(0x138, 1), now=10.0)
    assert not w.exit_held(1.0, now=12.0)  # только Share
    w.feed(ev(0x139, 1), now=10.5)
    assert not w.exit_held(1.0, now=11.0)
    assert w.exit_held(1.0, now=11.6)  # Share + Options держат 1 с
    w.feed(ev(0x139, 0), now=11.7)
    assert not w.exit_held(1.0, now=13.0)
    w.feed(ev(0x13A, 1) + ev(0x13B, 1), now=20.0)  # Select + Start (стандартная раскладка)
    assert w.exit_held(1.0, now=21.0)
