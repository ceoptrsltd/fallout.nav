"""Регистратор: сегментация, начало с ключевого кадра, именование, nosync, fsync, защита."""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

from camhelpers import FPS, decode_file, synth_au

from fnav.camera.recorder import (
    Recorder,
    TimeSync,
    check_time_synced,
    dated_path,
    nosync_path,
)
from fnav.camera.storage import list_recordings
from fnav.camera.tsmux import TS, count_pes, is_clean


class FakeClock:
    """Ускоренное «стенное» время для имён файлов: +step на каждый вызов."""

    def __init__(self, start: datetime, step_s: float = 2.0) -> None:
        self.t = start
        self.step = timedelta(seconds=step_s)

    def __call__(self) -> datetime:
        t = self.t
        self.t += self.step
        return t


def make_rec(root: Path, **kw) -> Recorder:
    args = {"segment_s": 2.0, "fsync_interval_s": 0.2, "gop": 15, "fps": FPS,
            "time_sync": TimeSync(lambda: True), "clock": FakeClock(datetime(2026, 10, 20, 12, 0, 0))}
    args.update(kw)
    return Recorder(root, **args)


def test_segmentation_by_time_starts_with_keyframe(tmp_path: Path, frames_10s):
    rec = make_rec(tmp_path)
    rec.start()
    # первые кадры без ключевого (начинаем с середины GOP) — должны быть пропущены
    frames = frames_10s[5:110]  # ts 0.33 .. 7.3 с
    for au, key, ts in frames:
        rec.push(au, key, ts)
    assert rec.drain()
    rec.stop()
    files = list_recordings(tmp_path)
    assert [f.relative_to(tmp_path).as_posix() for f in files] == [
        "2026-10-20/2026-10-20_12-00-00.ts",
        "2026-10-20/2026-10-20_12-00-02.ts",
        "2026-10-20/2026-10-20_12-00-04.ts",
        "2026-10-20/2026-10-20_12-00-06.ts",
    ]
    total = 0
    for f in files:
        assert is_clean(f)
        n, errors, dec = decode_file(f)
        assert errors == 0 and n > 0
        assert dec[0].key_frame, f"{f.name} must start with a keyframe"
        assert dec[0].pts == 126000  # каждый сегмент начинается с PTS_START
        total += n
    # пишем всё, начиная с первого ключевого кадра (кадр 15)
    assert total == len(frames_10s[15:110])
    # сегменты режутся по ключевым кадрам: 15..44, 45..74, 75..104, 105..109
    assert [decode_file(f)[0] for f in files] == [30, 30, 30, 5]


def test_rotation_waits_for_keyframe_and_requests_one(tmp_path: Path):
    asked = threading.Event()
    rec = make_rec(tmp_path, segment_s=1.0, gop=5, request_keyframe=asked.set)
    rec.start()
    period = 1_000_000 // FPS
    # ключевой только в начале, дальше 40 P-кадров (> 2*gop после истечения сегмента)
    for i in range(40):
        rec.push(synth_au(i == 0, i), i == 0, i * period)
    assert rec.drain()
    assert asked.is_set()
    assert len(list_recordings(tmp_path)) == 1  # без ключевого новый сегмент не начат
    rec.push(synth_au(True, 40), True, 40 * period)
    assert rec.drain()
    rec.stop()
    files = list_recordings(tmp_path)
    assert len(files) == 2
    assert count_pes(files[1].read_bytes()) == 1


def test_naming_dated_and_collision(tmp_path: Path):
    t = datetime(2026, 10, 20, 9, 5, 7)
    p = dated_path(tmp_path, t)
    assert p.relative_to(tmp_path).as_posix() == "2026-10-20/2026-10-20_09-05-07.ts"
    p.parent.mkdir(parents=True)
    p.touch()
    assert dated_path(tmp_path, t).name == "2026-10-20_09-05-08.ts"


