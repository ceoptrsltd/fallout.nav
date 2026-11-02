"""Хранилище настроек: загрузка с проверкой, глубокое слияние, атомарная запись settings.json.

Последняя удачная копия: `settings.json.lkg` — пишется после каждой удачной загрузки (если отличается) и каждого
сохранения. Битый settings.json (не читается, не JSON, не проходит проверку) → настройки берутся из копии (битые
секции — из копии, а без неё — умолчания), исходник — в `settings.json.bad-<время>` (хранятся BAD_KEEP новейших),
и settings.json сразу перезаписывается восстановленным: следующий запуск снова читает целый файл, а не плодит .bad.
Исключение: файл не читается (права) и удачной копии нет — он не трогается до первого сохранения, как раньше.
Все файлы — через atomic_write_text (mkstemp: 0600, владелец — пользователь сервиса, на устройстве fnav:fnav).
"""

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
from fnav.models import SETTINGS_VERSION, Settings

log = logging.getLogger("fnav.core.config")

Listener = Callable[[Settings], Awaitable[None] | None]

LKG_SUFFIX = ".lkg"  # settings.json.lkg — последняя удачно прочитанная или сохранённая копия
BAD_KEEP = 3  # сколько копий битого файла (settings.json.bad-*) хранить


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


def migrate(data: dict) -> dict:
    """Сохранённые настройки старых версий → текущая (SETTINGS_VERSION). Только то, что умолчания не покрывают."""
    ver = data.get("version", 1)
    if not isinstance(ver, int) or ver >= SETTINGS_VERSION:
        return data
    data = dict(data)
    if ver < 2:  # клавиша видеовыхода (по умолчанию 4, у кого-то другая) — теперь камеры: передняя ↔ задняя
        kp = data.get("keypad")
        if isinstance(kp, dict):
            data["keypad"] = {k: "camera_switch" if v == "video_toggle" else v for k, v in kp.items()}
    data["version"] = SETTINGS_VERSION
    return data


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


def lkg_path(path: Path) -> Path:
    return path.with_name(path.name + LKG_SUFFIX)


def _read_json(path: Path) -> tuple[str, Any]:
    """→ ("missing", None) | ("error", текст) | ("corrupt", текст) | ("ok", dict)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "missing", None
    except OSError as e:  # права, ввод-вывод: содержимое не прочесть и не скопировать
        return "error", f"cannot read {path}: {e}"
    except UnicodeDecodeError as e:
        return "corrupt", f"{path.name} is corrupted ({e})"
    try:
        data = json.loads(raw)
    except ValueError as e:
        return "corrupt", f"{path.name} is corrupted ({e})"
    if not isinstance(data, dict):
        return "corrupt", f"{path.name} is corrupted (not an object)"
    return "ok", data


def _load_lkg(path: Path) -> Settings | None:
    state, data = _read_json(lkg_path(path))
    if state != "ok":
        return None
    try:
        return validate_settings(migrate(data))
    except SettingsError:
        return None


def load_settings(path: Path) -> tuple[Settings, list[str], str]:
    """Прочитать settings.json → (настройки, предупреждения, состояние).

    Состояние: "ok" — файл цел; "missing" — файла нет (умолчания); "recovered" — файл битый: настройки из последней
    удачной копии (.lkg), битые секции — из неё же или умолчаниями, исходник сохранён в .bad-<время> — файл можно
    перезаписать восстановленным; "degraded" — то же, но перезаписывать нельзя: файл не читается (права) или копию
    исходника сделать не удалось, а удачной копии нет — иначе настройки владельца пропали бы безвозвратно."""
    warnings: list[str] = []
    state, data = _read_json(path)
    if state == "missing":
        return Settings(), warnings, "missing"
    lkg = None
    if state == "ok":
        data = migrate(data)
        try:
            return validate_settings(data), warnings, "ok"
        except SettingsError as e:
            warnings.append(str(e))
        lkg = _load_lkg(path)
        # по секциям: целые — из файла, битые — из последней удачной копии, а без неё — умолчания
        cur = (lkg or Settings()).model_dump()
        for key, val in data.items():
            try:
                cur = validate_settings({**cur, key: val}).model_dump()
            except SettingsError:
                warnings.append(f"section '{key}' reset to {'the last good copy' if lkg else 'defaults'}")
        settings = Settings.model_validate(cur)
    else:
        warnings.append(data)
        lkg = _load_lkg(path)
        settings = lkg or Settings()
        warnings.append(f"settings restored from {lkg_path(path).name}" if lkg
                        else f"no good copy ({lkg_path(path).name}) - using defaults")
    kept = state != "error" and _backup_bad(path)  # нечитаемый (права, ввод-вывод) не скопировать
    return settings, warnings, "recovered" if lkg is not None or kept else "degraded"


def load_settings_file(path: Path) -> tuple[Settings, list[str]]:
    """Прочитать settings.json (см. load_settings). Битые секции — из последней удачной копии или умолчания."""
    settings, warnings, _state = load_settings(path)
    return settings, warnings


def _backup_bad(path: Path, keep: int = BAD_KEEP) -> bool:
    """Копия битого файла (0600: в настройках бывают ключи); такая же уже есть — не дублировать; хранить keep
    новейших. → есть ли копия этого содержимого."""
    try:
        data = path.read_bytes()
        olds = sorted(path.parent.glob(f"{path.name}.bad-*"), key=lambda q: (q.stat().st_mtime, q.name))
        if not any(q.read_bytes() == data for q in olds):
            bad = path.with_name(f"{path.name}.bad-{time.strftime('%Y%m%d-%H%M%S')}")
            fd = os.open(bad, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            olds = [q for q in olds if q != bad] + [bad]
        for q in olds[:-keep]:
            q.unlink(missing_ok=True)
        return True
    except OSError as e:
        log.warning("cannot keep a copy of the broken %s: %s", path.name, e)
        return False


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
        self.settings, warnings, self.load_state = load_settings(self.path)
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
        """Файла нет или он был битый — записать то, с чем работаем (камера читает то же самое); целый — обновить
        последнюю удачную копию (.lkg), если она отличается."""
        text = self._dump(self.settings)
        if self.load_state == "degraded":  # файл не тронут: владелец может починить права и перезапустить
            log.error("%s is broken and neither a copy of it nor a good copy exists: left as is, running on "
                      "defaults until settings are saved", self.path.name)
            return
        if self.load_state == "recovered" or not self.path.exists():
            try:
                atomic_write_text(self.path, text)
            except OSError as e:
                log.error("cannot write %s: %s", self.path, e)
                return
            if self.load_state == "recovered":
                log.warning("%s rewritten with the recovered settings", self.path.name)
            self.load_state = "ok"
        self._write_lkg(text, only_if_changed=True)

    def _write_lkg(self, text: str, only_if_changed: bool = False) -> None:
        lkg = lkg_path(self.path)
        try:
            if only_if_changed and lkg.read_text(encoding="utf-8") == text:
                return
        except (OSError, UnicodeDecodeError):
            pass
        try:
            atomic_write_text(lkg, text)
        except OSError as e:
            log.warning("cannot write %s: %s", lkg, e)

    @staticmethod
    def _dump(s: Settings) -> str:
        return json.dumps(s.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"

    async def _save(self, s: Settings) -> None:
        text = self._dump(s)
        await asyncio.to_thread(atomic_write_text, self.path, text)
        self.settings = s
        self.load_state = "ok"
        await asyncio.to_thread(self._write_lkg, text)  # последняя удачная копия — после каждого сохранения

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
