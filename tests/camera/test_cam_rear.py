"""Задняя камера (RTSP): адрес с паролем, часы камеры, приём потока через rtsp_pull (файл вместо камеры),
второй экземпляр сервиса камеры (записи в rear/, выключена — disabled)."""

from __future__ import annotations

import io
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
from fnav.camera.rtspsrc import RtspCameraSource, url_host, with_credentials
from fnav.camera.service import REAR_DIR, CameraService, rear_camera_settings
from fnav.camera.sources import FakeSource
from fnav.models import LoresSettings, RearCameraSettings, Settings


def make_video(path: Path, n: int = 60, w: int = 320, h: int = 180, fps: int = 15, fmt: str = "mpegts",
               gop: int | None = None) -> Path:
    """Короткое H.264-видео (libx264, GOP = fps или gop) — «камера» для rtsp_pull."""
    import av

    rng = np.random.default_rng(3)
    noise = rng.integers(0, 255, (h, w, 3), dtype=np.uint8)
    with av.open(str(path), "w", format=fmt) as out:
        st = out.add_stream("libx264", rate=fps, options={"x264-params": "scenecut=0"} if gop else None)
        st.width, st.height, st.pix_fmt = w, h, "yuv420p"
        st.codec_context.gop_size = gop or fps
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

    def wants_lores(self, ts_us=None):
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


def pull_messages(video: Path, *, emit_video: bool, fps: float) -> list[tuple[int, int]]:
    """rtsp_pull по файлу (как можно быстрее, без темпа меток) → [(тип, флаги)] сообщений родителю."""
    buf = io.BytesIO()
    with pytest.raises(RuntimeError, match="stream ended"):
        rp.run(str(video), rp.Writer(buf), rp.Control(fps), emit_video=emit_video, lores_w=160)
    data, pos, out = buf.getvalue(), 0, []
    while pos < len(data):
        typ, flags, size, _ts = rp.HEADER.unpack_from(data, pos)
        pos += rp.HEADER.size + size
        out.append((typ, flags))
    return out


def test_second_stream_decodes_every_frame_and_thins_by_camera_time(tmp_path: Path):
    video = make_video(tmp_path / "cam.ts", n=45, gop=45)  # 3 с, один ключевой кадр
    main = pull_messages(video, emit_video=True, fps=1)
    assert sum(1 for t, f in main if t == rp.T_VIDEO and f & rp.F_KEY) == 1
    # основной поток при 1 к/с — только ключевые кадры (декод 1440p дорог): один малый кадр за 3 с
    assert sum(1 for t, _ in main if t == rp.T_LORES) == 1
    # второй поток декодируется весь (задний вид не ждёт ключевого кадра), 1 к/с — по меткам камеры, хотя файл
    # прочитан за доли секунды (по приходу вышел бы один кадр): 0; 0,93; 1,87; 2,8 с
    sub = pull_messages(video, emit_video=False, fps=1)
    assert [t for t, _ in sub].count(rp.T_LORES) == 4 and rp.T_VIDEO not in [t for t, _ in sub]
    # 15 к/с камеры при спросе 15 к/с — все кадры (раньше по приходу пачками терялась почти половина)
    assert [t for t, _ in pull_messages(video, emit_video=False, fps=15)].count(rp.T_LORES) == 45


