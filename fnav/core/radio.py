"""Интернет-радио: каталог radio-browser.info, плеер mpv (fnav-radio.service) по JSON IPC, состояние.

docs/contracts/radio.md. Каталог — как в myAIfriend (RadioBrowser.kt): случайное зеркало, повтор после паузы,
популярные станции жанра, отметка прослушивания. Звук — только в Bluetooth-колонку (bluealsa): нет колонки —
поток не открываем, колонка подключилась — играем.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from fnav.models import RadioOutput, RadioSettings, RadioState, RadioStation, RadioStatus

log = logging.getLogger("fnav.core.radio")

USER_AGENT = "fallout.nav/1.0 (car radio)"
FALLBACK_MIRROR = "all.api.radio-browser.info"
RETRY_PAUSE_S = 0.7
LIMIT_TAG, LIMIT_NAME, LIMIT_TOP = 60, 100, 30
RECENT_MAX = 12
BACKOFF_S = (2.0, 4.0, 8.0, 16.0, 30.0)
CONNECT_TIMEOUT_S = 25.0  # дольше «connecting» без звука — открыть поток заново
MPV_SOCKET = "/run/fnav-radio/mpv.sock"

# Плитки музыкальной страницы myAIfriend (MusicGenres.TILES): тег каталога, эмодзи, подпись, вид.
GENRES: list[dict[str, str]] = [
    {"tag": t, "emoji": e, "label": lbl, "kind": k}
    for t, e, lbl, k in [
        ("relax", "🌊", "Calm", "mood"), ("sleep", "🌙", "Sleep", "mood"), ("lofi", "📚", "Study", "mood"),
        ("workout", "💪", "Workout", "mood"), ("party", "🎉", "Upbeat", "mood"),
        ("romantic", "💞", "Romantic", "mood"), ("meditation", "🧘", "Meditation", "mood"),
        ("pop", "🎤", "Pop", "genre"), ("rock", "🎸", "Rock", "genre"), ("jazz", "🎷", "Jazz", "genre"),
        ("classical", "🎻", "Classical", "genre"), ("electronic", "🎛️", "Electronic", "genre"),
        ("hip hop", "🎧", "Hip-hop", "genre"), ("chanson", "🪗", "Chanson", "genre"),
        ("lounge", "🍸", "Lounge", "genre"), ("dance", "💃", "Dance", "genre"), ("blues", "🎺", "Blues", "genre"),
        ("folk", "🪕", "Folk", "genre"), ("kids", "🧸", "Kids", "genre"),
        ("hits", "🔥", "Hits", "era"), ("oldies", "📻", "Oldies", "era"), ("80s", "📼", "80s", "era"),
        ("90s", "💿", "90s", "era"), ("news", "📰", "News", "era"),
    ]
]
GENRE_LABELS = {g["tag"]: g["label"] for g in GENRES}
# Речь стоит слушать на понятном языке; музыка — откуда угодно (MusicGenres.language)
SPEECH_TAGS = {"news", "talk", "podcast", "sport", "comedy"}


class RadioError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ───────────────────────── каталог ─────────────────────────


def parse_stations(data: Any) -> list[RadioStation]:
    """JSON каталога → станции; без адреса потока или с не-http(s) адресом — пропускаем."""
    out: list[RadioStation] = []
    if not isinstance(data, list):
        return out
    for o in data:
        if not isinstance(o, dict):
            continue

        def s(k: str, o: dict = o) -> str:
            v = o.get(k)
            return "" if v is None else str(v).strip()

        def n(k: str, o: dict = o) -> int:
            try:
                return int(o.get(k) or 0)
            except (TypeError, ValueError):
                return 0

        url = s("url_resolved") or s("url")  # url_resolved — уже раскрытые .pls/.m3u
        if not re.match(r"^https?://", url, re.IGNORECASE) or len(url) > 2048:
            continue
        out.append(RadioStation(
            uuid=s("stationuuid"), name=s("name") or "Radio", url=url, homepage=s("homepage"),
            favicon=s("favicon"), tags=s("tags"), country=s("country"), countrycode=s("countrycode"),
            language=s("language"), codec=s("codec"), bitrate=n("bitrate"), votes=n("votes"),
            clickcount=n("clickcount"), hls=n("hls") == 1,
        ))
    return out


class RadioDirectory:
    """radio-browser.info: зеркало выбирается один раз за запуск, при сбое — пауза и новое зеркало."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, retry_pause_s: float = RETRY_PAUSE_S):
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=10.0), transport=transport,
                                         headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        self.mirror: str | None = None
        self.retry_pause_s = retry_pause_s

    async def close(self) -> None:
        await self._client.aclose()

    async def _pick_mirror(self) -> str:
        try:
            r = await self._client.get(f"https://{FALLBACK_MIRROR}/json/servers")
            r.raise_for_status()
            names = [x.get("name") for x in r.json() if isinstance(x, dict) and x.get("name")]
            if names:
                return random.choice(names)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("radio: mirror lookup failed (%s), using %s", e, FALLBACK_MIRROR)
        return FALLBACK_MIRROR

    async def search(self, criteria: dict[str, str], limit: int) -> list[RadioStation] | None:
        """Один поиск; None — каталог не ответил и со второй попытки."""
        q = {**criteria, "hidebroken": "true", "order": "clickcount", "reverse": "true", "limit": str(limit)}
        for attempt in range(2):
            host = self.mirror or await self._pick_mirror()
            self.mirror = host
            try:
                r = await self._client.get(f"https://{host}/json/stations/search?{urlencode(q)}")
                r.raise_for_status()
                return parse_stations(r.json())
            except (httpx.HTTPError, ValueError) as e:
                log.warning("radio: search on %s failed: %s", host, e)
            if attempt == 0:
                await asyncio.sleep(self.retry_pause_s)
                self.mirror = None
        return None

    async def by_tag(self, tag: str) -> list[RadioStation] | None:
        c = {"tag": tag}
        if tag in SPEECH_TAGS:
            c["language"] = "english"
        return await self.search(c, LIMIT_TAG)

    async def by_name(self, name: str) -> list[RadioStation] | None:
        return await self.search({"name": name}, LIMIT_NAME)

    async def top(self, country: str | None) -> list[RadioStation] | None:
        cc = (country or "").strip().upper()
        c = {"countrycode": cc} if re.fullmatch(r"[A-Z]{2}", cc) else {"is_https": "true"}
        return await self.search(c, LIMIT_TOP)

    async def note_played(self, uuid: str) -> None:
        """Каталог считает прослушивания (по ним сортирует). Ошибка не мешает музыке."""
        if not uuid or not self.mirror:
            return
        with contextlib.suppress(httpx.HTTPError):
            await self._client.get(f"https://{self.mirror}/json/url/{uuid}")


