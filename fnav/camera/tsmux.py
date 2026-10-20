"""Собственный минимальный MPEG-TS муксер (одна программа, одна дорожка H.264) и восстановление файла.

Почему свой, а не libavformat:
  * каждый access unit превращается в целое число 188-байтных пакетов и отдаётся одним ``write()``
    прямо в ядро — в процессе нет скрытых буферов (AVIOContext, interleave-очередь), поэтому после
    ``fsync()`` на диске гарантированно всё, что было записано до него;
  * файл всегда выровнен по 188 байт, обрыв портит максимум хвост — его отрезает ``repair_ts``;
  * PAT/PMT повторяются перед каждым ключевым кадром — любой кусок файла самодостаточен;
  * путь записи не зависит от версии PyAV/libav на устройстве (их проверяем только в тестах).
"""

from __future__ import annotations

import os
from pathlib import Path

TS = 188
SYNC = 0x47
PID_PAT = 0x0000
PID_PMT = 0x1000
PID_VIDEO = 0x0100
PID_NULL = 0x1FFF
STREAM_TYPE_H264 = 0x1B
PTS_MASK = (1 << 33) - 1
# Метка «файл закрыт корректно»: null-пакет (плееры его игнорируют) с этой подписью.
END_MARKER = b"FNAV-END-OF-SEGMENT"

# PTS первого кадра и запас PCR относительно DTS (как у ffmpeg: muxdelay 0.7 с).
PTS_START = 126000  # 1.4 с
PCR_DELAY = 63000  # 0.7 с


def _crc32_table() -> list[int]:
    tbl = []
    for i in range(256):
        c = i << 24
        for _ in range(8):
            c = ((c << 1) ^ 0x04C11DB7) if c & 0x80000000 else (c << 1)
        tbl.append(c & 0xFFFFFFFF)
    return tbl


_CRC_TBL = _crc32_table()