def test_main_stream_lost_the_second_keeps_the_picture(tmp_path: Path, monkeypatch):
    """Камера не отдаёт основной поток, второй работает: перезапуска всего нет (on_error), картинка идёт со второго,
    основной переподключается сам с паузой; main_error — причина для статуса."""
    import fnav.camera.rtspsrc as rs

    monkeypatch.setattr(rs, "MAIN_RETRY_S", 0.3)
    bad = tmp_path / "main.ts"
    bad.write_bytes(b"not a video stream " * 200)  # как камера, закрывшая основной поток: Invalid data
    good = make_video(tmp_path / "sub.ts", n=45)
    starts = {"main": 0, "sub": 0}

    def cmd(**kw):
        starts["main" if kw["video"] else "sub"] += 1
        return [*rs.default_cmd(**kw), "--realtime", "--lores-fps", "15"]

    sink = Sink(lores_fps=15)
    lost: list[str] = []
    sink.on_main_lost = lost.append
    rear = RearCameraSettings(enabled=True, url=str(bad), lores_url=str(good), lores=LoresSettings(w=160, h=90, fps=15))
    src = RtspCameraSource(rear, sink, cmd_factory=cmd)
    src.start()
    try:
        assert _wait(lambda: len(sink.lores) >= 5 and starts["main"] >= 2, 30), (starts, len(sink.lores))
    finally:
        src.stop()
    assert not sink.errors, "the whole pipeline must not restart while the second stream shows the picture"
    assert not sink.video and lost and src.main_error == lost[-1]
    assert src.picture_mono > 0 and sink.lores[-1].w == 160


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
    # экспозиция, размеры кадра, поворот, живой битрейт передней — не задней
    front = s.camera.model_copy(update={"ev": 1.5, "exposure_mode": "long", "out_w": 1920, "out_h": 1080,
                                        "hflip": True, "live_bitrate_kbps": 1500, "max_exposure_ms_parked": 500})
    assert rear_camera_settings(s.model_copy(update={"camera": front})) == cam
    assert RearCameraSettings().fps == 15 and RearCameraSettings().bitrate_kbps == 2048  # как настроена C150


def _rear_service(tmp_path: Path, enabled: bool, record: bool = True) -> CameraService:
    settings = tmp_path / "settings.json"
    rear = RearCameraSettings(enabled=enabled, url="rtsp://cam/s", record=record, fps=15)
    settings.write_text(Settings(rear_camera=rear).model_dump_json(), encoding="utf-8")

    def factory(_s, sink):
        return FakeSource(small_cam(), sink)

    return CameraService(root=tmp_path / "rec" / REAR_DIR, settings_path=settings, source_factory=factory,
                         rtsp_url=None, camera_id="rear", segment_s_override=2.0, ring_path=tmp_path / "ring",
                         rear_state_path=tmp_path / "camera_rear.json", sound_dir=tmp_path / "clips")


def test_front_camera_changes_do_not_reconnect_the_rear(tmp_path: Path):
    """Правка передней (EV, экспозиция, размеры кадра, живой битрейт) не переподключает заднюю (раньше — да: дыра и
    новый файл в её записи); у задней — зеркало и оценки fps/битрейта тоже на ходу, длина сегмента и fsync (общие) —
    регистратору без переподключения; адрес, запись, малые кадры — переподключение."""
    svc = _rear_service(tmp_path, enabled=True)
    base = Settings.model_validate_json(svc.settings_path.read_text(encoding="utf-8"))

    def reload_with(**upd) -> bool:
        s = base.model_copy(update=upd)
        svc.settings_path.write_text(s.model_dump_json(), encoding="utf-8")
        return svc.reload()

    front = base.camera.model_copy(update={"ev": -1.0, "exposure_mode": "short", "out_w": 1280, "out_h": 960,
                                           "bitrate_kbps": 9000, "fps": 20, "live_bitrate_kbps": 1200})
    assert reload_with(camera=front) is False
    rc = base.rear_camera
    assert reload_with(rear_camera=rc.model_copy(update={"mirror": False, "fps": 25, "bitrate_kbps": 2560})) is False
    assert reload_with(camera=base.camera.model_copy(update={"segment_minutes": 5, "fsync_interval_s": 2.0})) is False
    rc = rc.model_copy(update={"url": "rtsp://10.0.0.7/s"})
    assert reload_with(rear_camera=rc) is True
    assert reload_with(rear_camera=rc.model_copy(update={"lores": LoresSettings(w=640, h=360, fps=15)})) is True

    (tmp_path / "live").mkdir()
    svc = _rear_service(tmp_path / "live", enabled=True)
    svc.start()
    try:
        assert _wait(lambda: svc.status().state == "recording")
        rec = svc._p.recorder
        svc.segment_s_override = None
        s = Settings.model_validate_json(svc.settings_path.read_text(encoding="utf-8"))
        s = s.model_copy(update={"camera": s.camera.model_copy(update={"segment_minutes": 3, "fsync_interval_s": 2.5})})
        svc.settings_path.write_text(s.model_dump_json(), encoding="utf-8")
        assert svc.reload() is False
        assert svc._p.recorder is rec and (rec.segment_s, rec.fsync_interval_s) == (180.0, 2.5)
    finally:
        svc.stop()


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


