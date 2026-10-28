"""Задняя камера (RTSP): адрес с паролем, часы камеры, приём потока через rtsp_pull (файл вместо камеры),
второй экземпляр сервиса камеры (записи в rear/, выключена — disabled)."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from camhelpers import small_cam

from fnav.camera import rtsp_pull as rp
from fnav.camera.h264 import nal_types
from fnav.camera.rtspsrc import Clock, RtspCameraSource, url_host, with_credentials
from fnav.camera.service import REAR_DIR, CameraService, rear_camera_settings
from fnav.camera.sources import FakeSource
from fnav.models import LoresSettings, RearCameraSettings, Settings


def make_video(path: Path, n: int = 60, w: int = 320, h: int = 180, fps: int = 15, fmt: str = "mpegts") -> Path:
    """Короткое H.264-видео (libx264, GOP = fps) — «камера» для rtsp_pull."""
    import av

    rng = np.random.default_rng(3)
    noise = rng.integers(0, 255, (h, w, 3), dtype=np.uint8)
    with av.open(str(path), "w", format=fmt) as out:
        st = out.add_stream("libx264", rate=fps)
        st.width, st.height, st.pix_fmt = w, h, "yuv420p"
        st.codec_context.gop_size = fps
        st.codec_context.max_b_frames = 0
        for i in range(n):
            vf = av.VideoFrame.from_ndarray(np.roll(noise, i * 4, axis=1), format="rgb24").reformat(format="yuv420p")
            vf.pts = i
            for pkt in st.encode(vf):
                out.mux(pkt)
        for pkt in st.encode(None):
            out.mux(pkt)
    return path


def test_url_credentials_and_redaction():
    assert with_credentials("rtsp://10.0.0.5:554/h264", "admin", "p@ss:w/rd") == \
        "rtsp://admin:p%40ss%3Aw%2Frd@10.0.0.5:554/h264"
    assert with_credentials("rtsp://u:x@10.0.0.5/s", "admin", "y") == "rtsp://u:x@10.0.0.5/s"  # уже в адресе
    assert with_credentials("rtsp://10.0.0.5/s", "", "y") == "rtsp://10.0.0.5/s"
    assert rp.redact("rtsp://admin:secret@10.0.0.5/s") == "rtsp://admin:***@10.0.0.5/s"
    assert rp.redact("rtsp://10.0.0.5/s") == "rtsp://10.0.0.5/s"
    assert url_host("rtsp://admin:x@cam.local:554/s") == "cam.local"


def test_params_from_avcc_and_annexb():
    sps, pps = b"\x67\x42\x00\x1e\xab", b"\x68\xce\x38\x80"
    avcc = bytes([1, 0x42, 0, 0x1E, 0xFF, 0xE1]) + len(sps).to_bytes(2, "big") + sps + b"\x01" + \
        len(pps).to_bytes(2, "big") + pps
    assert rp.annexb_params(avcc) == rp.START_CODE + sps + rp.START_CODE + pps
    annexb = rp.START_CODE + sps + rp.START_CODE + pps
    assert rp.annexb_params(annexb) == annexb
    assert rp.annexb_params(b"") == b""
    assert rp.has_sps(annexb) and not rp.has_sps(rp.START_CODE + b"\x65\x88")
    framed = len(sps).to_bytes(4, "big") + sps + len(pps).to_bytes(4, "big") + pps
    assert rp.avcc_to_annexb(framed, 4) == annexb


def test_clock_follows_the_camera_and_only_grows():
    now = [100.0]
    c = Clock(lambda: now[0])
    assert c.map(5_000_000) == 100_000_000
    now[0] = 100.04
    assert c.map(5_040_000) == 100_040_000  # шаг камеры, не момент прихода
    assert c.map(5_040_000) == 100_040_001  # та же метка — всё равно растёт
    now[0] = 103.0
    assert c.map(5_080_000) == 103_000_000  # ушли больше чем на 2 с (обрыв, скачок) — привязка заново
    assert c.map(-1) == 103_000_001  # без метки — по нашим часам


class Sink:
    def __init__(self, lores_fps: float = 0.0) -> None:
        self.video: list[tuple[bytes, bool, int]] = []
        self.lores: list = []
        self.errors: list[str] = []
        self.lores_fps = lores_fps
        self.done = threading.Event()

    def on_video(self, au, key, ts):
        self.video.append((au, key, ts))

    def on_lores(self, frame):
        self.lores.append(frame)

    def wants_lores(self):
        return True

    def lores_demand_fps(self):
        return self.lores_fps

    def on_error(self, msg):
        self.errors.append(msg)
        self.done.set()


@pytest.mark.parametrize("fmt", ["mpegts", "mp4"])
def test_source_passes_h264_through_and_decodes_small_frames(tmp_path: Path, fmt: str):
    video = make_video(tmp_path / f"cam.{'ts' if fmt == 'mpegts' else 'mp4'}", fmt=fmt)
    sink = Sink(lores_fps=30)
    rear = RearCameraSettings(enabled=True, url=str(video), lores=LoresSettings(w=160, h=90, fps=30))

    def cmd(**kw):  # файл — в темпе меток (как камера), иначе декодер не успеет включиться
        from fnav.camera.rtspsrc import default_cmd

        return [*default_cmd(**kw), "--realtime", "--lores-fps", "30"]

    src = RtspCameraSource(rear, sink, cmd_factory=cmd)
    src.start()
    try:
        assert sink.done.wait(30), "the stream did not end"
    finally:
        src.stop()
    assert sink.errors and "stream ended" in sink.errors[0]  # конец файла = камера пропала → перезапуск
    assert len(sink.video) == 60
    first, key, _ = sink.video[0]
    assert key and 7 in nal_types(first) and 5 in nal_types(first)  # ключевой с SPS/PPS, Annex-B
    ts = [t for _, _, t in sink.video]
    assert ts == sorted(ts) and 3.0 < (ts[-1] - ts[0]) / 1e6 < 5.0  # 60 кадров по 1/15 с
    assert src.output_size == "320x180" and src.codec == "h264" and src.stream_fps == 15
    assert sink.lores, "no decoded frames"
    fr = sink.lores[-1]
    assert (fr.kind, fr.w, fr.h) == ("yuv420", 160, 90) and fr.data.shape == (135, 160)


def test_source_reports_unreachable_camera(tmp_path: Path):
    sink = Sink()
    src = RtspCameraSource(RearCameraSettings(enabled=True, url=str(tmp_path / "missing.ts")), sink)
    with pytest.raises(RuntimeError, match="rtsp://"):
        src.start()
    src = RtspCameraSource(RearCameraSettings(enabled=True, url=""), sink)
    with pytest.raises(RuntimeError, match="not set"):
        src.start()


def test_rear_settings_for_the_pipeline():
    s = Settings()
    s = s.model_copy(update={"rear_camera": RearCameraSettings(fps=20, bitrate_kbps=3000)})
    cam = rear_camera_settings(s)
    assert (cam.fps, cam.gop, cam.bitrate_kbps) == (20, 80, 3000)
    assert cam.segment_minutes == s.camera.segment_minutes and cam.lores == s.rear_camera.lores


def _rear_service(tmp_path: Path, enabled: bool, record: bool = True) -> CameraService:
    settings = tmp_path / "settings.json"
    rear = RearCameraSettings(enabled=enabled, url="rtsp://cam/s", record=record, fps=15)
    settings.write_text(Settings(rear_camera=rear).model_dump_json(), encoding="utf-8")

    def factory(_s, sink):
        return FakeSource(small_cam(), sink)

    return CameraService(root=tmp_path / "rec" / REAR_DIR, settings_path=settings, source_factory=factory,
                         rtsp_url=None, camera_id="rear", segment_s_override=2.0, ring_path=tmp_path / "ring")


def _wait(pred, timeout=15.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_rear_service_records_under_rear_and_idles_when_disabled(tmp_path: Path):
    svc = _rear_service(tmp_path, enabled=False)
    svc.start()
    try:
        time.sleep(0.5)
        st = svc.status()
        assert st.state == "disabled" and st.camera == "rear" and st.error is None and svc._p is None
    finally:
        svc.stop()

    svc = _rear_service(tmp_path, enabled=True)
    svc.start()
    try:
        assert _wait(lambda: svc.status().state == "recording")
        st = svc.status()
        assert st.current_segment.startswith("rear/") and st.camera == "rear"
        assert st.password_set is False and st.max_exposure_ms is None
        assert svc.protect_current().startswith("rear/")
        assert list((tmp_path / "rec" / REAR_DIR).rglob("*.ts"))
    finally:
        svc.stop()


def test_rear_without_recording_is_live(tmp_path: Path):
    svc = _rear_service(tmp_path, enabled=True, record=False)
    svc.start()
    try:
        assert _wait(lambda: svc.status().state == "live")
        assert svc.status().current_segment is None and not list((tmp_path / "rec").rglob("*.ts"))
        assert _wait(lambda: (svc.status().fps_actual or 0) > 5)  # частота — по входящему видео
        svc._no_space = True  # места нет, но записи нет — это не ошибка
        assert svc.status().state == "live" and svc.status().error is None
    finally:
        svc.stop()


def test_storage_front_cleans_both_but_not_the_rear_segment_being_written(tmp_path: Path):
    import os

    from fnav.camera.storage import cleanup, list_recordings, stats

    root = tmp_path / "rec"
    old = time.time() - 3600
    files = {}
    for rel in ("2026-10-27/a.ts", "rear/2026-10-27/b.ts", "2026-10-28/c.ts", "rear/2026-10-28/d.ts"):
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"\x47" * 188 * 10)
        files[rel] = f
    for i, rel in enumerate(("2026-10-27/a.ts", "rear/2026-10-27/b.ts", "2026-10-28/c.ts")):
        os.utime(files[rel], (old + i, old + i))  # d.ts — только что записан (текущий сегмент задней)
    assert [p.name for p in list_recordings(root, ("rear",))] == ["a.ts", "c.ts"]
    assert stats(root, ("rear",))[0] == 2 and stats(root)[0] == 4
    res = cleanup(root, target_free_bytes=10**15, free_fn=lambda _p: 0, skip_recent_s=120)
    assert sorted(p.name for p in res.deleted) == ["a.ts", "b.ts", "c.ts"]  # самые старые обеих камер
    assert files["rear/2026-10-28/d.ts"].exists()
