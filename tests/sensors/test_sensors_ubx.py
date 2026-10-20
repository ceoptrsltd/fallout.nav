from __future__ import annotations

import pytest
from sensors_fakes import nmea

from fnav.sensors import ubx


def hx(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", ""))


# Известные кадры (u-center / документация u-blox)
KNOWN = [
    (ubx.frame(0x0A, 0x04), "B5 62 0A 04 00 00 0E 34"),  # MON-VER poll
    (ubx.cfg_rate(10), "B5 62 06 08 06 00 64 00 01 00 01 00 7A 12"),  # 10 Гц
    (ubx.cfg_rate(5), "B5 62 06 08 06 00 C8 00 01 00 01 00 DE 6A"),  # 5 Гц
    (ubx.cfg_rate(1), "B5 62 06 08 06 00 E8 03 01 00 01 00 01 39"),  # 1 Гц
    (ubx.frame(0x06, 0x09, hx("00000000 FFFF0000 00000000 17")),
     "B5 62 06 09 0D 00 00 00 00 00 FF FF 00 00 00 00 00 00 17 31 BF"),  # CFG-CFG save all
    (ubx.cfg_prt_uart(115200, in_proto=0x07, out_proto=0x03),
     "B5 62 06 00 14 00 01 00 00 00 D0 08 00 00 00 C2 01 00 07 00 03 00 00 00 00 00 C0 7E"),
]


@pytest.mark.parametrize("got,want", KNOWN)
def test_known_frames(got: bytes, want: str) -> None:
    assert got == hx(want)


def test_checksum_fletcher() -> None:
    assert ubx.checksum(b"") == b"\x00\x00"
    assert ubx.checksum(bytes([0x0A, 0x04, 0x00, 0x00])) == bytes([0x0E, 0x34])


def test_parse_frame_roundtrip_and_errors() -> None:
    f = ubx.frame(0x06, 0x24, bytes(range(36)))
    m = ubx.parse_frame(f)
    assert m.key == (0x06, 0x24) and m.payload == bytes(range(36))
    bad = bytearray(f)
    bad[-1] ^= 0xFF
    with pytest.raises(ValueError):
        ubx.parse_frame(bytes(bad))
    with pytest.raises(ValueError):
        ubx.parse_frame(f[:-1])


def test_ack_nak() -> None:
    ack = ubx.parse_frame(ubx.frame(0x05, 0x01, bytes([0x06, 0x08])))
    nak = ubx.parse_frame(ubx.frame(0x05, 0x00, bytes([0x06, 0x08])))
    assert ack.is_ack_for(0x06, 0x08) is True
    assert nak.is_ack_for(0x06, 0x08) is False
    assert ack.is_ack_for(0x06, 0x24) is None
    other = ubx.parse_frame(ubx.frame(0x0A, 0x04))
    assert other.is_ack_for(0x06, 0x08) is None
    # известный кадр ACK-ACK на CFG-PRT
    assert ubx.frame(0x05, 0x01, bytes([0x06, 0x00])) == hx("B5 62 05 01 02 00 06 00 0E 37")


def test_stream_parser_mixed_and_split() -> None:
    gga = nmea("GNGGA,092750.000,5321.6802,N,00630.3372,W,1,8,1.03,61.7,M,55.2,M,,")
    line_bad = "$GNGGA,1,2,3*00"
    f1 = ubx.frame(0x05, 0x01, bytes([0x06, 0x08]))
    f2 = ubx.mon_ver_poll()
    data = b"\x00\xff garbage" + f1 + (gga + "\r\n").encode() + b"\xb5\x00" + (line_bad + "\r\n").encode() + f2
    p = ubx.StreamParser()
    items: list = []
    for i in range(0, len(data), 7):  # кусками
        items += p.feed(data[i : i + 7])
    kinds = [type(x).__name__ for x in items]
    assert kinds == ["UbxMessage", "str", "UbxMessage"]
    assert items[1] == gga
    assert items[0].is_ack_for(0x06, 0x08) is True


def test_nmea_checksum() -> None:
    assert ubx.nmea_checksum_ok("$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A")
    assert not ubx.nmea_checksum_ok("$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6B")
    assert not ubx.nmea_checksum_ok("hello")


def test_cfg_nav5_automotive() -> None:
    m = ubx.parse_frame(ubx.cfg_nav5(ubx.DYN_MODELS["automotive"]))
    assert m.key == (0x06, 0x24) and len(m.payload) == 36
    assert m.payload[0:2] == b"\x01\x00" and m.payload[2] == 4


def test_gnss_config_roundtrip_and_select() -> None:
    cfg = ubx.GnssConfig.default()
    parsed = ubx.parse_cfg_gnss(ubx.parse_frame(ubx.build_cfg_gnss(cfg)).payload)
    assert parsed.blocks == cfg.blocks
    new = ubx.gnss_config_for(parsed, ["gps", "glonass", "galileo"])
    assert sorted(new.enabled_names()) == ["galileo", "glonass", "gps", "qzss", "sbas"]
    only = ubx.gnss_config_for(parsed, ["glonass"])
    assert only.enabled_names() == ["glonass"]
    # сигнальная маска сохраняется
    q = next(b for b in new.blocks if b.gnss_id == 5)
    assert (q.flags >> 16) & 0xFF == 0x05
    assert sum(b.res_trk for b in new.blocks if b.enabled) <= new.num_trk_use


def test_mon_ver_parse() -> None:
    p = b"ROM CORE 3.01 (107888)".ljust(30, b"\0") + b"00080000".ljust(10, b"\0") + b"PROTVER=18.00".ljust(30, b"\0")
    v = ubx.parse_mon_ver(p)
    assert v["sw"].startswith("ROM CORE 3.01") and v["hw"] == "00080000" and v["ext"] == ["PROTVER=18.00"]