class _SilentSource:
    """Камера пропала: источник запускается, но кадров не даёт."""

    sensor_mode = output_size = None

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def request_keyframe(self) -> None: ...


def test_lost_camera_reports_video_age_and_protects_the_last_segment(tmp_path: Path):
    """Охрана: video_age_s — сколько нет кадров (после перезапуска конвейера без кадров — None); сегмента сейчас
    нет — защищается последний закрытый (в нём то, что было перед пропажей), если он моложе LAST_SEGMENT_S."""
    svc = _rear_service(tmp_path, enabled=True)
    dead = threading.Event()
    real = svc.source_factory
    svc.source_factory = lambda s, sink: _SilentSource() if dead.is_set() else real(s, sink)
    svc.start()
    try:
        assert _wait(lambda: svc.status().state == "recording")
        assert svc.status().video_age_s is not None and svc.status().video_age_s < 1.0
        dead.set()
        svc.restart()
        assert _wait(lambda: svc._p is not None and isinstance(svc._p.source, _SilentSource))
        st = svc.status()
        assert st.video_age_s is None and st.current_segment is None
        assert svc.protect_current() is None  # обычная защита: текущей записи нет — ничего
        rel = svc.protect_current(recent=True)  # охрана, камера пропала: последний закрытый сегмент
        assert rel is not None and rel.startswith("rear/")
        seg = tmp_path / "rec" / rel
        assert seg.is_file() and seg.with_name(seg.name + ".lock").is_file()
        path, rel0, _t = svc._last_segment
        svc._last_segment = (path, rel0, time.monotonic() - 601)  # старше 10 мин — не то, что было перед пропажей
        assert svc.protect_current(recent=True) is None
    finally:
        svc.stop()


def test_protect_takes_the_neighbours_at_a_segment_boundary(tmp_path: Path):
    """Защита у границы сегмента (FakeSource, сегменты по 3 с, ключевые кадры раз в 1 с): сразу после смены — и
    прошлый; у конца — и следующий, когда откроется; после перезапуска конвейера — и сегмент, закрытый при
    остановке (регистратор новый, о нём не знает). Ответ — по-прежнему текущий сегмент."""
    svc = _rear_service(tmp_path, enabled=True)
    svc.segment_s_override = 3.0
    root = tmp_path / "rec"

    def locked(rel: str) -> bool:
        return (root / (rel + ".lock")).is_file()

    def seg() -> str | None:
        return svc.status().current_segment

    svc.start()
    try:
        assert _wait(lambda: seg() is not None)
        rec = svc._p.recorder
        rec.protect_edge_s = 1.0
        first = seg()
        assert _wait(lambda: seg() not in (None, first))
        cur = seg()
        assert svc.protect_current() == cur
        assert locked(first) and locked(cur)

        assert _wait(lambda: seg() not in (None, cur))
        near = seg()
        assert _wait(lambda: rec._seg_last_ts - rec._seg_first_ts >= 2.2e6 if rec._seg_first_ts else False)
        assert svc.protect_current() == near and locked(near)
        assert _wait(lambda: seg() not in (None, near))
        after = seg()
        assert _wait(lambda: locked(after))  # следующий — как только открылся

        assert _wait(lambda: seg() not in (None, after))
        last = seg()
        assert _wait(lambda: rec._seg_last_ts - rec._seg_first_ts >= 1.2e6 if rec._seg_first_ts else False)
        assert not locked(last)
        svc.restart()
        assert _wait(lambda: svc._p is not None and svc._p.recorder is not rec and seg() not in (None, last))
        assert svc.protect_current() == seg()
        assert locked(last)  # закрыт при остановке конвейера только что

        (root / (last + ".lock")).unlink()
        path, rel, _t = svc._last_segment
        svc._last_segment = (path, rel, time.monotonic() - 61)  # давно — о событии в нём ничего
        assert svc.protect_current() is not None and not locked(last)
    finally:
        svc.stop()