# ───────────────────────── плеер ─────────────────────────

PlayerEvent = Callable[[str, Any], None]  # ("ready"|"playing"|"buffering"|"ended"|"title", значение)


class Player(Protocol):
    def on_event(self, cb: PlayerEvent) -> None: ...
    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def load(self, url: str, device: str) -> None: ...
    async def halt(self) -> None: ...
    async def set_volume(self, volume: int) -> None: ...


def pick_title(metadata: dict | None, media_title: str | None, station: RadioStation | None) -> str | None:
    """Трек: ICY StreamTitle; иначе media-title, если это не адрес/файл и не имя станции."""
    md = {str(k).lower(): v for k, v in (metadata or {}).items()}
    for key in ("icy-title", "title"):
        t = md.get(key)
        if isinstance(t, str) and t.strip() and not _is_noise(t, station, md):
            return _clean(t)
    t = media_title
    if isinstance(t, str) and t.strip() and not _is_noise(t, station, md):
        return _clean(t)
    return None


def _clean(t: str) -> str:
    t = re.sub(r"\s+", " ", t).strip(" -|")
    return t[:200]


def _is_noise(t: str, station: RadioStation | None, md: dict) -> bool:
    """Не трек: пусто, адрес, имя файла потока (так mpv называет поток без метаданных), имя станции."""
    s = t.strip().lower()
    if not s.strip(" -|.") or "://" in s or re.search(r"\.(mp3|aac|aacp|ogg|opus|m3u8?|pls|flac)(\?.*)?$", s):
        return True
    names = {str(md.get("icy-name") or "").strip().lower()}
    if station:
        url = station.url.lower().split("?", 1)[0].rstrip("/")
        names |= {station.name.strip().lower(), url, url.rsplit("/", 1)[-1]}
    return s in names


