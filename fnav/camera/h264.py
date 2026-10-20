"""Минимальные утилиты H.264 Annex-B: разбор NAL, AUD, кэш SPS/PPS."""

from __future__ import annotations

NAL_SLICE = 1
NAL_IDR = 5
NAL_SEI = 6
NAL_SPS = 7
NAL_PPS = 8
NAL_AUD = 9

AUD = b"\x00\x00\x00\x01\x09\xf0"


def iter_nals(au: bytes) -> list[tuple[int, int, int]]:
    """Список NAL в access unit: (начало стартового кода, конец NAL, тип NAL)."""
    res: list[tuple[int, int, int]] = []
    n = len(au)
    pos = au.find(b"\x00\x00\x01")
    starts: list[tuple[int, int]] = []  # (начало стартового кода, начало полезной части)
    while pos != -1:
        sc = pos - 1 if pos > 0 and au[pos - 1] == 0 else pos
        starts.append((sc, pos + 3))
        pos = au.find(b"\x00\x00\x01", pos + 3)
    for i, (sc, body) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else n
        if body < n:
            res.append((sc, end, au[body] & 0x1F))
    return res


def nal_types(au: bytes) -> list[int]:
    return [t for _, _, t in iter_nals(au)]


class AccessUnitFixer:
    """Готовит access unit к записи в MPEG-TS.

    * в начало — AUD (MPEG-TS требует его для H.264);
    * у ключевого кадра без SPS/PPS — добавляем последние известные SPS/PPS.
    """

    def __init__(self) -> None:
        self.sps: bytes | None = None
        self.pps: bytes | None = None

    @property
    def has_params(self) -> bool:
        return self.sps is not None and self.pps is not None

    def fix(self, au: bytes, keyframe: bool) -> tuple[bytes, bool]:
        nals = iter_nals(au)
        types = [t for _, _, t in nals]
        if NAL_IDR in types:
            keyframe = True
        for sc, end, t in nals:
            if t == NAL_SPS:
                self.sps = au[sc:end]
            elif t == NAL_PPS:
                self.pps = au[sc:end]
        need_params = keyframe and NAL_SPS not in types and self.has_params
        if types and types[0] == NAL_AUD:
            if not need_params:
                return au, keyframe
            aud_end = nals[0][1]
            head, rest = au[:aud_end], au[aud_end:]
        else:
            head, rest = AUD, au
        if need_params:
            head += self.sps + self.pps  # type: ignore[operator]
        return head + rest, keyframe

    def params(self) -> bytes:
        """SPS+PPS в Annex-B (для extradata) или b''."""
        if not self.has_params:
            return b""
        return self.sps + self.pps  # type: ignore[operator]
