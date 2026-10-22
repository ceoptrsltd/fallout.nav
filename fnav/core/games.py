"""Игры на экране машины: RetroArch (Game Boy, Doom и т.д.) вместо навигации, пока стоим.

Запуск — служба fnav-games (cage + RetroArch, конфликтует с fnav-display: экран навигации останавливается и
возвращается сам, когда игра закрыта). Что запускать, служба берёт из /run/fnav-core/game.args (две строки:
ядро libretro, файл игры) — так sudo разрешает ровно «systemctl start/stop fnav-games» и ничего больше.
Пока идёт игра, радио на паузе; машина поехала — игра закрывается.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from fnav.core.system import run

log = logging.getLogger("fnav.core.games")

UNIT = "fnav-games.service"
ARGS_FILE = Path(os.environ.get("FNAV_GAME_ARGS", "/run/fnav-core/game.args"))
CONTENT_DIR = Path(os.environ.get("FNAV_GAMES_DIR", "/var/lib/fnav/games/content"))
CORE_DIRS = [Path("/opt/fnav/games/cores"), Path("/usr/lib/aarch64-linux-gnu/libretro"),
             Path("/usr/lib/x86_64-linux-gnu/libretro"), Path("/usr/lib/libretro")]
DOOM_DIR = Path("/usr/share/games/doom")
MAX_START_SPEED_MPS = 2.0  # играть — только стоя
STOP_SPEED_MPS = 2.5  # поехали быстрее…
STOP_AFTER_S = 3.0  # …дольше 3 с — игра закрывается

# расширение файла → (система, ядро libretro)
SYSTEMS: dict[str, tuple[str, str]] = {
    ".gb": ("Game Boy", "gambatte_libretro.so"),
    ".gbc": ("Game Boy Color", "gambatte_libretro.so"),
    ".gba": ("Game Boy Advance", "mgba_libretro.so"),
    ".nes": ("NES", "nestopia_libretro.so"),
    ".sfc": ("SNES", "snes9x_libretro.so"),
    ".smc": ("SNES", "snes9x_libretro.so"),
    ".md": ("Mega Drive", "genesis_plus_gx_libretro.so"),
    ".gen": ("Mega Drive", "genesis_plus_gx_libretro.so"),
    ".sms": ("Master System", "genesis_plus_gx_libretro.so"),
    ".gg": ("Game Gear", "genesis_plus_gx_libretro.so"),
    ".wad": ("Doom", "prboom_libretro.so"),
}
KNOWN_TITLES = {"freedoom1": "Freedoom: Phase 1", "freedoom2": "Freedoom: Phase 2"}


@dataclass(frozen=True)
class Game:
    id: str
    title: str
    system: str
    core: Path
    content: Path

    def public(self) -> dict:
        return {"id": self.id, "title": self.title, "system": self.system}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "game"


def find_core(name: str, dirs: list[Path] | None = None) -> Path | None:
    for d in CORE_DIRS if dirs is None else dirs:
        p = d / name
        if p.is_file():
            return p
    return None


def scan_games(content_dir: Path | None = None, doom_dir: Path | None = None,
               core_dirs: list[Path] | None = None) -> list[Game]:
    """Игры, для которых есть и файл, и ядро: content_dir/**/*.<ext> и свободные WAD Freedoom."""
    content_dir = CONTENT_DIR if content_dir is None else content_dir
    doom_dir = DOOM_DIR if doom_dir is None else doom_dir
    files: list[Path] = []
    if content_dir.is_dir():
        files += sorted(p for p in content_dir.rglob("*") if p.is_file() and p.suffix.lower() in SYSTEMS)
    files += [doom_dir / f"{n}.wad" for n in ("freedoom1", "freedoom2") if (doom_dir / f"{n}.wad").is_file()]
    games: list[Game] = []
    seen: set[str] = set()
    for f in files:
        system, core_name = SYSTEMS[f.suffix.lower()]
        core = find_core(core_name, core_dirs)
        if core is None:
            continue
        title = KNOWN_TITLES.get(f.stem.lower()) or f.stem.replace("_", " ").strip()
        gid = _slug(title)
        while gid in seen:
            gid += "-2"
        seen.add(gid)
        games.append(Game(gid, title, system, core, f))
    return games


class GamesError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class GamesService:
    def __init__(self, get_speed: Callable[[], float | None], pause_radio: Callable[[], Awaitable[bool]],
                 resume_radio: Callable[[], Awaitable[None]], notify: Callable[[str, str], None],
                 mock: bool = False) -> None:
        self.get_speed = get_speed
        self.pause_radio = pause_radio  # → True, если радио играло и его поставили на паузу
        self.resume_radio = resume_radio
        self.notify = notify  # (уровень, текст) — тост в админке
        self.mock = mock
        self.running = False
        self.current: Game | None = None
        self._radio_paused = False
        self._moving_since: float | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    def games(self) -> list[Game]:
        return scan_games()

    def state(self) -> dict:
        return {"running": self.running, "current": self.current.public() if self.current else None,
                "games": [g.public() for g in self.games()]}

    async def _systemctl(self, action: str) -> tuple[int, str]:
        if self.mock:
            return 0, ""
        rc, out, err = await run("sudo", "-n", "/usr/bin/systemctl", action, "fnav-games", timeout=20)
        return rc, (err or out).strip()

    async def _is_active(self) -> bool:
        if self.mock:
            return self.running
        _rc, out, _ = await run("systemctl", "is-active", UNIT, timeout=5)
        return out.strip() in ("active", "activating")

    async def launch(self, game_id: str) -> dict:
        async with self._lock:
            game = next((g for g in self.games() if g.id == game_id), None)
            if game is None:
                raise GamesError(404, "Game not found")
            speed = self.get_speed()
            if speed is not None and speed > MAX_START_SPEED_MPS:
                raise GamesError(409, "Stop the vehicle to play")
            if self.running:
                raise GamesError(409, "A game is already running")
            ARGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = ARGS_FILE.with_suffix(".tmp")
            tmp.write_text(f"{game.core}\n{game.content}\n", encoding="utf-8")
            os.replace(tmp, ARGS_FILE)
            self._radio_paused = await self.pause_radio()
            rc, msg = await self._systemctl("start")
            if rc != 0:
                if self._radio_paused:
                    await self.resume_radio()
                    self._radio_paused = False
                raise GamesError(500, f"Cannot start the game: {msg[:200] or rc}")
            self.running, self.current, self._moving_since = True, game, None
            log.info("game started: %s (%s)", game.title, game.system)
            return self.state()

    async def stop(self, reason: str = "") -> dict:
        async with self._lock:
            if self.running or await self._is_active():
                await self._systemctl("stop")
                if reason:
                    log.info("game stopped: %s", reason)
            await self._finished()
            return self.state()

    async def _finished(self) -> None:
        if not self.running:
            return
        self.running, self.current = False, None
        if self._radio_paused:
            self._radio_paused = False
            try:
                await self.resume_radio()
            except Exception:
                log.exception("radio resume after game")

    # ── сторож: игра закрыта из меню RetroArch, машина поехала ──
    def start(self) -> None:
        self._task = asyncio.create_task(self._watch(), name="games-watch")

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    async def _watch(self) -> None:
        last_check = 0.0
        while True:
            await asyncio.sleep(1.0)
            if not self.running:
                continue
            try:
                speed = self.get_speed() or 0.0
                now = time.monotonic()
                if speed > STOP_SPEED_MPS:
                    self._moving_since = self._moving_since or now
                    if now - self._moving_since >= STOP_AFTER_S:
                        self.notify("warn", "The vehicle is moving: the game was closed")
                        await self.stop("vehicle is moving")
                        continue
                else:
                    self._moving_since = None
                if now - last_check >= 2.0:
                    last_check = now
                    if not await self._is_active():  # вышли из игры (Select+Start / меню RetroArch)
                        log.info("game exited")
                        async with self._lock:
                            await self._finished()
            except Exception:
                log.exception("games watch")