class MpvPlayer:
    """mpv --idle по JSON IPC. Адрес: путь unix-сокета или tcp://host:port (тесты на Windows)."""

    def __init__(self, address: str = MPV_SOCKET, reconnect_s: float = 2.0) -> None:
        self.address = address
        self.reconnect_s = reconnect_s
        self._cb: PlayerEvent = lambda _k, _v: None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._rid = 0
        self._connected = asyncio.Event()
        self._props: dict[str, Any] = {}
        self.volume = 60

    def on_event(self, cb: PlayerEvent) -> None:
        self._cb = cb

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="mpv-ipc")

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._drop()

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self.address.startswith("tcp://"):
            host, _, port = self.address[6:].rpartition(":")
            return await asyncio.open_connection(host, int(port), limit=1 << 20)
        return await asyncio.open_unix_connection(self.address, limit=1 << 20)

    async def _run(self) -> None:
        logged = False
        while True:
            try:
                reader, writer = await self._open()
            except (OSError, ValueError, AttributeError, NotImplementedError) as e:  # Windows: нет unix-сокетов
                if not logged:
                    log.warning("radio: mpv IPC %s unavailable (%s), retrying", self.address, e)
                    logged = True
                await asyncio.sleep(self.reconnect_s)
                continue
            logged = False
            self._writer = writer
            self._connected.set()
            log.info("radio: connected to mpv")
            reader_task = asyncio.create_task(self._read(reader), name="mpv-ipc-read")
            try:
                for i, prop in enumerate(("metadata", "media-title", "idle-active", "core-idle",
                                          "paused-for-cache"), start=1):
                    await self.command("observe_property", i, prop)
                await self.command("set_property", "volume", self.volume)
                self._cb("ready", True)  # mpv (пере)запущен: сервис сам решит, открыть ли поток заново
                await reader_task
            except (OSError, ConnectionError, asyncio.IncompleteReadError, TimeoutError) as e:
                log.warning("radio: mpv IPC lost: %s", e)
            finally:
                reader_task.cancel()
                self._drop()
                self._cb("ready", False)
            await asyncio.sleep(self.reconnect_s)

    def _drop(self) -> None:
        self._connected.clear()
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
        self._writer = None
        for f in self._pending.values():
            if not f.done():
                f.set_exception(ConnectionError("mpv IPC closed"))
        self._pending.clear()
        self._props.clear()

    async def _read(self, reader: asyncio.StreamReader) -> None:
        while True:
            line = await reader.readline()
            if not line:
                raise ConnectionError("mpv closed the IPC socket")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "request_id" in msg and "event" not in msg:
                f = self._pending.pop(msg["request_id"], None)
                if f and not f.done():
                    if msg.get("error") == "success":
                        f.set_result(msg.get("data"))
                    else:
                        f.set_exception(RuntimeError(str(msg.get("error"))))
                continue
            self._on_mpv_event(msg)

    def _on_mpv_event(self, msg: dict) -> None:
        ev = msg.get("event")
        if ev == "property-change":
            name, data = msg.get("name"), msg.get("data")
            self._props[name] = data
            if name in ("metadata", "media-title"):
                self._cb("title", (self._props.get("metadata"), self._props.get("media-title")))
            elif name in ("core-idle", "paused-for-cache", "idle-active"):
                self._report_activity()
        elif ev == "end-file":
            reason = msg.get("reason")
            if reason in ("eof", "error"):
                self._cb("ended", msg.get("file_error") or reason)
        elif ev == "playback-restart":
            self._report_activity()

    def _report_activity(self) -> None:
        p = self._props
        if p.get("idle-active"):
            return
        if p.get("core-idle") is False and not p.get("paused-for-cache"):
            self._cb("playing", True)
        elif p.get("paused-for-cache"):
            self._cb("buffering", True)

    async def command(self, *args: Any, timeout: float = 5.0) -> Any:
        if self._writer is None:
            raise ConnectionError("mpv is not running")
        self._rid += 1
        rid = self._rid
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._writer.write((json.dumps({"command": list(args), "request_id": rid}) + "\n").encode())
        await self._writer.drain()
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    async def load(self, url: str, device: str) -> None:
        # fnav-core перезапустился, а mpv продолжает играть ту же станцию на то же устройство — не перезапускаем
        try:
            same = (await self.command("get_property", "path") == url
                    and await self.command("get_property", "audio-device") == device
                    and not await self.command("get_property", "idle-active"))
        except RuntimeError:  # «property unavailable», пока ничего не загружено
            same = False
        if same:
            self._cb("title", (self._props.get("metadata"), self._props.get("media-title")))
            self._report_activity()
            return
        await self.command("set_property", "audio-device", device)
        await self.command("set_property", "volume", self.volume)
        await self.command("loadfile", url, "replace")

    async def halt(self) -> None:
        if self.connected:
            with contextlib.suppress(ConnectionError, RuntimeError, TimeoutError):
                await self.command("stop")

    async def set_volume(self, volume: int) -> None:
        self.volume = volume
        if self.connected:
            with contextlib.suppress(ConnectionError, RuntimeError, TimeoutError):
                await self.command("set_property", "volume", volume)


