"""Приём RTSP-потока IP-камеры (задняя камера) — всегда в отдельном процессе.

``python -m fnav.camera.rtsp_pull`` (адрес — в переменной окружения ``FNAV_RTSP_URL``: в нём может быть пароль, а
командную строку видно в ``ps``). PyAV при сетевом I/O держит GIL, поэтому в процессе камеры его использовать
нельзя — это остановило бы запись (как и в rtsp_relay.py).

Поток не перекодируется: H.264 из RTSP уходит родителю как есть (Annex-B; у ключевого кадра без SPS/PPS
в потоке — SPS/PPS из SDP). По запросу родителя (строка ``lores <к/с>`` в stdin, 0 — выкл) кадры ещё и
декодируются (программно), уменьшаются и отдаются как yuv420p. Полный декод дорог (1440×1080 на CM4 — ~24 мс
процессора на кадр + ~5 мс уменьшение): для экрана и распознавания лучше второй поток камеры (``lores_url``).

Сообщения в stdout: заголовок ``<BBIq`` (тип, флаги, длина, метка времени мкс по часам камеры или -1) + данные:
  * INFO  — JSON {"codec", "width", "height", "fps"};
  * VIDEO — access unit, флаг 1 — ключевой кадр;
  * LORES — ``<HH`` (ширина, высота) + плоскости yuv420p подряд;
  * ERROR — текст ошибки (перед выходом с кодом 1).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import struct
import sys
import threading
import time

HEADER = struct.Struct("<BBIq")
LORES_HEAD = struct.Struct("<HH")
T_INFO, T_VIDEO, T_LORES, T_ERROR = 1, 2, 3, 4
F_KEY = 1
URL_ENV = "FNAV_RTSP_URL"
# программный: на CM4 h264_v4l2m2m через PyAV тратит вдвое больше процессора (58 против 29 мс на кадр 1440×1080,
# замер 2026-10-28 — копирование кадров), поэтому только как запасной
DECODERS = ("h264", "h264_v4l2m2m")
START_CODE = b"\x00\x00\x00\x01"

log = logging.getLogger("fnav.camera.rtsp_pull")


def redact(url: str) -> str:
    """rtsp://user:pass@host/… → rtsp://user:***@host/… (для логов и ошибок)."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    auth, at, host = rest.partition("@")
    if not at or "/" in auth:
        return url
    user = auth.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


def annexb_params(extradata: bytes | None) -> bytes:
    """SPS/PPS из extradata потока в Annex-B: avcC (начинается с 1) → со стартовыми кодами; Annex-B — как есть."""
    if not extradata:
        return b""
    if extradata[:3] == b"\x00\x00\x01" or extradata[:4] == START_CODE:
        return bytes(extradata)
    if extradata[0] != 1 or len(extradata) < 7:
        return b""
    out = bytearray()
    pos = 5
    try:
        for count_mask in (0x1F, 0xFF):  # SPS, затем PPS
            n = extradata[pos] & count_mask
            pos += 1
            for _ in range(n):
                ln = int.from_bytes(extradata[pos:pos + 2], "big")
                pos += 2
                out += START_CODE + extradata[pos:pos + ln]
                pos += ln
    except IndexError:
        return b""
    return bytes(out)


def avcc_to_annexb(data: bytes, nal_len: int) -> bytes:
    """Кадр в формате avcC (NAL с длиной впереди, как в MP4) → Annex-B (со стартовыми кодами)."""
    out = bytearray()
    pos = 0
    while pos + nal_len <= len(data):
        n = int.from_bytes(data[pos:pos + nal_len], "big")
        pos += nal_len
        out += START_CODE + data[pos:pos + n]
        pos += n
    return bytes(out)


def has_sps(au: bytes) -> bool:
    """Есть ли в access unit (Annex-B) NAL SPS (тип 7)."""
    i = au.find(b"\x00\x00\x01")
    while i != -1 and i + 3 < len(au):
        if au[i + 3] & 0x1F == 7:
            return True
        i = au.find(b"\x00\x00\x01", i + 3)
    return False


class Writer:
    def __init__(self, out) -> None:
        self.out = out
        self.lock = threading.Lock()

    def send(self, typ: int, payload: bytes, flags: int = 0, ts: int = -1) -> None:
        with self.lock:
            self.out.write(HEADER.pack(typ, flags, len(payload), ts))
            self.out.write(payload)
            self.out.flush()


class Control:
    """Строки из stdin: ``lores <к/с>``. Читается в своём потоке."""

    def __init__(self, lores_fps: float = 0.0) -> None:
        self.lores_fps = lores_fps
        self.eof = False

    def run(self, inp) -> None:
        for line in inp:
            parts = line.decode("ascii", "replace").split()
            if len(parts) == 2 and parts[0] == "lores":
                try:
                    self.lores_fps = max(0.0, min(60.0, float(parts[1])))
                except ValueError:
                    pass
        self.eof = True  # родитель закрыл stdin — пора выходить


def open_decoder(extradata: bytes | None):
    import av

    last: Exception | None = None
    for name in DECODERS:
        try:
            cc = av.CodecContext.create(name, "r")
            if extradata:
                cc.extradata = extradata
            cc.open()
            return cc, name
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"no H.264 decoder: {last}")


def pts_us(pkt) -> int:
    if pkt.pts is None or pkt.time_base is None:
        return -1
    return int(pkt.pts * pkt.time_base * 1_000_000)


