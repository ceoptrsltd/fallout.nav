"""Включение/выключение физического видеовыхода (композит на RV-50).

Выключение через wlr-randr у экранной оболочки cage: CRTC гасится, видео-ЦАП обесточивается — сигнала нет,
и ничего не наводится на соседний вход монитора (родную камеру). При включении возвращаются режим,
поля (margins из cmdline) и зеркало. Состояние всегда берётся у самой оболочки (после перезапуска
fnav-display выход снова включён).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from fnav.core.system import run

log = logging.getLogger("fnav.core.videoout")

IS_LINUX = sys.platform.startswith("linux")
WAYLAND_DIR = os.environ.get("FNAV_WAYLAND_DIR", "/run/fnav-display")
WAYLAND_DISPLAY = os.environ.get("FNAV_WAYLAND_DISPLAY", "wayland-0")


class VideoOutputError(RuntimeError):
    pass


class VideoOutput:
    def __init__(self, mock: bool = False):
        self.mock = mock or not IS_LINUX
        self._mock_on = True
        self._lock = asyncio.Lock()

    async def _randr(self, *args: str) -> str:
        env_args = [f"XDG_RUNTIME_DIR={WAYLAND_DIR}", f"WAYLAND_DISPLAY={WAYLAND_DISPLAY}"]
        rc, out, err = await run("env", *env_args, "wlr-randr", *args, timeout=8)
        if rc != 0:
            raise VideoOutputError(f"wlr-randr: {(err or out).strip()[:200] or rc}")
        return out

    async def state(self) -> tuple[str, bool]:
        """(имя выхода, включён ли)."""
        if self.mock:
            return "Composite-1", self._mock_on
        out = await self._randr()
        name, enabled = None, None
        for line in out.splitlines():
            if line and not line[0].isspace():
                if name is not None:
                    break  # берём первый выход
                name = line.split()[0]
            elif name is not None and line.strip().startswith("Enabled:"):
                enabled = line.split(":", 1)[1].strip().lower() == "yes"
        if name is None or enabled is None:
            raise VideoOutputError("no display output found")
        return name, enabled

    async def set(self, on: bool | None = None) -> bool:
        """on=None — переключить. Возвращает новое состояние."""
        async with self._lock:
            name, enabled = await self.state()
            target = (not enabled) if on is None else on
            if target != enabled:
                if self.mock:
                    self._mock_on = target
                else:
                    await self._randr("--output", name, "--on" if target else "--off")
                log.info("video output %s: %s", name, "ON" if target else "OFF")
            return target
