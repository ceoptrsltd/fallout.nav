"""Ночная экспозиция: ступень AE в тюнинге, предел выдержки «едем/стоим», /motion, применение без перезапуска."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import ClassVar

import httpx
from test_cam_http import server, small_factory, wait_for  # noqa: F401  (фикстура)

from fnav.camera import picam
from fnav.camera.service import max_frame_us
from fnav.camera.sources import FakeSource
from fnav.models import CameraSettings

# фрагмент штатного ov5647.json (libcamera 0.7)
STOCK = {"version": 2.0, "algorithms": [
    {"rpi.lux": {"reference_lux": 987}},
    {"rpi.agc": {"channels": [{"exposure_modes": {
        "normal": {"shutter": [100, 10000, 30000, 60000, 66666], "gain": [1.0, 2.0, 4.0, 6.0, 8.0]},
        "short": {"shutter": [100, 5000, 10000, 20000, 66666], "gain": [1.0, 2.0, 4.0, 6.0, 8.0]},
        "long": {"exposure": [100, 10000, 30000, 60000, 120000], "gain": [1.0, 2.0, 4.0, 6.0, 8.0]},
    }}]}},
]}


def test_night_tuning_appends_a_stage_after_the_stock_maximum():
    t = picam.night_tuning(copy.deepcopy(STOCK))
    modes = t["algorithms"][1]["rpi.agc"]["channels"][0]["exposure_modes"]
    assert modes["normal"] == {"shutter": [100, 10000, 30000, 60000, 66666, 1_000_000],
                               "gain": [1.0, 2.0, 4.0, 6.0, 8.0, 16.0]}
    assert modes["short"]["shutter"][:5] == STOCK["algorithms"][1]["rpi.agc"]["channels"][0][
        "exposure_modes"]["short"]["shutter"]  # дневные ступени — как были
    assert modes["long"]["exposure"][-1] == 1_000_000 and modes["long"]["gain"][-1] == 16.0
    assert picam.night_tuning(copy.deepcopy(t)) == t  # повторно — без изменений
    assert t["algorithms"][0] == STOCK["algorithms"][0]


def test_sensor_model_from_v4l2_subdev(tmp_path: Path):
    for name, text in (("video0", "unicam-image"), ("v4l-subdev0", "ov5647 0-0036")):
        (tmp_path / name).mkdir()
        (tmp_path / name / "name").write_text(text + "\n")
    assert picam.sensor_model(tmp_path) == "ov5647"
    assert picam.sensor_model(tmp_path / "none") is None


def test_max_frame_by_motion():
    cam = CameraSettings()  # 15 fps, едем 66 мс, стоим 125 мс
    assert max_frame_us(cam, True) == 66667  # не короче кадра: частота не падает
    assert max_frame_us(cam, None) == 66667  # скорость неизвестна — как в движении
    assert max_frame_us(cam, False) == 125_000
    assert max_frame_us(cam.model_copy(update={"max_exposure_ms_driving": 100}), True) == 100_000


def test_picamera_source_frame_limits_without_camera():
    src = picam.PicameraSource(CameraSettings(), sink=None)  # type: ignore[arg-type]
    assert src._controls()["FrameDurationLimits"] == (66667, 66667)
    src.set_max_exposure(125_000)  # камера не открыта — только запоминается
    assert src._controls()["FrameDurationLimits"] == (66667, 125_000)
    src.set_max_exposure(10_000)  # короче кадра — кадр не короче 1/fps
    assert src._controls()["FrameDurationLimits"] == (66667, 66667)


class LimitSource(FakeSource):
    calls: ClassVar[list[int]] = []

    def set_max_exposure(self, us: int) -> None:
        LimitSource.calls.append(us)


def test_motion_endpoint_and_live_limits(server):  # noqa: F811
    base, svc, settings = server
    LimitSource.calls = []
    svc.source_factory = lambda s, sink: LimitSource(s.camera, sink, size=(160, 120), lores_size=(96, 72))
    svc.restart()
    wait_for(lambda: LimitSource.calls)
    assert LimitSource.calls[0] == 66667  # до старта: скорость неизвестна
    st = wait_for(lambda: (r := httpx.get(base + "/status").json())["state"] == "recording" and r)
    assert st["moving"] is None and st["max_exposure_ms"] == 66.667

    assert httpx.post(base + "/motion", json={"moving": False}).json() == {"ok": True, "max_exposure_ms": 125.0}
    assert LimitSource.calls[-1] == 125_000
    assert svc.status().moving is False
    assert svc._p.recorder.fps == 8.0  # кадр 125 мс — не «потерянный»
    assert httpx.post(base + "/motion", json={"moving": "yes"}).status_code == 400
    assert httpx.post(base + "/motion", content=b"nope").status_code == 400

    # предел на стоянке поменяли в админке: применяется без перезапуска записи
    seg = svc.status().current_segment
    settings.write_text(json.dumps({"camera": {"max_exposure_ms_parked": 200}, "storage": {"min_free_gb": 0.5}}))
    assert httpx.post(base + "/reload").json() == {"ok": True, "restarted": False}
    assert LimitSource.calls[-1] == 200_000
    assert svc.status().current_segment == seg

    assert httpx.post(base + "/motion", json={"moving": True}).json()["max_exposure_ms"] == 66.667
    assert LimitSource.calls[-1] == 66667 and svc._p.recorder.fps == 15.0