class _MainDropSource:
    """Задняя с двумя потоками: основной (FakeSource) можно «уронить», второй дальше даёт картинку (picture_mono)."""

    def __init__(self, cam, sink) -> None:
        self.sink = sink
        self.main_error: str | None = None
        self.picture_mono = 0.0
        self.main_down = threading.Event()
        self._fake = FakeSource(cam, self)
        self.sensor_mode, self.output_size = self._fake.sensor_mode, self._fake.output_size
        self._stop = threading.Event()

    def start(self) -> None:
        self._fake.start()
        threading.Thread(target=self._second_stream, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._fake.stop()

    def request_keyframe(self) -> None:
        self._fake.request_keyframe()

    def drop_main(self) -> None:
        self.main_down.set()
        self.main_error = "Invalid data found when processing input"
        self.sink.on_main_lost(self.main_error)

    def main_back(self) -> None:
        self.main_error = None
        self.main_down.clear()

    def _second_stream(self) -> None:
        while not self._stop.wait(0.1):
            if self.main_down.is_set():
                self.picture_mono = time.monotonic()

    # кадры FakeSource → сервис (основной поток «упал» — видео не идёт)
    def on_video(self, au, key, ts) -> None:
        if not self.main_down.is_set():
            self.sink.on_video(au, key, ts)

    def on_lores(self, frame) -> None:
        self.sink.on_lores(frame)

    def wants_lores(self, ts_us=None) -> bool:
        return self.sink.wants_lores(ts_us)

    def on_error(self, msg) -> None:
        self.sink.on_error(msg)


def test_main_stream_lost_pauses_recording_but_keeps_the_picture(tmp_path: Path):
    """Основной поток пропал, второй живёт: конвейер не перезапускается (экран, снимки, fnav-watch), сегмент закрыт
    (не «идёт запись» без кадров), статус — ошибка с причиной, video_age_s свежий; защита охраны recent — закрытый
    сегмент; основной вернулся — снова запись."""
    svc = _rear_service(tmp_path, enabled=True)
    svc.frame_timeout_s = 1.0  # без картинки перезапуск был бы через 1 с
    made: list[_MainDropSource] = []

    def factory(_s, sink):
        made.append(_MainDropSource(small_cam(), sink))
        return made[-1]

    svc.source_factory = factory
    svc.start()
    try:
        assert _wait(lambda: svc.status().state == "recording")
        src = made[-1]
        src.drop_main()
        assert _wait(lambda: svc.status().current_segment is None)
        time.sleep(2.0)
        st = svc.status()
        assert svc._p is not None and svc._p.source is src and len(made) == 1, "the pipeline must stay up"
        assert st.state == "error" and not st.recording and "no main stream" in (st.error or "")
        assert st.video_age_s is not None and st.video_age_s < 1.0
        rel = svc.protect_current(recent=True)
        assert rel is not None and rel.startswith("rear/")
        src.main_back()
        assert _wait(lambda: svc.status().state == "recording")
        assert svc.status().error is None and svc.status().current_segment != rel
    finally:
        svc.stop()


def test_camera_lost_right_after_a_segment_change_protects_the_segment_before_too(tmp_path: Path):
    """Охрана «камера пропала» (recent): пропала через секунды после смены сегмента — подход в сегменте перед
    последним, защищаются оба; пропала в середине сегмента — только он (сегменты по 3 с, «недавно» — 1 с)."""
    svc = _rear_service(tmp_path, enabled=True)
    svc.segment_s_override = 3.0
    svc.frame_timeout_s = 1.0
    root = tmp_path / "rec"
    made: list[_MainDropSource] = []

    def factory(_s, sink):
        made.append(_MainDropSource(small_cam(), sink))
        return made[-1]

    def seg() -> str | None:
        return svc.status().current_segment

    def locked(rel: str) -> bool:
        return (root / (rel + ".lock")).is_file()

    svc.source_factory = factory
    svc.start()
    try:
        assert _wait(lambda: seg() is not None)
        rec = svc._p.recorder
        rec.protect_edge_s = 1.0
        first = seg()
        assert _wait(lambda: seg() not in (None, first), timeout=10)
        second = seg()
        made[-1].drop_main()  # сразу после смены
        assert _wait(lambda: seg() is None)
        assert svc.protect_current(recent=True) == second
        assert locked(second) and locked(first)

        made[-1].main_back()
        assert _wait(lambda: seg() not in (None, second))
        third = seg()
        assert _wait(lambda: rec._seg_first_ts is not None)
        first_ts = rec._seg_first_ts
        assert _wait(lambda: rec._seg_first_ts == first_ts and rec._seg_last_ts - first_ts >= 1.6e6)
        (root / (second + ".lock")).unlink()
        made[-1].drop_main()  # в середине сегмента: перед ним — давно
        assert _wait(lambda: seg() is None)
        assert svc.protect_current(recent=True) == third
        assert locked(third) and not locked(second)
    finally:
        svc.stop()


def test_camera_lost_right_after_a_pipeline_restart_protects_the_segment_before_too(tmp_path: Path):
    """То же через перезапуск конвейера: прошлый сегмент закрыл старый регистратор (новый о нём не знает), камера
    пропала через секунды — защищаются оба."""
    svc = _rear_service(tmp_path, enabled=True)
    svc.segment_s_override = 30.0
    svc.frame_timeout_s = 1.0
    root = tmp_path / "rec"
    made: list[_MainDropSource] = []

    def factory(_s, sink):
        made.append(_MainDropSource(small_cam(), sink))
        return made[-1]

    def seg() -> str | None:
        return svc.status().current_segment

    svc.source_factory = factory
    svc.start()
    try:
        assert _wait(lambda: seg() is not None)
        first = seg()
        time.sleep(1.1)  # имя следующего — на секунду позже
        svc.restart()
        assert _wait(lambda: len(made) == 2 and seg() not in (None, first))
        second = seg()
        made[-1].drop_main()
        assert _wait(lambda: seg() is None)
        assert svc.protect_current(recent=True) == second
        assert (root / (second + ".lock")).is_file() and (root / (first + ".lock")).is_file()
    finally:
        svc.stop()


def test_protect_next_survives_a_pipeline_restart(tmp_path: Path):
    """Событие у конца сегмента: «защитить и следующий» ждало его открытия, а конвейер перезапустился раньше
    (смена настроек, сбой) — первый сегмент нового регистратора всё равно защищается."""
    svc = _rear_service(tmp_path, enabled=True)
    svc.segment_s_override = 6.0
    root = tmp_path / "rec"

    def seg() -> str | None:
        return svc.status().current_segment

    svc.start()
    try:
        assert _wait(lambda: seg() is not None)
        rec = svc._p.recorder
        rec.protect_edge_s = 3.0
        cur = seg()
        assert _wait(lambda: rec._seg_first_ts is not None)
        first_ts = rec._seg_first_ts  # 0 у самого первого сегмента (метки FakeSource — с нуля)
        assert _wait(lambda: rec._seg_first_ts == first_ts and rec._seg_last_ts - first_ts >= 3.3e6)
        assert svc.protect_current() == cur and rec._protect_next_until is not None
        svc.restart()
        assert _wait(lambda: svc._p is not None and svc._p.recorder is not rec and seg() not in (None, cur))
        nxt = seg()
        assert _wait(lambda: (root / (nxt + ".lock")).is_file())
        assert svc._carry_next is None and svc._p.recorder.protect_next_mono is None  # один раз
    finally:
        svc.stop()


def test_pending_next_deadline_is_kept_on_the_monotonic_clock(tmp_path: Path):
    from fnav.camera.recorder import Recorder

    rec = Recorder(tmp_path, segment_s=600, fsync_interval_s=1, gop=15, fps=15)
    assert rec.pending_next_mono() is None
    rec._protect_next_until, rec._seg_last_ts = 50_000_000, 30_000_000  # ждём ещё 20 с по меткам
    t = time.monotonic()
    assert 19.5 < rec.pending_next_mono() - t < 20.5
    rec._seg_last_ts = 51_000_000  # срок прошёл
    assert rec.pending_next_mono() is None
    rec._protect_next_until = None
    rec.protect_next_mono = time.monotonic() + 5  # перенесённый с прошлого регистратора — дальше как есть
    assert rec.pending_next_mono() == rec.protect_next_mono
    rec.protect_next_mono = time.monotonic() - 1
    assert rec.pending_next_mono() is None
