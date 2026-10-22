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
    got = parse_stations([ST, {"name": "bad", "url": "file:///etc/passwd"}, {"name": "empty"}, "junk"])
    assert len(got) == 1 and got[0].url == "http://jazz.example/stream" and got[0].name == "Jazz FM"
    assert got[0].bitrate == 128 and got[0].countrycode == "GB" and got[0].hls is False


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
    await pl.load("http://s/x", "alsa/bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp")  # то же — не перезапускаем
    assert loads() == n
    await pl.load("http://s/y", "alsa/bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp")
    await asyncio.sleep(0.05)
    assert loads() == n + 1
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
