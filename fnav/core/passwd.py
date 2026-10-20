"""Задать пароль веб-интерфейса.

    echo -n 'пароль' | python -m fnav.core.passwd          # из stdin (install.sh)
    python -m fnav.core.passwd                             # интерактивно (getpass, дважды)
    python -m fnav.core.passwd --secrets /etc/fnav/secrets.env

Пишет/обновляет FNAV_PASSWORD_HASH и, если его нет, FNAV_SESSION_SECRET.
Путь по умолчанию — fnav.paths.secrets_file() (FNAV_SECRETS или {FNAV_DATA_DIR}/secrets.env).
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from fnav import paths
from fnav.core import secretsfile as sf


def _read_password(stdin_flag: bool) -> str:
    if stdin_flag or not sys.stdin.isatty():
        data = sys.stdin.buffer.read().decode("utf-8")
        # убираем только перевод строки в конце (echo без -n), пробелы в пароле сохраняем
        return data.removesuffix("\n")
    p1 = getpass.getpass("New password: ")
    p2 = getpass.getpass("Repeat: ")
    if p1 != p2:
        raise SystemExit("Passwords do not match")
    return p1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m fnav.core.passwd", description="Set the fnav web UI password")
    ap.add_argument("--secrets", type=Path, default=None, help="path to secrets.env")
    ap.add_argument("--stdin", action="store_true", help="read the password from stdin even in a terminal")
    args = ap.parse_args(argv)

    path: Path = args.secrets or paths.secrets_file()
    pw = _read_password(args.stdin).rstrip("\r")
    if len(pw) < sf.MIN_PASSWORD_LEN:
        print(f"Error: password is shorter than {sf.MIN_PASSWORD_LEN} characters", file=sys.stderr)
        return 2
    cur = sf.read_secrets(path)
    upd = {sf.KEY_HASH: sf.hash_password(pw)}
    if not cur.get(sf.KEY_SECRET):
        upd[sf.KEY_SECRET] = sf.new_session_secret()
    sf.update_secrets(upd, path)
    print(f"Password saved to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
