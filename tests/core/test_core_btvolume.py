"""Одна громкость колонки (AVRCP через bluealsa): уровни, порядок установки, эхо, кнопки колонки, сирена."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx

from fnav.core import btvolume as bv
from fnav.core.radio import RadioDirectory, RadioService
from fnav.models import RadioOutput, RadioSettings

MAC = "20:64:DE:95:DD:60"
PCM = "/org/bluealsa/hci0/dev_20_64_DE_95_DD_60/a2dpsrc/sink"
PCM_PROPS = {"Device": "/org/bluez/hci0/dev_20_64_DE_95_DD_60", "Transport": "A2DP-source", "Mode": "sink",
             "SoftVolume": True, "Volume": bv.pack(127)}


def test_levels_and_packing():
    assert bv.pct_to_level(0) == 0 and bv.pct_to_level(100) == 127 and bv.pct_to_level(47) == 60
    assert bv.level_to_pct(127) == 100 and bv.level_to_pct(60) == 47
    assert bv.pack(60) == 0x3C3C and bv.unpack(0x3C3C) == 60
    assert bv.unpack(0x80BC) == 60  # бит mute — не громкость; каналы разные — по большему


class Clock:
    t = 100.0

    def __call__(self) -> float:
        return self.t


class FakeSpeakerBus(bv.SpeakerVolume):
    """Шина подменена: GetManagedObjects отдаёт PCM колонки, Set — записывается."""

    def __init__(self, objects: dict | None = None, **kw) -> None:
        super().__init__(**kw)
        self.objects = {PCM: {bv.PCM_IF: dict(PCM_PROPS)}} if objects is None else objects
        self.sets: list[tuple[str, object]] = []
        self.bus = object()

    async def start(self) -> None:
        await self._find()

    async def _call(self, dest, path, iface, member, signature="", body=None, timeout=3.0):
        if member == "GetManagedObjects":
            return [self.objects]
        if member == "Set":
            self.sets.append((body[1], body[2][1]))
        return []

    def signal(self, member: str, path: str, body: list) -> None:
        self._on_message(SimpleNamespace(member=member, path=path, body=body))


async def test_apply_sets_level_first_then_native_and_ignores_echo():
    changes: list[int] = []
    clk = Clock()
    sp = FakeSpeakerBus(on_change=changes.append, mono=clk)
    assert not await sp.apply(50)  # колонка не выбрана
    sp.select(MAC)
    await sp.start()
    assert sp.pcm == PCM and not sp.native
    assert await sp.apply(47)
    assert sp.sets == [("Volume", bv.pack(60)), ("SoftVolume", False)] and sp.native  # колонка не «прыгает»
    assert await sp.apply(47) and len(sp.sets) == 2  # то же — без вызовов
    sp.signal("PropertiesChanged", PCM, [bv.PCM_IF, {"Volume": bv.pack(59)}, []])  # колонка округлила шаг
    assert changes == []
    clk.t += 5
    sp.signal("PropertiesChanged", PCM, [bv.PCM_IF, {"Volume": bv.pack(70)}, []])  # кнопка на колонке
    assert changes == [55]
    sp.signal("PropertiesChanged", "/other", [bv.PCM_IF, {"Volume": bv.pack(10)}, []])
    assert changes == [55]
    sp.signal("InterfacesRemoved", PCM, [PCM, [bv.PCM_IF]])
    assert sp.pcm is None and not await sp.apply(47)  # колонка отключилась — громкость в mpv


async def test_pcm_appears_later_and_only_for_our_speaker():
    pcms: list[int] = []
    sp = FakeSpeakerBus(objects={}, on_pcm=lambda: pcms.append(1))
    sp.select(MAC)
    await sp.start()
    assert sp.pcm is None
    other = dict(PCM_PROPS, Device="/org/bluez/hci0/dev_11_22_33_44_55_66")
    sp.signal("InterfacesAdded", "/org/bluealsa", ["/org/bluealsa/hci0/dev_11_22_33_44_55_66/a2dpsrc/sink",
                                                   {bv.PCM_IF: other}])
    assert sp.pcm is None
    sp.signal("InterfacesAdded", "/org/bluealsa", [PCM, {bv.PCM_IF: dict(PCM_PROPS, SoftVolume=False)}])
    assert sp.pcm == PCM and sp.native and pcms == [1]


class CtlPlayer:
    def __init__(self) -> None:
        self.cb = None
        self.volume: int | None = None
        self.loads: list = []

    def on_event(self, cb) -> None:
        self.cb = cb

    async def start(self) -> None:
        self.cb("ready", True)

    async def close(self) -> None: ...

    async def load(self, url, device) -> None:
        self.loads.append(url)

    async def halt(self) -> None: ...

    async def set_volume(self, v: int) -> None:
        self.volume = v


class FakeSpeaker:
    def __init__(self) -> None:
        self.on_change = None
        self.on_pcm = None
        self.address = None
        self.levels: list[int] = []
        self.ok = True

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def select(self, address) -> None:
        self.address = address

    async def apply(self, pct: int) -> bool:
        if self.ok and self.address:
            self.levels.append(pct)
            return True
        return False


async def test_radio_volume_lives_in_the_speaker(tmp_path, monkeypatch):
    saved: list[int] = []

    async def save(v):
        saved.append(v)

    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda s, *a, **k: real_sleep(0 if s >= 0.5 else s, *a, **k))
    p, sp = CtlPlayer(), FakeSpeaker()
    svc = RadioService(RadioDirectory(transport=httpx.MockTransport(lambda r: httpx.Response(404))), p,
                       tmp_path / "radio.json", lambda: RadioSettings(volume=40), save, speaker=sp)
    await svc.start()
    assert p.volume == 40 and sp.levels == []  # колонки нет — громкость в mpv
    svc.set_output(RadioOutput(address=MAC, name="ULT FIELD 7", connected=True))
    await svc.set_volume(30)
    assert sp.levels[-1] == 30 and p.volume == 100  # в колонке; mpv — без ослабления
    sp.on_change(55)  # кнопка «громче» на колонке
    await svc._vol_save
    assert svc.volume == 55 and svc.status().volume == 55 and saved[-1] == 55
    assert await svc.play_alert(str(tmp_path / "siren.wav")) is None
    assert sp.levels[-1] == 100  # сирена — на всю колонку
    sp.on_change(20)  # кнопки во время сирены громкость радио не меняют
    assert svc.volume == 55
    await svc.end_alert()
    assert sp.levels[-1] == 55
    sp.ok = False  # колонка без AVRCP-громкости
    await svc.set_volume(25)
    assert p.volume == 25
    await svc.stop()
