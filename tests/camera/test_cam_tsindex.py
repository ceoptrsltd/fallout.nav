"""Индекс ключевых кадров (.ts.idx регистратора и поиск по файлу) и куски для HLS."""

from __future__ import annotations

import io
import itertools
from datetime import datetime
from pathlib import Path

import pytest
from camhelpers import FPS, synth_au

from fnav.camera import tsindex as ti
from fnav.camera.recorder import Recorder, TimeSync
from fnav.camera.storage import cleanup, list_recordings
from fnav.camera.tsmux import PID_PAT, PTS_START, TS, TsMuxer, repair_ts

TICKS = 90000 // FPS  # кадр, 90 кГц


def big_au(key: bool, i: int, size: int) -> bytes:
    """Синтетический AU нужного размера (для выборочного поиска нужны мегабайты)."""
    au = synth_au(key, i)
    return au + bytes((i + k) & 0xFF for k in range(256)) * (size // 256)


def write_ts(path: Path, n: int, *, gop: int = 15, key_bytes: int = 600, p_bytes: int = 300,
             sidecar: bool = False, end_marker: bool = True) -> list[tuple[int, int]]:
    """Файл как у регистратора (PAT/PMT в начале и перед каждым ключевым кадром).
    Возвращает [(смещение PAT перед ключевым кадром, pts)]."""
    mux = TsMuxer()
    side = ti.SidecarWriter(path) if sidecar else None
    keys = []
    with open(path, "wb") as f:
        f.write(mux.tables())
        for i in range(n):
            key = i % gop == 0
            pts = PTS_START + i * TICKS
            if key:
                keys.append((f.tell(), pts))
                if side:
                    side.add(f.tell(), pts)
            f.write(mux.mux(big_au(key, i, key_bytes if key else p_bytes), pts, keyframe=key))
        if end_marker:
            f.write(mux.end_marker())
    if side:
        side.close()
    return keys


def frame_end_pts(n: int) -> int:
    return PTS_START + n * TICKS


def check_chunks(path: Path, parts, total_pts: int) -> None:
    """Куски подряд, без дыр; каждый (кроме первого) — с PAT; сумма длительностей = длительность файла."""
    data = path.read_bytes()
    assert parts[0][0] == 0
    for (o1, n1, _), (o2, _, _) in itertools.pairwise(parts):
        assert o1 + n1 == o2
    for off, length, dur in parts:
        assert off % TS == 0 and length % TS == 0 and dur > 0
        assert data[off] == 0x47 and ((data[off + 1] & 0x1F) << 8 | data[off + 2]) == PID_PAT
    assert sum(d for *_, d in parts) == pytest.approx(total_pts / 90000, abs=1e-6)


def test_dense_scan_finds_every_keyframe(tmp_path: Path):
    p = tmp_path / "a.ts"
    keys = write_ts(p, 15 * 20)
    idx = ti.update_index(p)
    assert idx.from_sidecar == 0
    assert idx.keys == [(0, keys[0][1])] + keys[1:]  # первый кусок — вместе с PAT/PMT в начале файла
    assert idx.layout == ti.Layout(0, 0x100, frozenset({0, 0x11, 0x1000}))
    assert idx.end_pts == frame_end_pts(15 * 20)
    parts = ti.chunks(idx, live=False)
    assert [d for *_, d in parts] == [3.0] * 6 + [2.0]
    check_chunks(p, parts, frame_end_pts(300) - PTS_START)
    assert parts[-1][0] + parts[-1][1] == p.stat().st_size


def test_live_chunks_are_complete_and_stable_while_file_grows(tmp_path: Path):
    p = tmp_path / "a.ts"
    write_ts(p, 15 * 20, end_marker=False)
    full = p.read_bytes()
    keys = ti.update_index(p).keys
    cut = keys[9][0] + 5000  # середина 10-й секунды
    p.write_bytes(full[:cut])
    idx1 = ti.update_index(p)
    assert idx1.keys == keys[:10]
    live1 = ti.chunks(idx1, live=True)
    # законченные куски 0-3, 3-6, 6-9 с; хвост от ключевого кадра 9 с ещё пишется — не выдаётся
    assert [(o, d) for o, _, d in live1] == [(0, 3.0), (keys[3][0], 3.0), (keys[6][0], 3.0)]
    assert sum(n for _, n, _ in live1) == keys[9][0]
    with open(p, "ab") as f:
        f.write(full[cut:])
    idx2 = ti.update_index(p, idx1)
    assert idx1.keys == keys[:10]  # прошлый индекс не изменён
    assert idx2.keys == ti.update_index(p).keys == keys  # дочитанный = построенный заново
    live2 = ti.chunks(idx2, live=True)
    assert live2[:len(live1)] == live1  # старые куски не меняются
    assert ti.update_index(p, idx2) is idx2  # ничего не изменилось — тот же объект


def test_sparse_scan_without_sidecar(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ti, "DENSE_TAIL", 200_000)
    monkeypatch.setattr(ti, "_BLOCK", TS * 1000)
    p = tmp_path / "a.ts"
    n = 15 * 120
    keys = write_ts(p, n, key_bytes=20_000, p_bytes=5_000)  # ≈ 90 КБ на GOP, 10 МБ
    reads = []

    class Counting(io.FileIO):
        def read(self, k=-1):
            b = super().read(k)
            reads.append(len(b))
            return b

    with monkeypatch.context() as m:
        m.setattr(ti, "open", lambda path, mode="r", *a, **kw: Counting(path, "r"), raising=False)
        idx = ti.update_index(p)
    assert sum(reads) < p.stat().st_size * 0.5  # прочитана меньшая часть файла
    dense = dict([(0, keys[0][1])] + keys[1:])
    assert all(dense.get(o) == pts for o, pts in idx.keys)  # найденное — настоящие ключевые кадры
    assert len(idx.keys) < len(keys)
    parts = ti.chunks(idx, live=False)
    check_chunks(p, parts, frame_end_pts(n) - PTS_START)
    assert all(2.0 <= d <= 4.0 for *_, d in parts[:-1])


def test_sidecar_written_by_recorder_and_used(tmp_path: Path, frames_10s):
    rec = Recorder(tmp_path, segment_s=600, fsync_interval_s=0.2, gop=15, fps=FPS,
                   time_sync=TimeSync(lambda: True), clock=lambda: datetime(2026, 10, 20, 12, 0, 0))
    rec.start()
    for au, key, ts in frames_10s:
        rec.push(au, key, ts)
    assert rec.drain()
    rec.stop()
    (p,) = list_recordings(tmp_path)
    side = ti.read_sidecar(p)
    assert len(side) == 10 and side[0] == (2 * TS, PTS_START)  # после PAT/PMT начала файла
    idx = ti.update_index(p)
    assert idx.from_sidecar == 9  # последний перепроверяется поиском
    assert idx.keys == [(0, PTS_START)] + side[1:]  # первый кусок — с начала файла
    ti.sidecar_path(p).unlink()
    plain = ti.update_index(p)
    assert plain.from_sidecar == 0 and plain.keys == idx.keys
    assert ti.chunks(idx, live=False) == ti.chunks(plain, live=False)


def test_each_chunk_decodes_from_a_keyframe(tmp_path: Path, frames_10s):
    av = pytest.importorskip("av")
    rec = Recorder(tmp_path, segment_s=600, fsync_interval_s=0.2, gop=15, fps=FPS,
                   time_sync=TimeSync(lambda: True), clock=lambda: datetime(2026, 10, 20, 12, 0, 0))
    rec.start()
    for au, key, ts in frames_10s:
        rec.push(au, key, ts)
    assert rec.drain()
    rec.stop()
    (p,) = list_recordings(tmp_path)
    data = p.read_bytes()
    idx = ti.update_index(p)
    parts = ti.chunks(idx, live=False)
    assert len(parts) == 4
    t = 0.0
    for off, length, dur in parts:
        with av.open(io.BytesIO(data[off:off + length]), format="mpegts") as c:  # кусок сам по себе (PAT/PMT)
            frames = [fr for pkt in c.demux(c.streams.video[0]) for fr in pkt.decode()]
        assert frames and frames[0].key_frame
        assert frames[0].pts == PTS_START + round(t * 90000)
        assert len(frames) == round(dur * FPS)
        t += dur
    assert t == pytest.approx(10.0)


def test_sidecar_beyond_end_and_garbage_are_ignored(tmp_path: Path):
    p = tmp_path / "a.ts"
    keys = write_ts(p, 15 * 10, sidecar=True, end_marker=False)
    good = ti.update_index(p).keys
    assert good == [(0, keys[0][1])] + keys[1:]
    # обрыв питания: файл обрезан, строки индекса после конца файла — лишние
    data = p.read_bytes()
    p.write_bytes(data[:keys[6][0] + 3000])
    before, after = repair_ts(p)
    assert after < before
    idx = ti.update_index(p)
    assert idx.from_sidecar == 6 and idx.keys == good[:7]  # ключевой кадр 6 с уцелел, его строка перепроверена
    assert all(o < p.stat().st_size for o, _ in idx.keys)
    # мусор в боковом файле → поиск по файлу
    ti.sidecar_path(p).write_bytes(ti.SIDECAR_MAGIC + b"7 100\n13 200\n")
    assert ti.update_index(p).keys == idx.keys
    ti.sidecar_path(p).write_bytes(b"something else\n")
    assert ti.update_index(p).keys == idx.keys
    # недописанная последняя строка
    ti.sidecar_path(p).write_bytes(ti.SIDECAR_MAGIC + b"%d %d\n%d 12" % (keys[0][0], keys[0][1], keys[1][0]))
    assert ti.read_sidecar(p) == [keys[0]]


def test_generic_ts_from_libav(tmp_path: Path):
    """Файл чужого муксера (PyAV/libavformat): ключевые кадры по random_access, куски с ключевых кадров."""
    av = pytest.importorskip("av")
    import numpy as np

    p = tmp_path / "x.ts"
    with av.open(str(p), "w", format="mpegts") as out:
        st = out.add_stream("libx264", rate=FPS)
        st.width, st.height, st.pix_fmt = 64, 48, "yuv420p"
        st.options = {"g": "15", "bf": "0", "preset": "ultrafast", "x264-params": "scenecut=0"}
        rng = np.random.default_rng(2)
        for i in range(FPS * 7):
            fr = av.VideoFrame.from_ndarray(rng.integers(0, 255, (48, 64, 3), dtype=np.uint8), format="rgb24")
            fr.pts = i
            for pkt in st.encode(fr):
                out.mux(pkt)
        for pkt in st.encode():
            out.mux(pkt)
    idx = ti.update_index(p)
    assert len(idx.keys) == 7
    data = p.read_bytes()
    parts = ti.chunks(idx, live=False)
    assert sum(d for *_, d in parts) == pytest.approx(7.0, abs=0.01)
    for off, length, _ in parts:
        with av.open(io.BytesIO(data[off:off + length]), format="mpegts") as c:
            fr = next(fr for pkt in c.demux(c.streams.video[0]) for fr in pkt.decode())
        assert fr.key_frame


def test_cleanup_removes_sidecar(tmp_path: Path):
    old = tmp_path / "2026-10-19" / "2026-10-19_10-00-00.ts"
    old.parent.mkdir()
    write_ts(old, 30, sidecar=True)
    assert ti.sidecar_path(old).exists()
    res = cleanup(tmp_path, target_free_bytes=10, free_fn=lambda _p: 0 if old.exists() else 100)
    assert res.deleted == [old]
    assert not ti.sidecar_path(old).exists() and not old.parent.exists()


def test_sidecar_keeps_the_true_start(tmp_path: Path, frames_10s):
    """Истинное начало (unix) в заголовке .ts.idx: имя — по местному времени, а пояс меняется в дороге."""
    wall = datetime(2026, 11, 2, 13, 40, 10)
    for sub, synced in (("dated", True), ("nosync", False)):
        rec = Recorder(tmp_path / sub, segment_s=600, fsync_interval_s=0.2, gop=15, fps=FPS,
                       time_sync=TimeSync(lambda s=synced: s), clock=lambda: wall)
        rec.start()
        for au, key, ts in frames_10s:
            rec.push(au, key, ts)
        assert rec.drain()
        rec.stop()
        (p,) = list_recordings(tmp_path / sub)
        assert len(ti.read_sidecar(p)) == 10  # строки индекса — после строки start
        assert ti.update_index(p).from_sidecar == 9
        if synced:
            assert p.name == "2026-11-02_13-40-10.ts"
            assert ti.read_start(p) == pytest.approx(wall.timestamp(), abs=1e-3)
        else:  # время не синхронизировано — начало не известно
            assert p.parent.name == "nosync" and ti.read_start(p) is None


def test_sidecar_header_versions(tmp_path: Path):
    p = tmp_path / "a.ts"
    keys = write_ts(p, 15 * 4)
    lines = b"".join(b"%d %d\n" % k for k in keys)
    for head, start in ((b"fnav-tsindex 1\n", None),  # записи до смены формата
                        (b"fnav-tsindex 2\n", None),
                        (b"fnav-tsindex 2\nstart 1793627210.250\n", 1793627210.25),
                        (b"fnav-tsindex 2\nstart nan\n", None),
                        (b"fnav-tsindex 2\nstart -5\n", None)):
        ti.sidecar_path(p).write_bytes(head + lines)
        assert ti.read_start(p) == start, head
        assert ti.read_sidecar(p) == keys, head
    ti.sidecar_path(p).write_bytes(b"fnav-tsindex 2\nstart 17936")  # недописанная строка
    assert ti.read_start(p) is None and ti.read_sidecar(p) == []
    ti.sidecar_path(p).write_bytes(b"fnav-tsindex 9\nstart 1793627210\n" + lines)  # чужая версия
    assert ti.read_start(p) is None and ti.read_sidecar(p) == []
    assert ti.read_start(tmp_path / "none.ts") is None
    w = ti.SidecarWriter(p, 1793627210.25)
    w.add(*keys[0])
    w.close()
    assert ti.sidecar_path(p).read_bytes() == b"fnav-tsindex 2\nstart 1793627210.250\n%d %d\n" % keys[0]