class FakePlayer:
    """Режим --mock: «играет» любую станцию и меняет вымышленные ICY-заголовки."""

    TITLES = (
        "The Ink Spots - I Don't Want to Set the World on Fire",
        "Dean Martin - Ain't That a Kick in the Head",
        "Bing Crosby - Pistol Packin' Mama",
        "Patsy Cline - Walkin' After Midnight",
        "The Platters - Only You",
        "Frank Sinatra - Blue Moon",
        "Bob Crosby & The Bobcats - Big Iron on His Hip (a very long title to test the marquee)",
    )

    def __init__(self, connect_s: float = 1.2, title_every_s: float = 20.0) -> None:
        self.connect_s = connect_s
        self.title_every_s = title_every_s
        self._cb: PlayerEvent = lambda _k, _v: None
        self._task: asyncio.Task | None = None
        self.url: str | None = None
        self.device: str | None = None
        self.volume = 60

    def on_event(self, cb: PlayerEvent) -> None:
        self._cb = cb

    async def start(self) -> None:
        self._cb("ready", True)

    async def close(self) -> None:
        await self.halt()

    async def load(self, url: str, device: str) -> None:
        await self.halt()
        self.url, self.device = url, device
        self._task = asyncio.create_task(self._play(), name="fake-radio")

    async def _play(self) -> None:
        await asyncio.sleep(self.connect_s)
        self._cb("playing", True)
        i = random.randrange(len(self.TITLES))
        while True:
            self._cb("title", ({"icy-title": self.TITLES[i % len(self.TITLES)]}, None))
            i += 1
            await asyncio.sleep(self.title_every_s)

    async def halt(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        self.url = None

    async def set_volume(self, volume: int) -> None:
        self.volume = volume


# ───────────────────────── сервис ─────────────────────────


def bluealsa_device(address: str) -> str:
    return f"alsa/bluealsa:DEV={address},PROFILE=a2dp"


class RadioService:
    """Состояние радио; сохраняется в radio_state.json (станция, «играло ли», недавние)."""

    def __init__(self, directory: RadioDirectory, player: Player, state_path: Path,
                 get_settings: Callable[[], RadioSettings],
                 save_volume: Callable[[int], Awaitable[None]] | None = None,
                 test_output: bool = False) -> None:
        self.directory = directory
        self.player = player
        self.state_path = state_path
        self.get_settings = get_settings
        self.save_volume = save_volume
        self.station: RadioStation | None = None
        self.genre: str | None = None
        self.want_play = False
        self.phase = "stopped"
        self.track: str | None = None
        self.error: str | None = None
        self.recent: list[RadioStation] = []
        self.volume = get_settings().volume
        # FNAV_RADIO_TEST_OUTPUT=1 — проверка без колонки (mpv с --ao=null): «колонка» всегда подключена
        self.output: RadioOutput | None = (RadioOutput(address="null", name="Null output (test)", connected=True)
                                           if test_output else None)
        self._test_output = test_output
        self._player_ready = False
        self._attempt = 0
        self._retry: asyncio.Task | None = None
        self._watchdog: asyncio.Task | None = None
        self._vol_save: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        player.on_event(self._on_player)

    # ── жизненный цикл ──
    async def start(self, demo: RadioStation | None = None) -> None:
        self._load_state()
        if demo is not None and self.station is None:  # --mock без сохранённого состояния: сразу «играет»
            self.station, self.genre, self.want_play = demo, "oldies", True
        if self.want_play and not self.get_settings().resume_on_boot:
            self.want_play = False
        if self.want_play:
            log.info("radio: resuming %s", self.station.name if self.station else "?")
        await self.player.set_volume(self.volume)
        await self._apply()  # фаза; поток откроется по событию ready плеера
        await self.player.start()

    async def stop(self) -> None:
        for t in (self._retry, self._watchdog, self._vol_save):
            if t:
                t.cancel()
        await self.player.close()
        await self.directory.close()

    # ── состояние ──
    def _load_state(self) -> None:
        try:
            d = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        try:
            self.station = RadioStation(**d["station"]) if d.get("station") else None
            self.genre = d.get("genre") or None
            self.want_play = bool(d.get("playing")) and self.station is not None
            self.recent = [RadioStation(**x) for x in d.get("recent", [])][:RECENT_MAX]
        except (TypeError, ValueError, KeyError) as e:
            log.warning("radio: bad %s: %s", self.state_path, e)

    def _save_state(self) -> None:
        d = {"station": self.station.model_dump() if self.station else None, "genre": self.genre,
             "playing": self.want_play, "recent": [s.model_dump() for s in self.recent]}
        tmp = self.state_path.with_suffix(".tmp")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as e:
            log.warning("radio: cannot save %s: %s", self.state_path, e)

    def state(self) -> RadioState:
        return RadioState(state=self.phase, station=self.station, genre=self.genre, track=self.track,
                          volume=self.volume, output=self.output, error=self.error, recent=self.recent)

    def status(self) -> RadioStatus:
        genre = GENRE_LABELS.get(self.genre or "") if self.genre else None
        if not genre and self.station and self.station.tags:
            genre = self.station.tags.split(",")[0].strip().title() or None
        return RadioStatus(state=self.phase, station=self.station.name if self.station else None,
                           track=self.track if self.phase == "playing" else None, genre=genre,
                           volume=self.volume, output=self.output.name if self.output else None,
                           output_connected=bool(self.output and self.output.connected))

    # ── команды ──
    async def play(self, station: RadioStation, genre: str | None = None) -> RadioState:
        self.station, self.genre, self.want_play = station, genre, True
        self.recent = [station] + [s for s in self.recent if (s.uuid or s.url) != (station.uuid or station.url)]
        self.recent = self.recent[:RECENT_MAX]
        self._attempt = 0
        await asyncio.to_thread(self._save_state)
        asyncio.create_task(self.directory.note_played(station.uuid), name="radio-click")
        await self._apply()
        return self.state()

    async def resume(self) -> RadioState:
        if self.station is None:
            raise RadioError(409, "No station to resume")
        return await self.play(self.station, self.genre)

    async def stop_playback(self) -> RadioState:
        self.want_play = False
        await asyncio.to_thread(self._save_state)
        await self._apply()
        return self.state()

    async def set_volume(self, volume: int) -> RadioState:
        self.volume = max(0, min(100, int(volume)))
        await self.player.set_volume(self.volume)
        if self._vol_save:
            self._vol_save.cancel()
        self._vol_save = asyncio.create_task(self._save_volume_later(self.volume), name="radio-volume")
        return self.state()

    async def _save_volume_later(self, v: int) -> None:
        await asyncio.sleep(1.0)  # ползунок двигают — в settings.json пишем один раз
        if self.save_volume:
            await self.save_volume(v)

    def apply_settings(self, s: RadioSettings) -> None:
        if s.volume != self.volume and (self._vol_save is None or self._vol_save.done()):
            self.volume = s.volume
            asyncio.get_running_loop().create_task(self.player.set_volume(s.volume))

    def set_output(self, out: RadioOutput | None) -> None:
        """Колонка по умолчанию сменилась / подключилась / отключилась (от bluetooth.py)."""
        if self._test_output:
            return
        before = (self.output.address, self.output.connected) if self.output else None
        self.output = out
        after = (out.address, out.connected) if out else None
        if before != after:
            self._attempt = 0
            asyncio.get_running_loop().create_task(self._apply(), name="radio-output")

    # ── машина состояний ──
    def _cancel_timers(self) -> None:
        for t in (self._retry, self._watchdog):
            if t and t is not asyncio.current_task():
                t.cancel()
        self._retry = self._watchdog = None

    async def _apply(self) -> None:
        async with self._lock:
            self._cancel_timers()
            self.track = None
            if not self.want_play or self.station is None:
                self.phase, self.error = "stopped", None
                await self.player.halt()
                return
            if not (self.output and self.output.connected):
                self.phase, self.error = "waiting_output", None  # без колонки поток не открываем (трафик)
                await self.player.halt()
                return
            self.phase = "connecting"
            if not self._player_ready:
                return  # mpv ещё не поднялся: откроем по событию ready
            device = "auto" if self._test_output else bluealsa_device(self.output.address)
            try:
                await self.player.load(self.station.url, device)
            except (ConnectionError, RuntimeError, TimeoutError, OSError) as e:
                self._schedule_retry(f"Player: {e}")
                return
            self._watchdog = asyncio.create_task(self._connect_watchdog(), name="radio-watchdog")

    async def _connect_watchdog(self) -> None:
        await asyncio.sleep(CONNECT_TIMEOUT_S)
        if self.phase == "connecting" and self.want_play:
            self._schedule_retry("No audio from the station")

    def _schedule_retry(self, why: str) -> None:
        if not self.want_play:
            return
        delay = BACKOFF_S[min(self._attempt, len(BACKOFF_S) - 1)]
        self._attempt += 1
        self.phase, self.error, self.track = "connecting", why, None
        log.info("radio: %s - retry in %.0f s", why, delay)
        if self._retry and not self._retry.done() and self._retry is not asyncio.current_task():
            self._retry.cancel()

        async def later() -> None:
            await asyncio.sleep(delay)
            if self.want_play:
                await self._apply()

        self._retry = asyncio.get_running_loop().create_task(later(), name="radio-retry")

    def _on_player(self, kind: str, value: Any) -> None:
        loop = asyncio.get_running_loop()
        if kind == "ready":
            self._player_ready = bool(value)
            if value and self.want_play:
                loop.create_task(self._apply(), name="radio-ready")  # mpv перезапустился — открыть заново
            elif not value and self.want_play and self.phase == "playing":
                self.phase = "connecting"
        elif kind == "playing":
            if self.want_play and self.phase in ("connecting", "playing"):
                if self.phase != "playing":
                    log.info("radio: playing %s", self.station.name if self.station else "?")
                self.phase, self.error, self._attempt = "playing", None, 0
                if self._watchdog:
                    self._watchdog.cancel()
                    self._watchdog = None
        elif kind == "buffering":
            if self.want_play and self.phase == "playing":
                self.phase = "connecting"
                self._watchdog = loop.create_task(self._connect_watchdog(), name="radio-watchdog")
        elif kind == "ended":
            if self.want_play and self.phase in ("connecting", "playing"):
                self._schedule_retry(f"Stream ended: {value}" if value and value != "eof" else "Stream ended")
        elif kind == "title":
            md, mt = value if isinstance(value, tuple) else (None, None)
            t = pick_title(md, mt, self.station)
            if t != self.track and self.want_play:
                self.track = t
                if t:
                    log.debug("radio: track %s", t)


MOCK_STATION = RadioStation(uuid="mock-rnv", name="Radio New Vegas", url="http://radio.invalid/new-vegas",
                            tags="oldies,swing,country", country="United States", countrycode="US",
                            codec="MP3", bitrate=128)


def create_radio(mock: bool, data_dir: Path, get_settings: Callable[[], RadioSettings],
                 save_volume: Callable[[int], Awaitable[None]] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 player: Player | None = None) -> RadioService:
    if player is None:
        player = FakePlayer() if mock else MpvPlayer(os.environ.get("FNAV_RADIO_IPC", MPV_SOCKET))
    test_output = os.environ.get("FNAV_RADIO_TEST_OUTPUT", "") == "1"
    return RadioService(RadioDirectory(transport=transport), player, data_dir / "radio_state.json",
                        get_settings, save_volume, test_output=test_output)

