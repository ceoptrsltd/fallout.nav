"""MPEG-TS муксер, H.264-утилиты и восстановление оборванного файла."""

from __future__ import annotations

import random
from pathlib import Path

import pytest
from camhelpers import FPS, decode_file

from fnav.camera.h264 import AUD, NAL_AUD, NAL_SPS, AccessUnitFixer, nal_types
from fnav.camera.tsmux import (
    PID_PAT,
    PID_PMT,
    PTS_START,
    TS,
    TsMuxer,
    count_pes,
    crc32_mpeg,
    is_clean,
    pts_from_us,
    repair_ts,
)


def write_ts(path: Path, frames, clean: bool = True) -> list[int]:
    """Пишет файл; возвращает смещения конца каждого кадра."""
    mux = TsMuxer()
    fx = AccessUnitFixer()
    ends = []
    with open(path, "wb") as f:
        f.write(mux.tables())
        base = frames[0][2]
        for au, key, ts in frames:
            au, key = fx.fix(au, key)
            f.write(mux.mux(au, PTS_START + pts_from_us(ts - base), keyframe=key))
            ends.append(f.tell())
        if clean:
            f.write(mux.end_marker())
    return ends


def test_crc_known_value():
    # CRC-32/MPEG-2 от "123456789" = 0x0376E6E7
    assert crc32_mpeg(b"123456789") == 0x0376E6E7


def test_psi_tables_valid():
    data = TsMuxer().tables()
    assert len(data) == 2 * TS
    for i, pid in enumerate((PID_PAT, PID_PMT)):
        pkt = data[i * TS:(i + 1) * TS]
        assert pkt[0] == 0x47 and ((pkt[1] & 0x1F) << 8 | pkt[2]) == pid
        sec_len = ((pkt[6] & 0x0F) << 8) | pkt[7]
        section = pkt[5:5 + 3 + sec_len]
        assert crc32_mpeg(section) == 0  # CRC по секции вместе с CRC = 0


def test_fixer_adds_aud_and_params():
    from camhelpers import PPS, SPS, synth_au

    fx = AccessUnitFixer()
    key, _ = fx.fix(synth_au(True, 0), True)
    assert key.startswith(AUD) and nal_types(key)[:3] == [NAL_AUD, NAL_SPS, 8]
    # ключевой кадр без SPS/PPS — добавляются из кэша, AUD остаётся первым
    idr_only = b"\x00\x00\x00\x01\x65" + b"\x11" * 50
    fixed, k = fx.fix(AUD + idr_only, False)
    assert k is True
    assert fixed == AUD + SPS + PPS + idr_only
    p, k = fx.fix(b"\x00\x00\x01\x41\x22\x33", False)
    assert not k and p.startswith(AUD)


def test_mux_decodes_all_frames_with_correct_pts(tmp_path: Path, frames_10s):
    f = tmp_path / "a.ts"
    write_ts(f, frames_10s)
    data = f.read_bytes()
    assert len(data) % TS == 0
    assert all(data[i] == 0x47 for i in range(0, len(data), TS))
    assert count_pes(data) == len(frames_10s)
    assert is_clean(f)
    n, errors, frames = decode_file(f)
    assert errors == 0 and n == len(frames_10s)
    pts = [fr.pts for fr in frames]
    assert pts == [PTS_START + i * (90000 // FPS) for i in range(n)]
    assert frames[0].key_frame


@pytest.mark.parametrize("seed", range(8))
def test_repair_random_cut(tmp_path: Path, frames_10s, seed: int):
    f = tmp_path / "cut.ts"
    ends = write_ts(f, frames_10s, clean=False)
    size = f.stat().st_size
    rnd = random.Random(seed)
    cut = rnd.randrange(ends[3], size)
    with open(f, "r+b") as fh:
        fh.truncate(cut)
        if seed % 2:  # «мусор» после сбоя: нули/случайные байты в хвосте
            fh.seek(cut)
            fh.write(bytes(rnd.randrange(1, 5000)) if seed % 4 == 1 else rnd.randbytes(rnd.randrange(1, 5000)))
    _, after = repair_ts(f)
    assert after % TS == 0 and is_clean(f)
    complete = sum(1 for e in ends if e <= cut)  # кадров целиком до точки обрыва
    n, errors, _ = decode_file(f)
    assert errors == 0
    # теряем не больше одного кадра сверх оборванного
    assert complete - 1 <= n <= complete
    # повторное восстановление файл не меняет
    assert repair_ts(f) == (after, after)


def test_repair_keeps_clean_file(tmp_path: Path, frames_10s):
    f = tmp_path / "ok.ts"
    write_ts(f, frames_10s[:30])
    size = f.stat().st_size
    assert repair_ts(f) == (size, size)
    assert decode_file(f)[0] == 30


def test_repair_file_without_frames(tmp_path: Path):
    f = tmp_path / "empty.ts"
    f.write_bytes(TsMuxer().tables() + b"\x47\x00")
    assert repair_ts(f)[1] == 0
