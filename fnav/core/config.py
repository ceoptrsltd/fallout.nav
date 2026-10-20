"""Хранилище настроек: загрузка с проверкой, глубокое слияние, атомарная запись settings.json."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from fnav import paths
from fnav.models import Settings

log = logging.getLogger("fnav.core.config")

Listener = Callable[[Settings], Awaitable[None] | None]


class SettingsError(ValueError):
    """Ошибка валидации настроек; .detail — текст по-русски, .errors — список полей."""

    def __init__(self, detail: str, errors: list[dict] | None = None):
        super().__init__(detail)
        self.detail = detail
        self.errors = errors or []


def deep_merge(base: dict, patch: dict) -> dict:
    """Рекурсивное слияние словарей. Списки и скаляры заменяются целиком. `base` не меняется."""
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def format_validation_error(e: ValidationError) -> SettingsError:
    errs = []
    parts = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err.get("loc", ()))
        msg = err.get("msg", "")
        errs.append({"loc": loc, "msg": msg})
        parts.append(f"{loc}: {msg}" if loc else msg)
    return SettingsError("Invalid settings: " + "; ".join(parts), errs)


def validate_settings(data: Any) -> Settings:
    if not isinstance(data, dict):
        raise SettingsError("Invalid settings: a JSON object is expected")
    try:
        s = Settings.model_validate(data)
    except ValidationError as e:
        raise format_validation_error(e) from None
    if not s.nav.zoom_table:
        raise SettingsError("Invalid settings: nav.zoom_table must not be empty")
    return s


def load_settings_file(path: Path) -> tuple[Settings, list[str]]:
    """Прочитать settings.json. Битые секции заменяются умолчаниями (с предупреждением)."""
    warnings: list[str] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Settings(), warnings
    except OSError as e:
        warnings.append(f"cannot read {path}: {e}")
        return Settings(), warnings
    try:
        data = json.loads(raw)
    except ValueError as e:
        data = e
    if not isinstance(data, dict):
        warnings.append(f"settings.json is corrupted ({data if isinstance(data, Exception) else 'not an object'})"
                        " - using defaults")
        _backup_bad(path)
        return Settings(), warnings
    try:
        return validate_settings(data), warnings
    except SettingsError as e:
        warnings.append(str(e))
    # по секциям: берём то, что проходит проверку
    cur = Settings().model_dump()
    for key, val in data.items():
        try:
            cur = validate_settings({**cur, key: val}).model_dump()
        except SettingsError:
            warnings.append(f"section '{key}' reset to defaults")
    _backup_bad(path)
    return Settings.model_validate(cur), warnings


def _backup_bad(path: Path) -> None:
    try:
        bad = path.with_name(f"{path.name}.bad-{time.strftime('%Y%m%d-%H%M%S')}")
        bad.write_bytes(path.read_bytes())
    except OSError:
        pass


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:  # Windows: читатель держит файл открытым
                if attempt == 19:
                    raise
                time.sleep(0.05)
        if hasattr(os, "O_DIRECTORY"):
            try:
                dfd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class SettingsStore:
    def __init__(self, path: Path | None = None):
        self.path = path or paths.settings_file()
        self.settings, warnings = load_settings_file(self.path)
        for w in warnings:
            log.warning("%s", w)
        self.warnings = warnings
        self._lock = asyncio.Lock()
        self._listeners: list[Listener] = []

    def get(self) -> Settings:
        return self.settings

    def on_change(self, cb: Listener) -> None:
        self._listeners.append(cb)

    def ensure_file(self) -> None:
        """Записать файл, если его нет (чтобы камера читала то же самое)."""
        if not self.path.exists():
            try:
                atomic_write_text(self.path, self._dump(self.settings))
            except OSError as e:
                log.error("cannot write %s: %s", self.path, e)

    @staticmethod
    def _dump(s: Settings) -> str:
        return json.dumps(s.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"

    async def _save(self, s: Settings) -> None:
        await asyncio.to_thread(atomic_write_text, self.path, self._dump(s))
        self.settings = s

    async def replace(self, new: Settings | dict) -> Settings:
        s = new if isinstance(new, Settings) else validate_settings(new)
        async with self._lock:
            await self._save(s)
        await self._notify(s)
        return s

    async def patch(self, patch: Any) -> Settings:
        if not isinstance(patch, dict):
            raise SettingsError("Invalid settings: a JSON object is expected")
        async with self._lock:  # слияние под замком: параллельные PATCH не теряют друг друга
            merged = deep_merge(self.settings.model_dump(mode="json"), patch)
            s = validate_settings(merged)
            await self._save(s)
        await self._notify(s)
        return s

    async def reset(self) -> Settings:
        return await self.replace(Settings())

    async def _notify(self, s: Settings) -> None:
        for cb in list(self._listeners):
            try:
                r = cb(s)
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                log.exception("settings change listener failed")
