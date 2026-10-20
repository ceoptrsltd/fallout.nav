"""Файл секретов (`secrets.env`): хэш пароля и секрет подписи сессий.

Формат — строки `KEY=VALUE` (как EnvironmentFile systemd / shell). Значения пишем в одинарных
кавычках: в хэше есть `$`, без кавычек shell подставил бы переменные. Читаем оба варианта.

    FNAV_PASSWORD_HASH='scrypt$16384$8$1$<salt_b64>$<hash_b64>'
    FNAV_SESSION_SECRET='<token>'
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import tempfile
from pathlib import Path

from fnav import paths

KEY_HASH = "FNAV_PASSWORD_HASH"
KEY_SECRET = "FNAV_SESSION_SECRET"

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
MIN_PASSWORD_LEN = 6


# ───────────────────────── хэш пароля ─────────────────────────


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def hash_password(password: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=SCRYPT_DKLEN,
                        maxmem=256 * 1024 * 1024)
    return f"scrypt${n}${r}${p}${_b64e(salt)}${_b64e(dk)}"


def verify_password(password: str, stored: str | None) -> bool:
    """Проверка пароля против `scrypt$n$r$p$salt$hash`. Любая ошибка формата → False."""
    if not stored:
        return False
    try:
        algo, n_s, r_s, p_s, salt_s, hash_s = stored.split("$")
        if algo != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        if n < 2 or n > 2**20 or r < 1 or r > 32 or p < 1 or p > 16:
            return False
        salt, expected = _b64d(salt_s), _b64d(hash_s)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected),
                            maxmem=256 * 1024 * 1024)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


def new_session_secret() -> str:
    return secrets.token_urlsafe(32)


# ───────────────────────── файл ─────────────────────────


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def read_secrets(path: Path | None = None) -> dict[str, str]:
    """Прочитать `KEY=VALUE`. Нет файла → {}."""
    path = path or paths.secrets_file()
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, IsADirectoryError):
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        k, v = line.split("=", 1)
        out[k.strip()] = _unquote(v)
    return out


def _format_value(v: str) -> str:
    if "'" in v or "\n" in v:
        raise ValueError("invalid character in secret value")
    return f"'{v}'"


def update_secrets(updates: dict[str, str], path: Path | None = None) -> None:
    """Обновить/добавить ключи, сохранив остальные строки. Атомарно (tmp + replace), права 600.

    Если каталог недоступен на запись (например /etc/fnav принадлежит root, а файл — fnav),
    пишем файл на месте."""
    path = path or paths.secrets_file()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = ["# fallout.nav secrets - do not commit to git"]
    done: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        s = line.strip()
        key = s[7:].lstrip() if s.startswith("export ") else s
        key = key.split("=", 1)[0].strip() if "=" in key and not s.startswith("#") else None
        if key and key in updates:
            if key in done:
                continue  # дубликаты выкидываем
            new_lines.append(f"{key}={_format_value(updates[key])}")
            done.add(key)
        else:
            new_lines.append(line)
    for k, v in updates.items():
        if k not in done:
            new_lines.append(f"{k}={_format_value(v)}")
    data = "\n".join(new_lines) + "\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp = tempfile.mkstemp(prefix=".secrets.", dir=str(path.parent))
    except PermissionError:
        _write_in_place(path, data)
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        _replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_in_place(path: Path, data: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _replace(src: str, dst: Path) -> None:
    import time

    for attempt in range(10):
        try:
            os.replace(src, dst)
            return
        except PermissionError:  # Windows: файл открыт другим процессом
            if attempt == 9:
                raise
            time.sleep(0.05)


def ensure_secrets(*, default_password: str | None = None, path: Path | None = None) -> dict[str, str]:
    """Гарантировать наличие секрета сессий (и пароля, если задан default_password и хэша нет)."""
    path = path or paths.secrets_file()
    cur = read_secrets(path)
    upd: dict[str, str] = {}
    if not cur.get(KEY_HASH) and default_password is not None:
        upd[KEY_HASH] = hash_password(default_password)
    if not cur.get(KEY_SECRET):
        upd[KEY_SECRET] = new_session_secret()
    if upd:
        update_secrets(upd, path)
        cur.update(upd)
    return cur