def run(url: str, w: Writer, ctl: Control, *, transport: str = "tcp", emit_video: bool = True,
        lores_w: int = 768, realtime: bool = False, timeout_s: float = 10.0) -> None:
    import av

    opts = {"rtsp_transport": transport, "allowed_media_types": "video", "max_delay": "500000"}
    if not url.startswith("rtsp"):
        opts = {}  # файл (тесты): без RTSP-опций
    container = av.open(url, options=opts, timeout=(timeout_s, timeout_s))
    try:
        if not container.streams.video:
            raise RuntimeError("no video stream")
        vs = container.streams.video[0]
        codec = vs.codec_context.name
        if codec != "h264":
            raise RuntimeError(f"video codec is {codec}, only H.264 is supported")
        extradata = vs.codec_context.extradata
        params = annexb_params(extradata)
        # RTSP отдаёт Annex-B; MP4/MKV (файл вместо камеры) — avcC: длины NAL вместо стартовых кодов
        nal_len = (extradata[4] & 3) + 1 if extradata and extradata[0] == 1 and len(extradata) > 4 else 0
        rate = vs.average_rate or vs.guessed_rate
        info = {"codec": codec, "width": vs.codec_context.width, "height": vs.codec_context.height,
                "fps": float(rate) if rate else None}
        w.send(T_INFO, json.dumps(info).encode())
        dec = None
        dec_name = None
        key_only = False  # ≤ 1 к/с — декодировать только ключевые кадры (свежий снимок почти даром)
        last_lores = 0.0
        t0 = time.monotonic()
        first_pts: int | None = None
        for pkt in container.demux(vs):
            if ctl.eof:
                return
            if pkt.size == 0:
                continue
            ts = pts_us(pkt)
            key = bool(pkt.is_keyframe)
            if realtime and ts >= 0:  # файл как «камера»: в темпе меток времени
                if first_pts is None:
                    first_pts = ts
                delay = t0 + (ts - first_pts) / 1e6 - time.monotonic()
                if delay > 0:
                    time.sleep(min(delay, 1.0))
            if emit_video:
                au = avcc_to_annexb(bytes(pkt), nal_len) if nal_len else bytes(pkt)
                if key and params and not has_sps(au):
                    au = params + au
                w.send(T_VIDEO, au, F_KEY if key else 0, ts)
            fps = ctl.lores_fps
            if fps <= 0:
                if dec is not None:
                    dec = None  # декодер не нужен — освободить (на Pi — аппаратный блок)
                continue
            if dec is not None and key_only != (fps <= 1):
                dec = None  # смена режима — заново с ключевого кадра (иначе P-кадры ссылаются на пропущенные)
            if dec is None:
                if not key:
                    continue  # декодирование начинаем с ключевого кадра
                dec, dec_name = open_decoder(extradata)
                key_only = fps <= 1
                if key_only:
                    try:
                        dec.skip_frame = "NONKEY"
                    except Exception as e:  # noqa: BLE001 — не умеет: декодирует всё, лишнее отбросим
                        log.info("decoder %s: no key-frame-only mode (%s)", dec_name, e)
                log.info("decoder %s%s", dec_name, " (key frames only)" if key_only else "")
            try:
                frames = dec.decode(pkt)
            except Exception as e:  # noqa: BLE001 — битый кадр: ждём следующий ключевой
                log.warning("decode: %s", e)
                dec = None
                continue
            for fr in frames:
                now = time.monotonic()
                if now - last_lores < 0.9 / fps:
                    continue
                last_lores = now
                lw = min(lores_w, fr.width) & ~1
                lh = max(2, round(lw * fr.height / fr.width)) & ~1
                small = fr.reformat(width=lw, height=lh, format="yuv420p")
                planes = b"".join(_plane_bytes(small, i) for i in range(3))
                w.send(T_LORES, LORES_HEAD.pack(lw, lh) + planes, 0, ts)
        raise RuntimeError("stream ended")
    finally:
        container.close()


def _plane_bytes(frame, i: int) -> bytes:
    """Плоскость без выравнивания строк (line_size может быть больше ширины)."""
    p = frame.planes[i]
    w = frame.width if i == 0 else frame.width // 2
    h = frame.height if i == 0 else frame.height // 2
    if p.line_size == w:
        return bytes(p)[: w * h]
    buf = memoryview(bytes(p))
    return b"".join(buf[r * p.line_size:r * p.line_size + w] for r in range(h))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m fnav.camera.rtsp_pull")
    ap.add_argument("--transport", choices=("tcp", "udp"), default="tcp")
    ap.add_argument("--no-video", action="store_true", help="only decoded lores frames (second stream)")
    ap.add_argument("--lores-w", type=int, default=768)
    ap.add_argument("--lores-fps", type=float, default=0.0)
    ap.add_argument("--realtime", action="store_true", help="pace a file input by its timestamps (tests)")
    args = ap.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    url = os.environ.get(URL_ENV, "")
    w = Writer(sys.stdout.buffer)
    ctl = Control(args.lores_fps)
    threading.Thread(target=ctl.run, args=(sys.stdin.buffer,), name="ctl", daemon=True).start()
    try:
        if not url:
            raise RuntimeError(f"{URL_ENV} is not set")
        run(url, w, ctl, transport=args.transport, emit_video=not args.no_video, lores_w=args.lores_w,
            realtime=args.realtime)
        return 0
    except BrokenPipeError:
        return 0
    except Exception as e:  # noqa: BLE001
        msg = str(e).replace(url, redact(url)) if url else str(e)
        try:
            w.send(T_ERROR, msg.encode("utf-8", "replace"))
        except (BrokenPipeError, OSError):
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
