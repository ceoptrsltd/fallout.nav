"""Проверка камеры и регистратора НА УСТРОЙСТВЕ (Picamera2, аппаратный кодер, fsync на eMMC).

    sudo systemctl stop fnav-camera        # камерой владеет один процесс
    /opt/fnav/venv/bin/python -m fnav.camera.selftest [--seconds 30] [--out /var/lib/fnav/selftest] [--rtsp]
    sudo systemctl start fnav-camera

Печатает режимы сенсора, проверяет конфигурацию 2592×1944 → 1440×1080 (полное поле зрения),
пишет N секунд через тот же Recorder, проверяет файл (выравнивание, PyAV-декодирование, GOP, fps),
время fsync, JPEG lores, имитацию обрыва. Код возврата 0 — всё в порядке.
"""

from __future__ import annotations

import argparse
import logging
import random
import shutil
import statistics
import sys
import time
from pathlib import Path

from fnav.models import Settings

from .jpeg import LoresFrame, encode_jpeg
from .recorder import Recorder, SegmentFile, TimeSync
from .rtsp import DEFAULT_URL, RtspPublisher
from .tsmux import TS, is_clean, repair_ts

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, msg: str) -> bool:
    RESULTS.append((bool(ok), msg))
    print(("  [PASS] " if ok else "  [FAIL] ") + msg, flush=True)
    return bool(ok)


def section(title: str) -> None:
    print(f"\n== {title} ==", flush=True)