def test_nosync_naming_and_switch_after_sync(tmp_path: Path, monkeypatch):
    import fnav.camera.recorder as recmod

    monkeypatch.setattr(recmod, "_BOOT_ID", "abcdef0123456789")
    assert nosync_path(tmp_path, "abcdef0123456789").name == "boot-abcdef01_0000.ts"
    synced = {"v": False}
    ts = TimeSync(lambda: synced["v"])
    rec = make_rec(tmp_path, segment_s=1.0, time_sync=ts)
    rec.start()
    period = 1_000_000 // FPS
    i = 0

    def feed(n):
        nonlocal i
        for _ in range(n):
            rec.push(synth_au(i % 15 == 0, i), i % 15 == 0, i * period)
            i += 1
        assert rec.drain()

    feed(31)  # три сегмента (кадры 0-14, 15-29, 30) без синхронизации времени
    names = sorted(p.relative_to(tmp_path).as_posix() for p in list_recordings(tmp_path))
    assert names == [f"nosync/boot-abcdef01_{k:04d}.ts" for k in range(3)]
    assert rec.segment_time_synced is False
    synced["v"] = True
    ts.refresh()
    assert rec.segment_rel == "nosync/boot-abcdef01_0002.ts"  # текущий не переименовывается
    feed(15)
    rec.stop()
    names = sorted(p.relative_to(tmp_path).as_posix() for p in list_recordings(tmp_path))
    assert len(names) == 4 and re.fullmatch(r"\d{4}-\d\d-\d\d/\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d\.ts", names[0])


def test_time_synced_year_check():
    assert check_time_synced(datetime(2024, 12, 31)) is False


def test_fsync_makes_all_written_data_visible(tmp_path: Path, frames_10s):
    syncs: list[tuple[int, int]] = []
    ev = threading.Event()

    def on_sync(path, size, frames):
        syncs.append((size, frames))
        if frames >= 45:
            ev.set()

    rec = make_rec(tmp_path, segment_s=600, on_sync=on_sync)
    rec.start()
    for au, key, ts in frames_10s[:45]:
        rec.push(au, key, ts)
    assert rec.drain()
    assert ev.wait(3), syncs
    path = rec.current_path
    assert path is not None
    size, frames = syncs[-1]
    # всё, что было записано до fsync, уже в файле (нет буферов в процессе): читаем другим дескриптором
    with open(path, "rb") as f:
        data = f.read()
    assert len(data) == size and size % TS == 0
    assert count_pes(data) == frames == 45
    rec.stop()


def test_protect_current(tmp_path: Path, frames_10s):
    rec = make_rec(tmp_path, segment_s=600)
    assert rec.protect_current() is None
    rec.start()
    for au, key, ts in frames_10s[:20]:
        rec.push(au, key, ts)
    assert rec.drain()
    rel = rec.protect_current()
    assert rel == "2026-10-20/2026-10-20_12-00-00.ts"
    assert (tmp_path / (rel + ".lock")).exists()
    rec.stop()


def test_queue_overflow_resyncs_on_keyframe(tmp_path: Path, frames_10s):
    rec = make_rec(tmp_path, segment_s=600, queue_frames=10)
    # писатель не запущен — очередь переполнится
    for au, key, ts in frames_10s[:40]:
        rec.push(au, key, ts)
    assert rec.frames_dropped == 30
    rec.start()
    for au, key, ts in frames_10s[40:90]:
        rec.push(au, key, ts)
        assert rec.drain()  # дальше писатель успевает
    rec.stop()
    assert rec.frames_dropped == 30 + 5  # переполнение + P-кадры 40..44 до ключевого
    f = list_recordings(tmp_path)[0]
    n, errors, _ = decode_file(f)
    # 10 из очереди + с ключевого кадра 45 по 89
    assert errors == 0 and n == 10 + 45


def test_rates(tmp_path: Path, frames_10s):
    rec = make_rec(tmp_path)
    for au, key, ts in frames_10s[:46]:
        rec._account_rate(ts, len(au))
    fps, kbps = rec.rates()
    assert fps is not None and abs(fps - FPS) < 0.5 and kbps and kbps > 0
