"""Радио: каталог radio-browser, выбор трека, машина состояний, mpv IPC, API; Bluetooth: BlueZ-логика и API."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from core_helpers import login, make_opts, running_app

from fnav.core import radio as rmod
from fnav.core.bluetooth import (
    ADAPTER_IF,
    DEVICE_IF,
    OM_IF,
    PROPS_IF,
    BluetoothError,
    BlueZBluetooth,
    FakeBluetooth,
    is_audio,
    norm_mac,
)
from fnav.core.radio import (
    MpvPlayer,
    RadioDirectory,
    RadioService,
    bluealsa_device,
    parse_stations,
    pick_title,
)
from fnav.models import BluetoothSettings, RadioOutput, RadioSettings, RadioStation

ST = {"stationuuid": "u1", "name": " Jazz FM ", "url": "http://x/pls", "url_resolved": "http://jazz.example/stream",
      "tags": "jazz,smooth", "countrycode": "GB", "codec": "MP3", "bitrate": 128, "hls": 0}


# ── каталог ──


def test_parse_stations():
    got = parse_stations([ST, {"name": "bad", "url": "file:///etc/passwd"}, {"name": "empty"}, "junk",
                          {"stationuuid": "u2", "url": "http://plain.example/s", "url_resolved": ""},
                          {"stationuuid": "u3", "url": "mms://old.example/s", "url_resolved": "http://new/s"}])
    assert len(got) == 3 and got[0].url == "http://jazz.example/stream" and got[0].name == "Jazz FM"
    assert got[0].bitrate == 128 and got[0].countrycode == "GB" and got[0].hls is False
    # the catalogue's own address is kept too: it outlives a resolved CDN address with a token in it
    assert got[0].url_original == "http://x/pls"
    assert got[1].url == "http://plain.example/s" and got[1].url_original == ""  # the same address: not twice
    assert got[2].url == "http://new/s" and got[2].url_original == ""  # not http(s): mpv must not open it


async def test_directory_search_mirror_and_retry():
    calls: list[str] = []
    fail_first = {"n": 1}

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        assert req.headers["user-agent"].startswith("fallout.nav/")
        if req.url.path == "/json/servers":
            return httpx.Response(200, json=[{"name": "de1.api.radio-browser.info"}])
        if req.url.path == "/json/stations/search":
            if fail_first["n"]:
                fail_first["n"] -= 1
                return httpx.Response(502)
            return httpx.Response(200, json=[ST])
        return httpx.Response(404)

    d = RadioDirectory(transport=httpx.MockTransport(handler), retry_pause_s=0)
    res = await d.by_tag("news")
    assert res is not None and res[0].uuid == "u1"
    q = parse_qs(urlparse([c for c in calls if "search" in c][-1]).query)
    assert q["tag"] == ["news"] and q["language"] == ["english"] and q["hidebroken"] == ["true"]
    assert q["order"] == ["clickcount"] and q["reverse"] == ["true"] and q["limit"] == ["60"]
    assert sum("/json/servers" in c for c in calls) == 2  # после сбоя — новое зеркало
    q = parse_qs(urlparse((await d.top("pl")) and calls[-1]).query)
    assert q["countrycode"] == ["PL"] and q["limit"] == ["30"]
    await d.top("world")
    assert parse_qs(urlparse(calls[-1]).query)["is_https"] == ["true"]
    await d.close()


async def test_directory_unreachable():
    d = RadioDirectory(transport=httpx.MockTransport(lambda r: httpx.Response(503)), retry_pause_s=0)
    assert await d.by_name("jazz") is None
    await d.close()


def test_pick_title():
    st = RadioStation(name="Jazz FM", url="http://jazz.example/live/stream.mp3")
    assert pick_title({"icy-title": "Miles Davis - So What"}, "x", st) == "Miles Davis - So What"
    assert pick_title({"icy-title": "  "}, "stream.mp3", st) is None
    assert pick_title({"icy-name": "Jazz FM", "icy-title": "Jazz FM"}, "Jazz FM", st) is None
    assert pick_title({}, "http://jazz.example/live/stream.mp3", st) is None
    assert pick_title({}, "Nina Simone - Feeling Good", st) == "Nina Simone - Feeling Good"
    assert pick_title({"icy-title": "A  -  B -"}, None, None) == "A - B"


# ── машина состояний ──


class CtlPlayer:
    def __init__(self) -> None:
        self.cb = None
        self.loads: list[tuple[str, str]] = []
        self.halts = 0
        self.volume = None

    def on_event(self, cb) -> None:
        self.cb = cb

    async def start(self) -> None:
        self.cb("ready", True)

    async def close(self) -> None: ...

    async def load(self, url: str, device: str) -> None:
        self.loads.append((url, device))

    async def halt(self) -> None:
        self.halts += 1

    async def set_volume(self, v: int) -> None:
        self.volume = v


def _svc(tmp_path, player, settings=None, saved=None):
    d = RadioDirectory(transport=httpx.MockTransport(lambda r: httpx.Response(404)), retry_pause_s=0)

    async def save(v):
        saved.append(v)

    return RadioService(d, player, tmp_path / "radio_state.json", lambda: settings or RadioSettings(),
                        save if saved is not None else None)


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_state_machine(tmp_path, monkeypatch):
    monkeypatch.setattr(rmod, "BACKOFF_S", (0.01,))
    p = CtlPlayer()
    svc = _svc(tmp_path, p)
    await svc.start()
    st = RadioStation(uuid="u1", name="Jazz FM", url="http://jazz.example/stream", tags="jazz")
    await svc.play(st, "jazz")
    assert svc.phase == "waiting_output" and p.loads == []  # без колонки поток не открываем
    svc.set_output(RadioOutput(address="AA:BB:CC:DD:EE:FF", name="JBL", connected=True))
    await settle()
    assert svc.phase == "connecting" and p.loads == [("http://jazz.example/stream", bluealsa_device("AA:BB:CC:DD:EE:FF"))]
    p.cb("playing", True)
    p.cb("title", ({"icy-title": "Miles Davis - So What"}, None))
    assert svc.phase == "playing" and svc.status().track == "Miles Davis - So What"
    assert svc.status().genre == "Jazz" and svc.status().output == "JBL" and svc.status().output_connected
    p.cb("ended", "loading failed")
    assert svc.phase == "connecting" and "loading failed" in svc.error
    await asyncio.sleep(0.05)
    assert len(p.loads) == 2  # повтор
    svc.set_output(RadioOutput(address="AA:BB:CC:DD:EE:FF", name="JBL", connected=False))
    await settle()
    assert svc.phase == "waiting_output"
    await svc.stop_playback()
    assert svc.phase == "stopped" and json.loads((tmp_path / "radio_state.json").read_text())["playing"] is False
    assert svc.state().recent[0].uuid == "u1"


async def test_resume_on_boot(tmp_path):
    st = RadioStation(uuid="u2", name="Rock", url="http://rock.example/s")
    (tmp_path / "radio_state.json").write_text(json.dumps({"station": st.model_dump(), "playing": True,
                                                           "recent": [st.model_dump()]}))
    p = CtlPlayer()
    svc = _svc(tmp_path, p)
    svc.set_output(RadioOutput(address="AA:BB:CC:DD:EE:FF", connected=True))
    await svc.start()
    await settle()
    assert svc.phase == "connecting" and p.loads and p.loads[-1][0] == "http://rock.example/s"
    p2 = CtlPlayer()
    svc2 = _svc(tmp_path, p2, RadioSettings(resume_on_boot=False))
    svc2.set_output(RadioOutput(address="AA:BB:CC:DD:EE:FF", connected=True))
    await svc2.start()
    await settle()
    assert svc2.phase == "stopped" and p2.loads == [] and svc2.station.uuid == "u2"


async def test_volume_debounced(tmp_path, monkeypatch):
    saved: list[int] = []
    p = CtlPlayer()
    svc = _svc(tmp_path, p, saved=saved)
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep(asyncio.sleep))
    for v in (10, 20, 35):
        await svc.set_volume(v)
    await svc._vol_save
    assert p.volume == 35 and saved == [35]


def _fast_sleep(real):
    async def sleep(s, *a, **k):
        return await real(0 if s >= 0.5 else s, *a, **k)
    return sleep


# ── mpv IPC ──


async def test_mpv_ipc_roundtrip():
    got: list[dict] = []
    conns: list[asyncio.StreamWriter] = []
    state: dict = {}

    async def handle(reader, writer):
        conns.append(writer)
        while line := await reader.readline():
            msg = json.loads(line)
            got.append(msg)
            cmd = msg["command"]
            reply = {"request_id": msg["request_id"], "error": "success", "data": None}
            if cmd[0] == "get_property":
                reply["data"] = {"path": state.get("path"), "audio-device": state.get("dev"),
                                 "idle-active": False}.get(cmd[1])
                if reply["data"] is None:
                    reply["error"] = "property unavailable"
            elif cmd[:2] == ["set_property", "audio-device"]:
                state["dev"] = cmd[2]
            elif cmd[0] == "loadfile":
                state["path"] = cmd[1]
            writer.write((json.dumps(reply) + "\n").encode())
            if msg["command"][0] == "loadfile":
                for ev in ({"event": "property-change", "name": "idle-active", "data": False},
                           {"event": "property-change", "name": "core-idle", "data": False},
                           {"event": "property-change", "name": "metadata", "data": {"icy-title": "A - B"}},
                           {"event": "end-file", "reason": "error", "file_error": "loading failed"}):
                    writer.write((json.dumps(ev) + "\n").encode())
            await writer.drain()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    events: list[tuple] = []
    pl = MpvPlayer(f"tcp://127.0.0.1:{port}", reconnect_s=0.05)
    pl.on_event(lambda k, v: events.append((k, v)))
    await pl.start()
    for _ in range(100):
        if ("ready", True) in events:
            break
        await asyncio.sleep(0.01)
    assert [m["command"][0] for m in got][:5] == ["observe_property"] * 5
    await pl.load("http://s/x", "alsa/bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp")
    await asyncio.sleep(0.1)
    cmds = [m["command"] for m in got]
    assert ["set_property", "audio-device", "alsa/bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp"] in cmds
    assert ["loadfile", "http://s/x", "replace"] in cmds
    kinds = [k for k, _ in events]
    assert "playing" in kinds and ("ended", "loading failed") in events
    loads = lambda: sum(m["command"][0] == "loadfile" for m in got)
    n = loads()
    # first ready after a core restart: mpv already plays this on this speaker - adopted, not restarted
    assert await pl.adopt("http://s/x", "alsa/bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp") is True
    assert loads() == n
    assert await pl.adopt("http://s/x", "alsa/bluealsa:DEV=11:22:33:44:55:66,PROFILE=a2dp") is False
    assert await pl.adopt("http://s/y", "alsa/bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp") is False
    # load always opens the stream again, the same address too (a stream stuck in paused-for-cache)
    await pl.load("http://s/x", "alsa/bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp")
    await asyncio.sleep(0.05)
    assert loads() == n + 1
    await pl.load("http://s/y", "alsa/bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp")
    await asyncio.sleep(0.05)
    assert loads() == n + 2
    kinds = [k for k, _ in events]
    assert any(k == "title" and v[0] == {"icy-title": "A - B"} for k, v in events)
    # mpv перезапустился: соединение оборвалось → переподключение и снова ready
    n_ready = kinds.count("ready")
    conns[0].close()
    for _ in range(100):
        if [k for k, _ in events].count("ready") >= n_ready + 2:  # ready False, затем ready True
            break
        await asyncio.sleep(0.02)
    assert ("ready", False) in events and [k for k, v in events if k == "ready" and v][-1:] == ["ready"]
    await pl.close()
    server.close()


# ── Bluetooth (логика BlueZ без шины) ──


MAC = "AA:BB:CC:DD:EE:01"
DEV = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_01"


class FakeBlueZ(BlueZBluetooth):
    def __init__(self, tmp_path):
        super().__init__(tmp_path / "bt.json", lambda: BluetoothSettings())
        self.calls: list[tuple] = []
        self.bus = object()
        self.objects = {
            "/org/bluez/hci0": {ADAPTER_IF: {"Powered": True, "Discovering": False, "Address": "00:11:22:33:44:55"}},
            DEV: {DEVICE_IF: {"Address": MAC, "Alias": "JBL Go 3", "Adapter": "/org/bluez/hci0", "Paired": False,
                              "Trusted": False, "Connected": False, "RSSI": -60, "Icon": "audio-card",
                              "UUIDs": ["0000110b-0000-1000-8000-00805f9b34fb"]}},
        }

    async def _open(self):
        self._rebuild()

    async def _call(self, dest, path, iface, member, signature="", body=None, timeout=10.0):
        self.calls.append((path, iface, member))
        if member == "Pair":
            self.on_signal(path, PROPS_IF, "PropertiesChanged", [DEVICE_IF, {"Paired": True}, []])
        if member == "Connect":
            self.on_signal(path, PROPS_IF, "PropertiesChanged", [DEVICE_IF, {"Connected": True}, []])
        return []


async def test_bluez_connect_flow(tmp_path):
    bt = FakeBlueZ(tmp_path)
    outs: list = []
    bt.on_output(outs.append)
    await bt.start(background=False)
    assert bt.state().available and bt.state().devices[0].audio
    await bt.connect(MAC.lower())
    assert [c[2] for c in bt.calls] == ["Pair", "Set", "Connect"]
    assert bt.audio_device == MAC and json.loads((tmp_path / "bt.json").read_text())["audio_device"] == MAC
    assert outs[-1].connected and outs[-1].name == "JBL Go 3"
    bt.on_signal(DEV, PROPS_IF, "PropertiesChanged", [DEVICE_IF, {"Connected": False}, []])
    assert outs[-1].connected is False
    bt.on_signal("/", OM_IF, "InterfacesRemoved", [DEV, [DEVICE_IF]])
    assert bt.state().devices == []
    bt.on_signal("/org/freedesktop/DBus", "org.freedesktop.DBus", "NameOwnerChanged", ["org.bluez", ":1.5", ""])
    assert bt.state().available is False
    with pytest.raises(BluetoothError) as e:
        await bt.connect(MAC)
    assert e.value.status == 503


def test_bt_helpers():
    assert norm_mac("aa-bb-cc-dd-ee-ff") == "AA:BB:CC:DD:EE:FF"
    with pytest.raises(BluetoothError):
        norm_mac("not-a-mac")
    assert is_audio(["0000110B-0000-1000-8000-00805F9B34FB"], "") and is_audio([], "audio-headset")
    assert not is_audio(["00001124-0000-1000-8000-00805f9b34fb"], "input-keyboard")


# ── API (--mock: FakeBluetooth + FakePlayer) ──


async def test_radio_and_bt_api(data_dir):
    def directory(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/json/servers":
            return httpx.Response(200, json=[{"name": "de1.api.radio-browser.info"}])
        return httpx.Response(200, json=[ST])

    async with running_app(make_opts(radio_transport=httpx.MockTransport(directory))) as c:
        assert (await c.get("/api/radio/state")).status_code == 401
        await login(c)
        g = (await c.get("/api/radio/genres")).json()["genres"]
        assert len(g) == 24 and g[0] == {"tag": "relax", "emoji": "🌊", "label": "Calm", "kind": "mood"}
        assert (await c.get("/api/radio/stations")).status_code == 400
        sts = (await c.get("/api/radio/stations", params={"tag": "jazz"})).json()["stations"]
        assert sts[0]["uuid"] == "u1"
        r = await c.post("/api/radio/play", json={"station": {"name": "x", "url": "file:///etc/passwd"}})
        assert r.status_code == 422  # только http(s)
        r = (await c.post("/api/radio/play", json={"station": sts[0], "genre": "jazz"})).json()
        assert r["station"]["uuid"] == "u1" and r["output"]["name"] == "JBL Go 3"
        for _ in range(60):
            if (await c.get("/api/radio/state")).json()["state"] == "playing":
                break
            await asyncio.sleep(0.05)
        tel = (await c.get("/api/telemetry")).json()["radio"]
        assert tel["state"] == "playing" and tel["station"] == "Jazz FM" and tel["genre"] == "Jazz"
        assert (await c.post("/api/radio/volume", json={"volume": 101})).status_code == 422
        assert (await c.post("/api/radio/volume", json={"volume": 30})).json()["volume"] == 30
        r = await c.post("/api/radio/step", json={"delta": 1})  # как клавиша 2: по списку жанра
        assert r.status_code == 200 and r.json()["station"]["uuid"] == "u1" and r.json()["genre"] == "jazz"
        assert (await c.post("/api/radio/step", json={"delta": -1})).status_code == 200
        assert (await c.post("/api/radio/step", json={"delta": 2})).status_code == 422
        assert (await c.post("/api/radio/stop")).json()["state"] == "stopped"

        bt = (await c.get("/api/bluetooth/state")).json()
        assert bt["available"] and bt["powered"] and bt["audio_device"] == "11:22:33:44:55:01"
        assert (await c.post("/api/bluetooth/devices/bad/connect")).status_code == 400
        assert (await c.post("/api/bluetooth/devices/11:22:33:44:55:99/connect")).status_code == 404
        r = await c.post("/api/bluetooth/scan", json={"seconds": 5})
        assert r.json()["discovering"] is True
        core = c.app.state.core
        core.bt.delay_s = 0.01
        for _ in range(100):
            if "11:22:33:44:55:02" in core.bt.devices:
                break
            await asyncio.sleep(0.02)
        r = (await c.post("/api/bluetooth/devices/11:22:33:44:55:02/connect")).json()
        dev = next(d for d in r["devices"] if d["address"] == "11:22:33:44:55:02")
        assert dev["paired"] and dev["connected"] and r["audio_device"] == "11:22:33:44:55:02"
        r = (await c.delete("/api/bluetooth/devices/11:22:33:44:55:02")).json()
        assert r["audio_device"] is None and all(d["address"] != "11:22:33:44:55:02" for d in r["devices"])
        assert (await c.post("/api/bluetooth/power", json={"on": False})).json()["powered"] is False
        assert (await c.post("/api/bluetooth/scan", json={})).status_code == 409


async def test_bluez_without_adapter_does_not_touch_dbus(tmp_path, monkeypatch):
    import fnav.core.bluetooth as btmod

    monkeypatch.setattr(btmod, "SYS_BT", tmp_path / "no-bt")
    bt = btmod.BlueZBluetooth(tmp_path / "bt.json", lambda: BluetoothSettings())

    async def boom(*a, **k):
        raise AssertionError("BlueZ must not be called without an adapter")

    bt._call = boom
    await bt.start(background=False)
    st = bt.state()
    assert st.available is False and st.error == "No Bluetooth adapter"


async def test_step_through_genre_and_recent(tmp_path):
    svc = _svc(tmp_path, CtlPlayer())
    await svc.start()
    a, b, c = (RadioStation(uuid=u, name=u.upper(), url=f"http://x/{u}") for u in ("a", "b", "c"))

    async def by_tag(tag):
        return [a, b, c] if tag == "jazz" else None

    svc.directory.by_tag = by_tag
    await svc.play(a, "jazz")
    assert (await svc.step(1)).station.uuid == "b"
    assert (await svc.step(1)).station.uuid == "c"
    assert (await svc.step(1)).station.uuid == "a"  # по кругу
    assert (await svc.step(-1)).station.uuid == "c"
    # станция не из жанра (поиск, популярное) — шаги по недавним, порядок не скачет от нажатия к нажатию
    svc.genre = None
    svc._ring = None
    st = await svc.step(1)
    assert st.station.uuid == "a"  # недавние: c, a, b — следом за c идёт a
    assert (await svc.step(1)).station.uuid == "b"


async def test_step_random(tmp_path):
    settings = RadioSettings(mode="random")
    svc = _svc(tmp_path, CtlPlayer(), settings)
    await svc.start()
    catalogue = [RadioStation(uuid=f"r{i}", name=f"R{i}", url=f"http://r/{i}", tags=t)
                 for i, t in enumerate(["jazz", "rock", "news", "pop", "metal", "ambient"] * 10)]
    calls: list[int] = []

    async def random_batch(limit=50):  # radio-browser order=random: весь каталог, любой жанр
        calls.append(limit)
        return catalogue[len(calls) * 7 % 20:][:limit]

    svc.directory.random = random_batch
    jazz = RadioStation(uuid="j0", name="Jazz 0", url="http://j/0", tags="jazz")
    await svc.play(jazz, "jazz")
    assert svc.status().mode == "random"
    path = [svc.station.uuid]
    for _ in range(5):
        st = await svc.step(1)
        path.append(st.station.uuid)
        assert st.genre is None  # не привязана к жанру, из которого пришли
    assert len(set(path)) == len(path) and all(u.startswith("r") for u in path[1:])
    assert calls == [50]  # одна пачка на много нажатий
    # back: retraces the random path, then random again once the history is used up
    for want in reversed(path[:-1]):
        assert (await svc.step(-1)).station.uuid == want
    assert (await svc.step(-1)).station.uuid != path[0]
    # in order again
    settings.mode = "order"
    stations = [RadioStation(uuid=str(i), name=f"S{i}", url=f"http://x/{i}") for i in range(8)]

    async def by_tag(tag):
        return list(stations)

    svc.directory.by_tag = by_tag
    await svc.play(stations[3], "jazz")
    assert (await svc.step(1)).station.uuid == "4"
    assert svc.status().mode == "order"


async def test_step_random_without_catalogue_uses_the_genre_list(tmp_path):
    svc = _svc(tmp_path, CtlPlayer(), RadioSettings(mode="random"))
    await svc.start()
    stations = [RadioStation(uuid=str(i), name=f"S{i}", url=f"http://x/{i}") for i in range(6)]

    async def nothing(limit=50):
        return None

    async def by_tag(tag):
        return list(stations)

    svc.directory.random, svc.directory.by_tag = nothing, by_tag
    await svc.play(stations[0], "jazz")
    st = await svc.step(1)
    assert st.station.uuid in {"1", "2", "3", "4", "5"} and st.genre == "jazz"


def test_old_shuffle_setting_becomes_random_mode():
    assert RadioSettings.model_validate({"shuffle": True}).mode == "random"
    assert RadioSettings.model_validate({"shuffle": False}).mode == "order"
    assert RadioSettings.model_validate({"shuffle": True, "mode": "favorites"}).mode == "favorites"
    assert "shuffle" not in RadioSettings().model_dump()


async def test_favorites(tmp_path):
    settings = RadioSettings()
    svc = _svc(tmp_path, CtlPlayer(), settings)
    await svc.start()
    a, b, c, d = (RadioStation(uuid=u, name=u.upper(), url=f"http://x/{u}") for u in "abcd")

    async def by_tag(tag):
        return [a, b, c, d]

    svc.directory.by_tag = by_tag
    await svc.play(b, "jazz")
    assert await svc.add_favorite(from_keypad=True) is True
    assert await svc.add_favorite(from_keypad=True) is False  # already there
    assert await svc.add_favorite(d) is True
    st = svc.status()
    assert st.favorite is True and st.favorites == 2 and st.fav_seq == 2  # only the keypad presses count
    # favorites mode: through the favorites in the order they were added, the genre is kept
    settings.mode = "favorites"
    await svc.play(a, "jazz")
    assert svc.status().favorite is False
    assert (await svc.step(1)).station.uuid == "b"  # not a favorite now: forward from the first one
    assert (await svc.step(1)).station.uuid == "d"
    assert (await svc.step(1)).station.uuid == "b"
    assert (await svc.step(-1)).station.uuid == "d"
    assert svc.genre == "jazz"
    # kept across a restart
    svc2 = _svc(tmp_path, CtlPlayer(), settings)
    await svc2.start()
    assert [s.uuid for s in svc2.favorites] == ["b", "d"]
    # removed (web UI): the last one gone -> favorites mode steps like "order"
    await svc.remove_favorite(b)
    await svc.remove_favorite(RadioStation(uuid="d", name="?", url="http://x/other"))
    assert svc.favorites == [] and svc.status().favorites == 0
    assert (await svc.step(1)).station.uuid == "a"  # d is the last of the genre: round to a


async def test_bt_restarts_a_crashed_chip(tmp_path, monkeypatch):
    """The CYW43455 firmware crashed ("hci0: hardware error"): the adapter powered off by itself and refuses to
    power on. One failed attempt is enough to restart the chip (it never comes back without a restart), at most
    once in RESET_EVERY_S; "Busy" (the kernel is still resetting it) does not count; an adapter switched off
    from the web UI stays off."""
    from fnav.core import bluetooth as btmod

    class Crashed(FakeBluetooth):
        dead = True
        busy = False

        async def _power(self, on):
            if on and self.busy:
                raise BluetoothError(502, "Busy")
            if on and self.dead:
                raise BluetoothError(502, "Failed: Authentication Failed")
            await super()._power(on)

    bt = Crashed(tmp_path / "bt.json", lambda: BluetoothSettings(), delay_s=0)
    resets = []

    async def reset():
        resets.append(1)
        bt.dead = False

    bt.reset_controller = reset
    bt.busy = True
    await bt.start(background=False)
    await bt._tick()
    assert bt.powered is False and bt._power_failures == 0 and resets == []  # Busy: not a crash (yet)
    bt.busy = False
    await bt._tick()  # refuses to power on: restart the chip at once
    assert resets == [1]
    await bt._tick()
    assert bt.powered is True and bt._power_failures == 0
    # crashes again within 5 minutes: retries, but no second restart yet
    bt.dead, bt.powered = True, False
    for _ in range(3):
        await bt._tick()
    assert resets == [1]
    monkeypatch.setattr(btmod.time, "monotonic", lambda: 10**9)
    await bt._tick()
    assert resets == [1, 1]
    # switched off on purpose: maintenance leaves it off
    await bt._tick()
    assert bt.powered
    await bt.set_power(False)
    await bt._tick()
    assert bt.powered is False


async def test_bt_crash_is_handled_without_waiting(tmp_path, monkeypatch):
    """BlueZ reporting the adapter powered off by itself wakes maintenance right away (after the kernel's own
    reset), not on the next 30 s round; as soon as the restarted chip is up, the speaker is connected again."""
    from fnav.core import bluetooth as btmod

    monkeypatch.setattr(btmod, "AUTO_CONNECT_S", 3600.0)  # only the wake-ups drive maintenance here
    monkeypatch.setattr(btmod, "CRASH_SETTLE_S", 0.01)
    adapter = "/org/bluez/hci0"

    class Crashing(FakeBlueZ):
        dead = False

        async def _power(self, on):
            if on and self.dead:
                raise BluetoothError(502, "Failed: Authentication Failed")
            self.on_signal(adapter, PROPS_IF, "PropertiesChanged", [ADAPTER_IF, {"Powered": on}, []])

    bt = Crashing(tmp_path)
    bt.audio_device = MAC
    bt.objects[DEV][DEVICE_IF].update(Paired=True, Trusted=True, Connected=True)
    resets = []

    async def reset():
        resets.append(1)
        bt.dead = False
        # bluetoothd powers the restarted chip on by itself (AutoEnable)
        bt.on_signal(adapter, PROPS_IF, "PropertiesChanged", [ADAPTER_IF, {"Powered": True}, []])

    bt.reset_controller = reset
    await bt.start()
    try:
        bt.dead = True  # "hci0: hardware error": the speaker is gone, the adapter powered off
        bt.on_signal(DEV, PROPS_IF, "PropertiesChanged", [DEVICE_IF, {"Connected": False}, []])
        bt.on_signal(adapter, PROPS_IF, "PropertiesChanged", [ADAPTER_IF, {"Powered": False}, []])
        for _ in range(200):
            if bt.devices[MAC].connected:
                break
            await asyncio.sleep(0.01)
        assert resets == [1] and bt.powered and bt.devices[MAC].connected
        assert [c[2] for c in bt.calls] == ["Connect"]
    finally:
        await bt.stop()


# ── повторы, обрывы, свежий адрес (B16) ──


OUT = RadioOutput(address="AA:BB:CC:DD:EE:FF", name="JBL", connected=True)


async def until(cond, timeout: float = 2.0) -> None:
    for _ in range(int(timeout / 0.005)):
        if cond():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("timed out")


async def _playing_svc(tmp_path, station, player=None, settings=None):
    p = player or CtlPlayer()
    svc = _svc(tmp_path, p, settings)
    svc.set_output(OUT)
    await settle()
    await svc.start()
    await settle()
    await svc.play(station, "jazz")
    await settle()
    return svc, p


async def test_short_plays_do_not_reset_the_backoff_and_end_in_error(tmp_path, monkeypatch):
    """A station that plays a short "not available in your country" clip and closes: the pause between retries
    keeps growing (it used to restart from 2 s on every "playing"), and after FLAPS_MAX such stops in a row the
    radio gives up with the error state instead of looping forever."""
    monkeypatch.setattr(rmod, "BACKOFF_S", (0.01, 0.02, 0.03))
    st = RadioStation(uuid="geo", name="Geo FM", url="http://geo.example/s")
    svc, p = await _playing_svc(tmp_path, st)
    for i in range(rmod.FLAPS_MAX - 1):
        n = len(p.loads)
        p.cb("playing", True)
        assert svc.phase == "playing"
        if i == 1:
            p.cb("buffering", True)  # mpv waits for more data before it notices the end: still a short play
        p.cb("ended", "eof")
        assert svc.phase == "connecting" and svc._attempt == i + 1
        await until(lambda n=n: len(p.loads) > n)
    halts = p.halts
    p.cb("playing", True)
    p.cb("ended", "eof")
    assert svc.phase == "error" and "right after it starts" in svc.error and svc.want_play is False
    await until(lambda: p.halts > halts)  # mpv is silenced
    n = len(p.loads)
    await asyncio.sleep(0.1)
    assert len(p.loads) == n  # no more retries
    assert json.loads((tmp_path / "radio_state.json").read_text())["playing"] is False
    assert svc.state().state == "error" and svc.status().state == "error"
    svc.set_output(OUT.model_copy(update={"connected": False}))  # the error stays until a command
    await settle()
    assert svc.phase == "error"
    svc.set_output(OUT)
    await svc.resume()  # Play / knob press: from scratch
    assert svc.phase == "connecting" and svc.error is None and svc._attempt == 0 and svc._flaps == 0


async def test_steady_play_resets_the_backoff(tmp_path, monkeypatch):
    monkeypatch.setattr(rmod, "BACKOFF_S", (0.01, 0.02, 0.03))
    monkeypatch.setattr(rmod, "STEADY_S", 0.05)
    svc, p = await _playing_svc(tmp_path, RadioStation(uuid="a", name="A", url="http://a/s"))
    for _ in range(2):  # no internet: the stream does not open
        n = len(p.loads)
        p.cb("ended", "loading failed")
        await until(lambda n=n: len(p.loads) > n)
    assert svc._attempt == 2
    p.cb("playing", True)
    await asyncio.sleep(0.06)  # longer than STEADY_S without a break
    p.cb("buffering", True)  # the signal is gone again: the pauses start from the beginning
    assert svc.phase == "connecting" and svc._attempt == 0 and svc._flaps == 0
    p.cb("playing", True)
    p.cb("ended", "eof")  # a short bit after the dropout: one flap only
    assert svc._attempt == 1 and svc._flaps == 1


async def test_playing_cancels_a_pending_retry(tmp_path, monkeypatch):
    """A .pls/.m3u: the first address failed, mpv went on to the second one by itself - the retry scheduled for
    the failure must not reload the playlist from the first address 2 s later (2 s of music, a pause, forever)."""
    monkeypatch.setattr(rmod, "BACKOFF_S", (0.05,))
    svc, p = await _playing_svc(tmp_path, RadioStation(uuid="p", name="P", url="http://p/list.pls"))
    assert len(p.loads) == 1
    p.cb("ended", "loading failed")
    assert svc.phase == "connecting" and svc._retry is not None
    p.cb("playing", True)
    assert svc.phase == "playing" and svc._retry is None and svc._watchdog is None
    await asyncio.sleep(0.12)
    assert len(p.loads) == 1 and svc.phase == "playing"


class AdoptPlayer(CtlPlayer):
    """mpv (its own service) still plays the station after fnav-core restarted."""

    def __init__(self) -> None:
        super().__init__()
        self.adopts: list[str] = []

    async def adopt(self, url: str, device: str) -> bool:
        self.adopts.append(url)
        self.cb("playing", True)
        return True


async def test_retries_reload_and_only_the_first_ready_adopts(tmp_path, monkeypatch):
    """The first ready after a core restart keeps what mpv already plays; retries and the watchdog always reload
    (a stream stuck in paused-for-cache used to get "No audio from the station" forever and never a loadfile)."""
    monkeypatch.setattr(rmod, "BACKOFF_S", (0.01,))
    monkeypatch.setattr(rmod, "CONNECT_TIMEOUT_S", 0.03)
    st = RadioStation(uuid="h", name="HLS", url="http://h/live.m3u8")
    (tmp_path / "radio_state.json").write_text(json.dumps({"station": st.model_dump(), "playing": True}))
    p = AdoptPlayer()
    svc = _svc(tmp_path, p)
    svc.set_output(OUT)  # the speaker is known before mpv is (bluetooth starts first)
    await settle()
    await svc.start()
    await settle()
    assert p.adopts == ["http://h/live.m3u8"] and p.loads == [] and svc.phase == "playing"
    p.cb("buffering", True)  # paused-for-cache after a long dropout
    await until(lambda: len(p.loads) == 1)  # watchdog -> retry -> loadfile of the same address
    assert p.loads == [("http://h/live.m3u8", bluealsa_device(OUT.address))]
    p.cb("ready", False)  # mpv restarted: open again, no adopting any more
    p.cb("ready", True)
    await until(lambda: len(p.loads) == 2)
    assert p.adopts == ["http://h/live.m3u8"]


async def test_retries_try_a_fresh_address(tmp_path, monkeypatch):
    """Saved stations keep the catalogue's own address and the uuid: a resolved CDN address with an expired token
    no longer hangs in "connecting" - retries go round the original address and a fresh one from /json/url/<uuid>."""
    monkeypatch.setattr(rmod, "BACKOFF_S", (0.01,))
    st = RadioStation(uuid="stw", name="KLOS", url="http://cdn.example/klos.mp3?token=old",
                      url_original="http://playerservices.example/redirect/KLOS.mp3")
    asked: list[str] = []

    async def station_url(uuid):
        asked.append(uuid)
        return "http://cdn.example/klos.mp3?token=new"

    async def note_played(uuid):  # the catalogue was not opened yet: no mirror, no click
        return None

    p = CtlPlayer()
    svc = _svc(tmp_path, p)
    svc.directory.station_url, svc.directory.note_played = station_url, note_played
    svc.set_output(OUT)
    await settle()
    await svc.start()
    await settle()
    await svc.play(st)
    await settle()
    for n in (1, 2, 3, 4):
        p.cb("ended", "loading failed")  # 403: the token expired
        await until(lambda n=n: len(p.loads) > n)
    urls = [u for u, _ in p.loads]
    assert urls == ["http://cdn.example/klos.mp3?token=old", "http://playerservices.example/redirect/KLOS.mp3",
                    "http://cdn.example/klos.mp3?token=new", "http://playerservices.example/redirect/KLOS.mp3",
                    "http://cdn.example/klos.mp3?token=old"]
    assert asked == ["stw"]  # asked on the 2nd retry in a row (and again on the 6th...)
    p.cb("playing", True)
    p.cb("title", ({}, "klos.mp3"))  # the file name of the address playing now is no track either
    assert svc.track is None

    async def note_played_fresh(uuid):  # the catalogue answered the click with a newer address: tried at once
        return "http://cdn.example/klos.mp3?token=click"

    svc.directory.note_played = note_played_fresh
    await svc.play(st)
    await settle()
    p.cb("ended", "loading failed")
    await until(lambda: p.loads[-1][0] == "http://cdn.example/klos.mp3?token=click")


async def test_directory_station_url():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/json/servers":
            return httpx.Response(200, json=[{"name": "de1.api.radio-browser.info"}])
        if req.url.path == "/json/url/u1":
            return httpx.Response(200, json={"ok": True, "url": "http://fresh.example/s"})
        if req.url.path == "/json/url/u2":
            return httpx.Response(200, json={"ok": True, "url": "file:///etc/passwd"})
        return httpx.Response(500)

    d = RadioDirectory(transport=httpx.MockTransport(handler), retry_pause_s=0)
    assert await d.note_played("u1") is None  # no mirror chosen yet: no request
    assert await d.station_url("u1") == "http://fresh.example/s" and d.mirror == "de1.api.radio-browser.info"
    assert await d.note_played("u1") == "http://fresh.example/s"
    assert await d.station_url("u2") is None
    assert await d.station_url("u3") is None and d.mirror is None  # next time - another mirror
    assert await d.station_url("") is None
    await d.close()


def test_station_url_original_is_validated():
    assert RadioStation(url="http://a").url_original == ""
    with pytest.raises(ValueError):
        RadioStation(url="http://a", url_original="file:///etc/passwd")


async def test_favorite_gets_the_original_address(tmp_path):
    old = RadioStation(uuid="f", name="F", url="http://cdn/f?token=1")  # saved before url_original existed
    svc = _svc(tmp_path, CtlPlayer())
    await svc.start()
    await svc.add_favorite(old)
    new = old.model_copy(update={"url": "http://cdn/f?token=2", "url_original": "http://f/listen.pls"})
    await svc.play(new)
    assert svc.favorites == [new]
    assert json.loads((tmp_path / "radio_state.json").read_text())["favorites"][0]["url_original"] == \
        "http://f/listen.pls"


# ── перезапуск ядра при играющем mpv, пауза ради игры (B17) ──


class ReadyPlayer(CtlPlayer):
    def __init__(self) -> None:
        super().__init__()
        self.ready = False
        self.halts_ready: list[bool] = []

    async def start(self) -> None:
        self.ready = True
        self.cb("ready", True)

    async def halt(self) -> None:
        self.halts_ready.append(self.ready)


async def test_ready_silences_mpv_when_the_radio_must_not_play(tmp_path):
    """resume_on_boot off, fnav-core restarted while the radio played: mpv (its own service) kept playing while the
    admin and the screen said Stopped. When mpv is ready and the radio must not play, it is stopped."""
    st = RadioStation(uuid="u", name="U", url="http://u/s")
    (tmp_path / "radio_state.json").write_text(json.dumps({"station": st.model_dump(), "playing": True}))
    p = ReadyPlayer()
    svc = _svc(tmp_path, p, RadioSettings(resume_on_boot=False))
    svc.set_output(OUT)
    await svc.start()
    await settle()
    assert svc.phase == "stopped" and p.loads == [] and True in p.halts_ready


async def test_paused_for_a_game_survives_a_restart(tmp_path):
    """The game pause is kept in radio_state.json: a power cut or a core restart during a game no longer loses
    the radio (it used to be stop_playback with playing=false and an in-memory flag)."""
    st = RadioStation(uuid="g", name="G", url="http://g/s")
    svc, _p = await _playing_svc(tmp_path, st)
    assert await svc.pause_for_game() is True and svc.phase == "stopped"
    saved = json.loads((tmp_path / "radio_state.json").read_text())
    assert saved["playing"] is False and saved["paused_for_game"] is True
    # restart: silent until the game is over, then plays again
    p2 = CtlPlayer()
    svc2 = _svc(tmp_path, p2)
    svc2.set_output(OUT)
    await svc2.start()
    await settle()
    assert svc2.phase == "stopped" and p2.loads == []
    await svc2.resume_after_game()
    await settle()
    assert svc2.phase == "connecting" and p2.loads[-1][0] == "http://g/s"
    assert json.loads((tmp_path / "radio_state.json").read_text())["paused_for_game"] is False
    await svc2.resume_after_game()  # nothing to resume any more
    assert len(p2.loads) == 1
    # not playing: nothing to pause; switched off during the game: stays off after it
    await svc2.stop_playback()
    assert await svc2.pause_for_game() is False
    await svc2.play(st)
    await svc2.pause_for_game()
    await svc2.stop_playback()
    await svc2.resume_after_game()
    assert svc2.want_play is False
    # resume_on_boot off: a restart forgets the game pause too
    await svc2.play(st)
    await svc2.pause_for_game()
    svc3 = _svc(tmp_path, CtlPlayer(), RadioSettings(resume_on_boot=False))
    await svc3.start()
    await svc3.resume_after_game()
    assert svc3.want_play is False


# ── Bluetooth: выключили из веб-интерфейса (B10) ──


async def test_bt_switched_off_stays_off_on_settings_changes(tmp_path, monkeypatch):
    """Saving any settings (compass auto-calibration, key 3, the radio volume) used to power the adapter on again
    after it was switched off in the web UI; and the stale "switched off" flag then kept a crashed chip from being
    recovered."""
    from fnav.core import bluetooth as btmod

    monkeypatch.setattr(btmod, "CRASH_SETTLE_S", 0.0)
    adapter = "/org/bluez/hci0"

    class Switchable(FakeBlueZ):
        async def _power(self, on):
            self.calls.append((adapter, ADAPTER_IF, f"Powered={on}"))
            self.on_signal(adapter, PROPS_IF, "PropertiesChanged", [ADAPTER_IF, {"Powered": on}, []])

    bt = Switchable(tmp_path)
    await bt.start(background=False)
    await bt.set_power(False)
    bt.calls.clear()
    for _ in range(3):
        bt.apply_settings(BluetoothSettings())
        await settle()
        await bt._tick()
    assert bt.powered is False and bt.calls == []
    # powered on outside fnav-core (bluetoothd restarted, bluetoothctl): a later chip crash is recovered again
    bt.on_signal(adapter, PROPS_IF, "PropertiesChanged", [ADAPTER_IF, {"Powered": True}, []])
    assert bt._user_off is False
    bt.on_signal(adapter, PROPS_IF, "PropertiesChanged", [ADAPTER_IF, {"Powered": False}, []])  # the chip fell over
    await bt._tick()
    assert bt.powered is True and bt.calls == [(adapter, ADAPTER_IF, "Powered=True")]
    # a stale cache (Powered still True until the "off" signal arrives) does not undo a switch-off
    bt.powered, bt._user_off = False, True  # what set_power(False) leaves once BlueZ has replied
    bt.on_signal(DEV, PROPS_IF, "PropertiesChanged", [DEVICE_IF, {"RSSI": -70}, []])
    assert bt.objects[adapter][ADAPTER_IF]["Powered"] is True and bt._user_off is True
