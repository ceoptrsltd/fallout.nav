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
    assert gm.ARGS_FILE.read_text().splitlines()[1].endswith("Deadeus.gb")
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
