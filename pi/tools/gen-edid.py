#!/usr/bin/env python3
"""EDID generator for the fallout.nav HDMI output: 720x576 @ 50 Hz, progressive (CEA-861 VIC 17).

Why: the HDMI->AV (PAL) converter may give no EDID (or a "foreign" one), and the monitor may
not be connected at boot at all. The kernel parameters
    drm.edid_firmware=HDMI-A-1:edid/fnav-576p50.bin   video=HDMI-A-1:720x576@50D
make it use this EDID instead of the real one and treat the output as always connected, so the
mode is always the standard CEA one (27 MHz, 864x625) and not one computed by CVT.

Usage:
    gen-edid.py OUT.bin          # write 256 bytes (base block + CEA extension)
    gen-edid.py --check FILE     # verify checksums and decode the mode

Python 3 standard library only.
"""

from __future__ import annotations

import sys

# CEA-861 VIC 17 timings (720x576p @ 50 Hz, 4:3)
PIXEL_CLOCK_KHZ = 27000
H_ACTIVE, H_FRONT, H_SYNC, H_BACK = 720, 12, 64, 68
V_ACTIVE, V_FRONT, V_SYNC, V_BACK = 576, 5, 5, 39
H_BLANK = H_FRONT + H_SYNC + H_BACK  # 144
V_BLANK = V_FRONT + V_SYNC + V_BACK  # 49
H_SIZE_MM, V_SIZE_MM = 92, 69  # RV-50 visible area (informational)
VIC = 17


def _fix_checksum(block: bytearray) -> None:
    block[127] = (-sum(block[:127])) & 0xFF


def _text_descriptor(tag: int, text: str) -> bytes:
    data = (text[:12] + "\n").ljust(13).encode("ascii")  # text, LF, space padding
    return bytes([0, 0, 0, tag, 0]) + data


def _detailed_timing() -> bytes:
    pc = PIXEL_CLOCK_KHZ // 10  # units of 10 kHz
    d = bytearray(18)
    d[0], d[1] = pc & 0xFF, pc >> 8
    d[2] = H_ACTIVE & 0xFF
    d[3] = H_BLANK & 0xFF
    d[4] = ((H_ACTIVE >> 8) << 4) | (H_BLANK >> 8)
    d[5] = V_ACTIVE & 0xFF
    d[6] = V_BLANK & 0xFF
    d[7] = ((V_ACTIVE >> 8) << 4) | (V_BLANK >> 8)
    d[8] = H_FRONT & 0xFF
    d[9] = H_SYNC & 0xFF
    d[10] = ((V_FRONT & 0x0F) << 4) | (V_SYNC & 0x0F)
    d[11] = (
        ((H_FRONT >> 8) << 6) | ((H_SYNC >> 8) << 4) | ((V_FRONT >> 4) << 2) | (V_SYNC >> 4)
    )
    d[12], d[13] = H_SIZE_MM & 0xFF, V_SIZE_MM & 0xFF
    d[14] = ((H_SIZE_MM >> 8) << 4) | (V_SIZE_MM >> 8)
    d[15] = d[16] = 0  # borders
    d[17] = 0x18  # progressive, digital separate sync, HSync- VSync-
    return bytes(d)


def build_base() -> bytearray:
    b = bytearray(128)
    b[0:8] = bytes([0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00])
    mfg = ((ord("F") - 64) << 10) | ((ord("N") - 64) << 5) | (ord("V") - 64)
    b[8], b[9] = mfg >> 8, mfg & 0xFF
    b[10], b[11] = 0x01, 0x00  # product code
    b[12:16] = bytes([0x01, 0x00, 0x00, 0x00])  # serial number
    b[16], b[17] = 1, 2026 - 1990  # week, year
    b[18], b[19] = 1, 4  # EDID 1.4
    b[20] = 0xA2  # digital input, 8 bit, HDMI-a
    b[21], b[22] = 12, 9  # size, cm (4:3)
    b[23] = 0x78  # gamma 2.2
    b[24] = 0x06  # sRGB, preferred mode = first DTD
    b[25:35] = bytes([0xEE, 0x91, 0xA3, 0x54, 0x4C, 0x99, 0x26, 0x0F, 0x50, 0x54])  # sRGB
    # 35..37 established timings: none
    for i in range(8):  # standard timings: unused
        b[38 + 2 * i], b[39 + 2 * i] = 0x01, 0x01
    b[54:72] = _detailed_timing()
    b[72:90] = _text_descriptor(0xFC, "FNAV 576p50")
    # Range limits: V 49..51 Hz, H 30..40 kHz (the mode is 31.25 kHz), pixclock <= 30 MHz
    b[90:108] = bytes([0, 0, 0, 0xFD, 0, 49, 51, 30, 40, 3, 0x00, 0x0A, 0x20, 0x20, 0x20, 0x20, 0x20, 0x20])
    b[108:126] = bytes([0, 0, 0, 0x10, 0]) + bytes(13)  # dummy descriptor
    b[126] = 1  # one extension (CEA-861)
    _fix_checksum(b)
    return b


def build_cea() -> bytearray:
    e = bytearray(128)
    e[0], e[1] = 0x02, 0x03  # CEA-861, revision 3
    blocks = bytes(
        [(2 << 5) | 1, 0x80 | VIC]  # Video Data Block: VIC 17 (native)
        + [(3 << 5) | 5, 0x03, 0x0C, 0x00, 0x10, 0x00]  # HDMI VSDB: OUI 00-0C-03, phys addr 1.0.0.0
    )
    e[4 : 4 + len(blocks)] = blocks
    e[2] = 4 + len(blocks)  # DTD offset (no DTDs follow)
    e[3] = 0x00  # no underscan/audio/YCbCr
    _fix_checksum(e)
    return e


def build() -> bytes:
    return bytes(build_base() + build_cea())


def check(data: bytes) -> list[str]:
    """Return a list of problems (empty = all fine) and print the decoded mode."""
    problems: list[str] = []
    if len(data) not in (128, 256):
        problems.append(f"unexpected size {len(data)}")
    for i in range(0, len(data), 128):
        if sum(data[i : i + 128]) & 0xFF:
            problems.append(f"bad checksum in block {i // 128}")
    if data[:8] != bytes([0, 255, 255, 255, 255, 255, 255, 0]):
        problems.append("no EDID header")
    d = data[54:72]
    pclk = (d[0] | d[1] << 8) * 10
    ha, hb = d[2] | (d[4] >> 4) << 8, d[3] | (d[4] & 15) << 8
    va, vb = d[5] | (d[7] >> 4) << 8, d[6] | (d[7] & 15) << 8
    ht, vt = ha + hb, va + vb
    hz = pclk * 1000 / (ht * vt)
    print(f"mode: {ha}x{va} pixclock {pclk / 1000:.3f} MHz, total {ht}x{vt}, {hz:.3f} Hz")
    if (ha, va) != (H_ACTIVE, V_ACTIVE) or abs(hz - 50.0) > 0.01:
        problems.append("mode is not 720x576@50")
    if len(data) == 256 and data[128] != 0x02:
        problems.append("no CEA extension")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "--check":
        with open(argv[2], "rb") as f:
            problems = check(f.read())
        for p in problems:
            print("ERROR:", p, file=sys.stderr)
        return 1 if problems else 0
    if len(argv) != 2 or argv[1].startswith("-"):
        print(__doc__, file=sys.stderr)
        return 2
    data = build()
    problems = check(data)
    if problems:
        print("internal generator error:", problems, file=sys.stderr)
        return 1
    with open(argv[1], "wb") as f:
        f.write(data)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
