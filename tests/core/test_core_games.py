"""Игры на экране: список по файлам и ядрам, запуск только стоя, радио на паузе на время игры."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from core_helpers import login, make_opts, running_app

from fnav.core import games as gm
from fnav.core.app import create_app
from fnav.core.radio import RadioDirectory, RadioService
from fnav.models import RadioOutput, RadioSettings, RadioStation


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


def test_pad_presence(tmp_path, monkeypatch):
    inp = tmp_path / "input"
    (inp / "by-id").mkdir(parents=True)
    monkeypatch.setattr(gm, "PRESENCE_GLOBS", (str(inp / "js*"), str(inp / "by-id" / "*-event-joystick")))
    svc = gm.GamesService(lambda: 0.0, Radio(False).pause, Radio(False).resume, lambda *a: None, mock=False)
    assert svc.pad_present(now=100.0) is False
    (inp / "js0").write_text("")
    assert svc.pad_present(now=101.0) is False  # проверка не чаще раза в PRESENCE_CACHE_S
    assert svc.pad_present(now=102.1) is True
    (inp / "js0").unlink()
    (inp / "by-id" / "usb-Sony_Wireless_Controller-event-joystick").write_text("")
    assert svc.pad_present(now=104.2) is True
    (inp / "by-id" / "usb-Sony_Wireless_Controller-event-joystick").unlink()
    assert svc.pad_present(now=106.3) is False
    mock = gm.GamesService(lambda: 0.0, Radio(False).pause, Radio(False).resume, lambda *a: None, mock=True)
    assert mock.pad_present() is True  # на ноутбуке меню игр открывается с настоящего геймпада


async def test_telemetry_gamepad_flag(tmp_path, monkeypatch, data_dir):
    app = create_app(make_opts())
    async with app.router.lifespan_context(app):
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("10.0.0.5", 4000)),
                              base_url="http://testserver")
        async with c:
            await login(c)
            disp = (await c.get("/api/telemetry")).json()["display"]
            assert disp["gamepad"] is True  # mock: геймпад считается подключённым
        core = app.state.core
        monkeypatch.setattr(core.games, "pad_present", lambda now=None: False)
        assert core.build_telemetry().display.gamepad is False


# ── B17: пауза радио ради игры переживает перезапуск ──


class _Player:
    def __init__(self) -> None:
        self.cb = None
        self.loads: list[str] = []

    def on_event(self, cb) -> None:
        self.cb = cb

    async def start(self) -> None:
        self.cb("ready", True)

    async def close(self) -> None: ...

    async def load(self, url: str, device: str) -> None:
        self.loads.append(url)

    async def halt(self) -> None: ...

    async def set_volume(self, v: int) -> None: ...


async def _radio(tmp_path) -> RadioService:
    d = RadioDirectory(transport=httpx.MockTransport(lambda r: httpx.Response(404)), retry_pause_s=0)
    radio = RadioService(d, _Player(), tmp_path / "radio_state.json", RadioSettings)
    radio.set_output(RadioOutput(address="AA:BB:CC:DD:EE:FF", connected=True))
    await radio.start()
    for _ in range(5):
        await asyncio.sleep(0)
    return radio


def _games(radio: RadioService) -> gm.GamesService:
    return gm.GamesService(lambda: 0.0, radio.pause_for_game, radio.resume_after_game, lambda *a: None, mock=True)


async def test_radio_comes_back_after_a_restart_during_a_game(tmp_path, monkeypatch):
    """The radio was paused for a game with stop_playback() (playing=false on disk) and an in-memory flag: after a
    core restart, a reboot or a power cut during the game it never came back."""
    _setup(tmp_path, monkeypatch)
    radio = await _radio(tmp_path)
    await radio.play(RadioStation(uuid="a", name="A", url="http://a/s"))
    await _games(radio).launch("deadeus")
    assert radio.want_play is False
    # fnav-core restarted, the game goes on: the radio waits for it to end
    radio2 = await _radio(tmp_path)
    games2 = _games(radio2)

    async def active():
        return True

    async def inactive():
        return False

    monkeypatch.setattr(games2, "_is_active", active)
    await games2._adopt()
    assert games2.running and radio2.want_play is False
    monkeypatch.setattr(games2, "_is_active", inactive)
    await games2._check()  # the game was closed from its own menu
    assert not games2.running and radio2.want_play and radio2.station.uuid == "a"
    # a power cut during a game: no game after the reboot - the radio comes back at once
    await radio2.pause_for_game()
    radio3 = await _radio(tmp_path)
    assert radio3.want_play is False
    await _games(radio3)._adopt()  # --mock: no game is running
    assert radio3.want_play is True and radio3.player.loads[-1] == "http://a/s"
    # the radio was off: a restart without a game changes nothing
    await radio3.stop_playback()
    radio4 = await _radio(tmp_path)
    await _games(radio4)._adopt()
    assert radio4.want_play is False


async def test_core_pauses_the_radio_for_a_game(data_dir):
    async with running_app() as c:
        core = c.app.state.core
        await core.radio.play(RadioStation(uuid="a", name="A", url="http://x/a"))
        assert await core._pause_radio_for_game() is True
        assert json.loads((data_dir / "radio_state.json").read_text())["paused_for_game"] is True
        assert core.radio.state().state == "stopped"
        await core._resume_radio_after_game()
        assert core.radio.want_play and core.radio.station.uuid == "a"
        await core.radio.stop_playback()
        await core._resume_radio_after_game()  # not paused for a game: nothing
        assert core.radio.want_play is False