class Sink:
    def __init__(self, rec: Recorder, rtsp: RtspPublisher | None) -> None:
        self.rec = rec
        self.rtsp = rtsp
        self.frames = 0
        self.keys: list[int] = []
        self.sizes: list[int] = []
        self.ts: list[int] = []
        self.lores = 0
        self.jpeg_ms: list[float] = []
        self.jpeg_size = 0
        self.errors: list[str] = []

    def on_video(self, au: bytes, keyframe: bool, ts_us: int) -> None:
        if keyframe:
            self.keys.append(self.frames)
        self.frames += 1
        self.sizes.append(len(au))
        self.ts.append(ts_us)
        self.rec.push(au, keyframe, ts_us)
        if self.rtsp:
            self.rtsp.push(au, keyframe, ts_us)

    def on_lores(self, frame: LoresFrame) -> None:
        self.lores += 1
        if self.lores % 15 == 1:  # кодируем выборочно, чтобы оценить время simplejpeg
            t = time.perf_counter()
            j = encode_jpeg(frame, 70)
            self.jpeg_ms.append((time.perf_counter() - t) * 1000)
            self.jpeg_size = len(j)

    def wants_lores(self) -> bool:
        return True

    def on_error(self, msg: str) -> None:
        self.errors.append(msg)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m fnav.camera.selftest")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--out", type=Path, default=Path("/var/lib/fnav/selftest"))
    ap.add_argument("--rtsp", action="store_true", help=f"also publish to {DEFAULT_URL} (MediaMTX must run)")
    ap.add_argument("--keep", action="store_true", help="keep recorded files")
    args = ap.parse_args(argv)
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    cam = Settings().camera
    section("Picamera2 / sensor modes")
    try:
        import picamera2  # type: ignore[import-not-found]
        from picamera2 import Picamera2  # type: ignore[import-not-found]
    except Exception as e:  # noqa: BLE001
        check(False, f"import picamera2: {e}")
        return 1
    print("  picamera2", getattr(picamera2, "__version__", "?"))
    from .picam import PicameraSource, choose_mode, full_fov_modes

    p = Picamera2()
    try:
        print("  camera_properties:", {k: p.camera_properties.get(k) for k in
                                       ("Model", "PixelArraySize", "PixelArrayActiveAreas", "ScalerCropMaximum")})
        modes = p.sensor_modes
        for m in modes:
            print(f"  mode size={m.get('size')} crop_limits={m.get('crop_limits')} bit_depth={m.get('bit_depth')}"
                  f" fps={m.get('fps'):.2f} format={m.get('format')}")
        full = full_fov_modes(modes)
        print("  full-FoV modes:", [tuple(m["size"]) for m in full])
        chosen = choose_mode(modes, (cam.sensor_w, cam.sensor_h))
        check(tuple(chosen["size"]) == (2592, 1944), f"chosen sensor mode {chosen['size']} (want 2592x1944)")
        check(chosen.get("fps", 0) >= cam.fps, f"mode max fps {chosen.get('fps', 0):.2f} >= {cam.fps}")
    finally:
        p.close()

    out = args.out
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    fsync_ms: list[float] = []
    orig_sync = SegmentFile.sync

    def timed_sync(self):
        t = time.perf_counter()
        r = orig_sync(self)
        fsync_ms.append((time.perf_counter() - t) * 1000)
        return r

    SegmentFile.sync = timed_sync  # type: ignore[method-assign]

    section(f"Recording {args.seconds:.0f} s: {cam.sensor_w}x{cam.sensor_h} -> {cam.out_w}x{cam.out_h}@{cam.fps}")
    rec = Recorder(out, segment_s=3600, fsync_interval_s=cam.fsync_interval_s, gop=cam.gop, fps=cam.fps,
                   time_sync=TimeSync())
    rtsp = RtspPublisher(DEFAULT_URL, fps=cam.fps, width=cam.out_w, height=cam.out_h) if args.rtsp else None
    sink = Sink(rec, rtsp)
    src = PicameraSource(cam, sink)
    rec.start()
    if rtsp:
        rtsp.start()
    t_start = time.monotonic()
    try:
        src.start()
    except Exception as e:  # noqa: BLE001
        check(False, f"camera start: {e!r}")
        rec.stop()
        return 1
    try:
        cfg = src.picam2.camera_configuration()
        print("  sensor:", cfg.get("sensor"), " main:", cfg["main"]["size"], cfg["main"].get("format"),
              " lores:", cfg["lores"]["size"], cfg["lores"].get("format"))
        check(src.sensor_mode == f"{cam.sensor_w}x{cam.sensor_h}", f"sensor mode in use: {src.sensor_mode}")
        check(src.output_size == f"{cam.out_w}x{cam.out_h}", f"main size: {src.output_size}")
        md = src.picam2.capture_metadata()
        crop = md.get("ScalerCrop")
        print("  metadata ScalerCrop:", crop, " FrameDuration:", md.get("FrameDuration"),
              " ExposureTime:", md.get("ExposureTime"))
        full_area = src.picam2.camera_properties.get("PixelArraySize") or (cam.sensor_w, cam.sensor_h)
        if crop:
            covered = crop[2] / full_area[0], crop[3] / full_area[1]
            check(min(covered) > 0.98, f"field of view: ScalerCrop covers {covered[0]:.1%} x {covered[1]:.1%}")
        while time.monotonic() - t_start < args.seconds:
            time.sleep(0.5)
    finally:
        src.stop()
        rec.stop()
        if rtsp:
            rtsp.stop()
        SegmentFile.sync = orig_sync  # type: ignore[method-assign]

    section("Encoder output")
    dur = (sink.ts[-1] - sink.ts[0]) / 1e6 if len(sink.ts) > 1 else 0
    fps = (len(sink.ts) - 1) / dur if dur else 0
    kbps = sum(sink.sizes) * 8 / dur / 1000 if dur else 0
    gaps = [b - a for a, b in zip(sink.keys, sink.keys[1:], strict=False)]
    print(f"  frames={sink.frames} duration={dur:.1f}s fps={fps:.2f} bitrate={kbps:.0f} kbps keyframes={len(sink.keys)}"
          f" gop={sorted(set(gaps))}")
    check(abs(fps - cam.fps) < 0.5, f"fps {fps:.2f} ~ {cam.fps}")
    check(bool(gaps) and all(g == cam.gop for g in gaps), f"keyframe every {cam.gop} frames")
    check(0.4 * cam.bitrate_kbps < kbps < 1.6 * cam.bitrate_kbps, f"bitrate {kbps:.0f} kbps ~ {cam.bitrate_kbps}")
    check(rec.frames_dropped == 0, f"frames dropped: {rec.frames_dropped}")
    check(not sink.errors, f"source errors: {sink.errors}")
    if sink.jpeg_ms:
        check(statistics.median(sink.jpeg_ms) < 30,
              f"lores frames={sink.lores}, JPEG encode median {statistics.median(sink.jpeg_ms):.1f} ms,"
              f" {sink.jpeg_size / 1024:.0f} KiB")
    else:
        check(False, "no lores frames")
    if fsync_ms:
        check(max(fsync_ms) < 500, f"fsync count={len(fsync_ms)} median={statistics.median(fsync_ms):.1f} ms"
                                   f" max={max(fsync_ms):.1f} ms")
    if rtsp:
        check(rtsp.connects >= 1 and rtsp.dropped == 0,
              f"rtsp connects={rtsp.connects} dropped={rtsp.dropped} last_error={rtsp.last_error}")

    section("File check")
    files = sorted(out.rglob("*.ts"))
    check(len(files) == 1, f"files: {[f.relative_to(out).as_posix() for f in files]}")
    if not files:
        return 1
    f = files[0]
    size = f.stat().st_size
    check(size % TS == 0 and is_clean(f), f"{f.name}: {size / 1e6:.1f} MB, 188-aligned, end marker present")
    try:
        import av
    except Exception as e:  # noqa: BLE001
        check(False, f"PyAV not available for decode check: {e}")
        av = None
    if av is not None:
        n = errors = 0
        with av.open(str(f)) as c:
            st = c.streams.video[0]
            print(f"  stream: {st.codec_context.name} {st.width}x{st.height} rate={st.average_rate}"
                  f" duration={float(c.duration or 0) / 1e6:.1f}s")
            for pkt in c.demux(st):
                try:
                    n += len(pkt.decode())
                except av.error.FFmpegError:
                    errors += 1
        check(errors == 0 and n >= rec.frames_written - 1,
              f"decoded {n}/{rec.frames_written} frames, errors={errors}")

        section("Power-loss simulation (truncate at random byte + repair)")
        cut_file = out / "cut.ts"
        rnd = random.Random()
        cut = rnd.randrange(size // 2, size)
        with open(f, "rb") as src_f, open(cut_file, "wb") as dst:
            dst.write(src_f.read(cut))
        before, after = repair_ts(cut_file)
        m = 0
        with av.open(str(cut_file)) as c:
            for pkt in c.demux(video=0):
                m += len(pkt.decode())
        expected = n * cut / size
        check(after % TS == 0 and m >= expected - cam.fps * 1.5,
              f"cut at {cut}/{size}: repaired {before}->{after}, decoded {m} frames (~{expected:.0f} expected)")

    if not args.keep:
        shutil.rmtree(out, ignore_errors=True)
    failed = [m for ok, m in RESULTS if not ok]
    print(f"\nRESULT: {'PASS' if not failed else 'FAIL'} ({len(RESULTS) - len(failed)}/{len(RESULTS)})")
    for m in failed:
        print("  failed:", m)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
