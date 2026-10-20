"""Тест «обрыв питания»: процесс пишет в реальном времени, в случайный момент его убиваем (kill),
затем имитируем потерю не сброшенного fsync-ом хвоста (обрезка на случайном байте после последнего
fsync, иногда с мусором) → восстановление при старте → файл целиком декодируется PyAV,
потеряно не больше fsync_interval_s + 1 с.
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from camhelpers import FPS, REPO, decode_file

from fnav.camera.storage import list_recordings, repair_latest
from fnav.camera.tsmux import TS, is_clean

FSYNC_S = 1.0
CHILD = Path(__file__).resolve().parent / "crash_child.py"


def run_and_kill(root: Path, seed: int) -> tuple[int, int, int, Path | None]:
    """Возвращает (кадров отдано к моменту kill, байт после последнего fsync, кадров в нём, путь)."""
    env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONUNBUFFERED="1")
    proc = subprocess.Popen([sys.executable, str(CHILD), str(root), str(FSYNC_S)], cwd=str(REPO), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    state = {"F": 0, "S": (0, 0, None), "started": threading.Event()}

    def reader():
        assert proc.stdout is not None
        for line in proc.stdout:
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "F":
                state["F"] = int(parts[1])
                state["started"].set()
            elif parts[0] == "S":
                state["S"] = (int(parts[1]), int(parts[2]), Path(" ".join(parts[3:])))

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        assert state["started"].wait(30), "child did not start"
        time.sleep(random.Random(seed).uniform(2.2, 4.5))
        emitted = state["F"]
        proc.kill()
        proc.wait(10)
    finally:
        if proc.poll() is None:
            proc.kill()
    t.join(5)
    synced, synced_frames, path = state["S"]
    return emitted, synced, synced_frames, path


@pytest.mark.parametrize("seed,mode", [(1, "kill"), (2, "powerloss"), (3, "powerloss_garbage")])
def test_power_loss(tmp_path: Path, seed: int, mode: str):
    emitted, synced, synced_frames, path = run_and_kill(tmp_path, seed)
    files = list_recordings(tmp_path)
    assert len(files) == 1
    f = files[0]
    assert path is None or path.resolve() == f.resolve()
    assert synced > 0, "no fsync happened"
    size = f.stat().st_size
    rnd = random.Random(seed)
    if mode.startswith("powerloss"):
        # на диске гарантированно только то, что было до последнего fsync; дальше — что угодно
        cut = rnd.randrange(synced, size + 1)
        with open(f, "r+b") as fh:
            fh.truncate(cut)
            if mode.endswith("garbage"):
                fh.seek(cut)
                fh.write(bytes(rnd.randrange(100, 20000)))  # нули вместо незаписанных блоков
    # «перезагрузка»: восстановление при старте
    repair_latest(tmp_path)
    assert f.stat().st_size % TS == 0 and is_clean(f)
    n, errors, frames = decode_file(f)
    assert errors == 0
    assert n >= synced_frames - 1  # всё, что было сброшено fsync, читается (−1 кадр на неполный)
    lost_s = (emitted - n) / FPS
    print(f"{mode}: emitted={emitted} decoded={n} synced_frames={synced_frames} lost={lost_s:.2f}s")
    assert lost_s <= FSYNC_S + 1.0
    assert frames[0].key_frame