def crc32_mpeg(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TBL[((crc >> 24) ^ b) & 0xFF]
    return crc


def _section(body: bytes) -> bytes:
    return body + crc32_mpeg(body).to_bytes(4, "big")


def _pat() -> bytes:
    # table_id, syntax+length, tsid, version/current, section, last, program 1 -> PMT PID
    body = bytes([0x00, 0xB0, 13, 0x00, 0x01, 0xC1, 0x00, 0x00, 0x00, 0x01,
                  0xE0 | (PID_PMT >> 8), PID_PMT & 0xFF])
    return _section(body)


def _pmt() -> bytes:
    body = bytes([0x02, 0xB0, 18, 0x00, 0x01, 0xC1, 0x00, 0x00,
                  0xE0 | (PID_VIDEO >> 8), PID_VIDEO & 0xFF,  # PCR PID
                  0xF0, 0x00,  # program_info_length = 0
                  STREAM_TYPE_H264, 0xE0 | (PID_VIDEO >> 8), PID_VIDEO & 0xFF, 0xF0, 0x00])
    return _section(body)


_PAT = _pat()
_PMT = _pmt()


def _ts_bytes(prefix: int, v: int) -> bytes:
    v &= PTS_MASK
    return bytes([
        (prefix << 4) | (((v >> 30) & 0x07) << 1) | 1,
        (v >> 22) & 0xFF,
        (((v >> 15) & 0x7F) << 1) | 1,
        (v >> 7) & 0xFF,
        ((v & 0x7F) << 1) | 1,
    ])


def _pcr_bytes(pcr_base: int) -> bytes:
    b = pcr_base & PTS_MASK
    return bytes([(b >> 25) & 0xFF, (b >> 17) & 0xFF, (b >> 9) & 0xFF, (b >> 1) & 0xFF,
                  ((b & 1) << 7) | 0x7E, 0x00])


class TsMuxer:
    """Muxes H.264 access units (Annex-B, с AUD) в MPEG-TS. Каждый вызов возвращает кратно 188 байт."""

    def __init__(self) -> None:
        self._cc = {PID_PAT: 0, PID_PMT: 0, PID_VIDEO: 0, PID_NULL: 0}
        # заранее готовые заголовки «продолжение PES, только payload» для 16 значений CC
        self._cont_hdr = [bytes([SYNC, (PID_VIDEO >> 8) & 0x1F, PID_VIDEO & 0xFF, 0x10 | cc])
                          for cc in range(16)]

    def _next_cc(self, pid: int) -> int:
        cc = self._cc[pid]
        self._cc[pid] = (cc + 1) & 0x0F
        return cc

    def _psi_packet(self, pid: int, section: bytes) -> bytes:
        hdr = bytes([SYNC, 0x40 | ((pid >> 8) & 0x1F), pid & 0xFF, 0x10 | self._next_cc(pid)])
        pkt = hdr + b"\x00" + section
        return pkt + b"\xff" * (TS - len(pkt))

    def tables(self) -> bytes:
        """PAT + PMT."""
        return self._psi_packet(PID_PAT, _PAT) + self._psi_packet(PID_PMT, _PMT)

    def end_marker(self) -> bytes:
        hdr = bytes([SYNC, (PID_NULL >> 8) & 0x1F, PID_NULL & 0xFF, 0x10 | self._next_cc(PID_NULL)])
        pkt = hdr + END_MARKER
        return pkt + b"\xff" * (TS - len(pkt))

    def mux(self, au: bytes, pts: int, dts: int | None = None, keyframe: bool = False,
            with_tables: bool | None = None) -> bytes:
        """pts/dts — в единицах 90 кГц. Перед ключевым кадром по умолчанию пишутся PAT/PMT."""
        if dts is None:
            dts = pts
        if with_tables is None:
            with_tables = keyframe
        if dts != pts:
            opt = bytes([0x84, 0xC0, 10]) + _ts_bytes(0x3, pts) + _ts_bytes(0x1, dts)
        else:
            opt = bytes([0x84, 0x80, 5]) + _ts_bytes(0x2, pts)
        pes_len = len(opt) + len(au)
        if pes_len > 0xFFFF:
            pes_len = 0  # для видео допустимо «неограниченно»
        pes = b"\x00\x00\x01\xe0" + pes_len.to_bytes(2, "big") + opt + au

        out = bytearray(self.tables() if with_tables else b"")
        # первый пакет: адаптационное поле с PCR (+ random_access на ключевом кадре)
        af_flags = 0x10 | (0x40 if keyframe else 0x00)
        af_body = bytes([af_flags]) + _pcr_bytes(max(0, dts - PCR_DELAY))
        n = len(pes)
        cap = TS - 4 - 1 - len(af_body)
        first = pes[:cap]
        if len(first) < cap:
            af_body += b"\xff" * (cap - len(first))
        out += bytes([SYNC, 0x40 | ((PID_VIDEO >> 8) & 0x1F), PID_VIDEO & 0xFF,
                      0x30 | self._next_cc(PID_VIDEO), len(af_body)]) + af_body + first
        pos = len(first)
        hdrs = self._cont_hdr
        cc = self._cc[PID_VIDEO]
        while n - pos >= 184:
            out += hdrs[cc]
            out += pes[pos:pos + 184]
            cc = (cc + 1) & 0x0F
            pos += 184
        if pos < n:  # хвост PES: добиваем адаптационным полем со stuffing
            r = n - pos
            af_len = 183 - r
            af = bytes([af_len]) + ((b"\x00" + b"\xff" * (af_len - 1)) if af_len > 0 else b"")
            out += bytes([SYNC, (PID_VIDEO >> 8) & 0x1F, PID_VIDEO & 0xFF, 0x30 | cc]) + af + pes[pos:]
            cc = (cc + 1) & 0x0F
        self._cc[PID_VIDEO] = cc
        return bytes(out)


def pts_from_us(us: int) -> int:
    return (us * 9 + 50) // 100  # 90 кГц, с округлением


# ───────────────────────────── восстановление ─────────────────────────────


def _pid(pkt: bytes) -> int:
    return ((pkt[1] & 0x1F) << 8) | pkt[2]


def is_clean(path: Path) -> bool:
    """Файл закрыт корректно (последний пакет — метка конца)?"""
    size = path.stat().st_size
    if size < TS or size % TS:
        return False
    with open(path, "rb") as f:
        f.seek(size - TS)
        pkt = f.read(TS)
    return pkt[0] == SYNC and _pid(pkt) == PID_NULL and pkt[4:4 + len(END_MARKER)] == END_MARKER


def repair_ts(path: Path, drop_last_au: bool = True) -> tuple[int, int]:
    """Восстановить оборванный .ts. Возвращает (было байт, стало байт); 0 — файл пустой/без кадров.

    1. обрезать до кратного 188;
    2. отбросить хвостовые пакеты без синхробайта 0x47 (мусор/нули после сбоя);
    3. если файл не закрыт меткой конца — отбросить последний (возможно неполный) кадр:
       обрезать по началу последнего PES видео.
    """
    size = path.stat().st_size
    new = size - size % TS
    with open(path, "r+b") as f:
        while new >= TS:
            f.seek(new - TS)
            pkt = f.read(TS)
            if pkt[0] == SYNC:
                break
            new -= TS
        if new >= TS:
            f.seek(new - TS)
            last = f.read(TS)
            clean = _pid(last) == PID_NULL and last[4:4 + len(END_MARKER)] == END_MARKER
            if not clean and drop_last_au:
                # ищем назад начало последнего PES видео (блоками по 1 МБ)
                cut = None
                pos = new
                block = TS * 5000
                while pos > 0 and cut is None:
                    start = max(0, pos - block)
                    f.seek(start)
                    buf = f.read(pos - start)
                    for off in range(len(buf) - TS, -1, -TS):
                        if buf[off] == SYNC and (buf[off + 1] & 0x40) and _pid(buf[off:off + 3]) == PID_VIDEO:
                            cut = start + off
                            break
                    pos = start
                new = cut if cut is not None else 0
                # PAT/PMT, оставшиеся перед отрезанным ключевым кадром, безвредны
            if not clean and new > 0:
                # пометить файл закрытым, чтобы повторный запуск не отрезал ещё кадр
                f.seek(new)
                f.write(_null_end_packet())
                new += TS
        if new != size:
            f.truncate(new)
            f.flush()
            os.fsync(f.fileno())
    return size, new


def _null_end_packet() -> bytes:
    pkt = bytes([SYNC, (PID_NULL >> 8) & 0x1F, PID_NULL & 0xFF, 0x10]) + END_MARKER
    return pkt + b"\xff" * (TS - len(pkt))


def count_pes(data: bytes, pid: int = PID_VIDEO) -> int:
    """Число начал PES на PID (для тестов/selftest)."""
    n = 0
    for off in range(0, len(data) - TS + 1, TS):
        if data[off] == SYNC and (data[off + 1] & 0x40) and _pid(data[off:off + 3]) == pid:
            n += 1
    return n
