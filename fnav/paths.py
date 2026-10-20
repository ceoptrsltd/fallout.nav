"""Пути к данным — КОНТРАКТ (владелец: ведущий).

На устройстве:  FNAV_DATA_DIR=/var/lib/fnav, FNAV_SECRETS=/etc/fnav/secrets.env
На ПК (dev):    по умолчанию ./.devdata в корне репозитория.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    return Path(os.environ.get("FNAV_DATA_DIR", REPO_ROOT / ".devdata"))


def settings_file() -> Path:
    """Настройки. Пишет только fnav-core (атомарно: tmp + rename). Остальные читают."""
    return data_dir() / "settings.json"


def recordings_dir() -> Path:
    """Видео: {recordings_dir}/YYYY-MM-DD/YYYY-MM-DD_HH-MM-SS.ts (+ .lock = защищено)."""
    return data_dir() / "recordings"


def tiles_dir() -> Path:
    """Кэш тайлов: {tiles_dir}/{layer}/{z}/{x}/{y}.jpg|.png"""
    return data_dir() / "tiles"


def secrets_file() -> Path:
    """FNAV_PASSWORD_HASH=scrypt$... и FNAV_SESSION_SECRET=...  (не в git)."""
    return Path(os.environ.get("FNAV_SECRETS", data_dir() / "secrets.env"))


def web_dir() -> Path:
    return REPO_ROOT / "web"
