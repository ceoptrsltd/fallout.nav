"""Запасной RTSP-ретранслятор (если нет бинарника ffmpeg): MPEG-TS со stdin → PyAV → RTSP.

Запускается отдельным процессом (``python -m fnav.camera.rtsp_relay URL``): PyAV при сетевом I/O может
держать GIL, поэтому внутри процесса камеры его использовать нельзя — это остановило бы запись.
"""

from __future__ import annotations

import argparse
import sys


class _Pipe:
    """Несикабельный поток для PyAV (у stdin есть seek(), но он не работает) с read1 — без задержки."""

    def __init__(self, f) -> None:
        self._f = f

    def read(self, n: int) -> bytes:
        return self._f.read1(n)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--format", default="rtsp")
    args = ap.parse_args(argv)

    import av

    inp = av.open(_Pipe(sys.stdin.buffer), "r", format="mpegts",
                  options={"probesize": "500000", "analyzeduration": "1000000"})
    vin = inp.streams.video[0]
    opts = {"rtsp_transport": "tcp"} if args.format == "rtsp" else {}
    out = av.open(args.url, "w", format=args.format, options=opts)
    try:
        if hasattr(out, "add_stream_from_template"):
            vout = out.add_stream_from_template(vin)
        else:  # старый PyAV
            vout = out.add_stream(template=vin)
        for pkt in inp.demux(vin):
            if pkt.dts is None:
                continue
            pkt.stream = vout
            out.mux(pkt)
    finally:
        out.close()
        inp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
