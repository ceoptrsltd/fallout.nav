"""История кадров MJPEG-хаба: снимок «за N секунд до» (охрана берёт кадр за 5 с до события)."""

from __future__ import annotations

from fnav.camera import mjpeg
from fnav.camera.mjpeg import MjpegHub


def test_history_keeps_recent_frames_at_most_twice_a_second(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(mjpeg.time, "monotonic", lambda: now[0])
    hub = MjpegHub()
    assert hub.frame_before(5) is None
    for i in range(100):  # 25 кадров/с (зритель MJPEG) 4 с подряд
        hub.publish(b"f%d" % i)
        now[0] += 0.04
    assert len(hub._history) == 8  # раз в 0,5 с
    age, jpg = hub.frame_before(3.0)
    assert jpg == b"f26" and abs(age - 3.0) < 0.3  # кадр, пришедший ~3 с назад (в истории — каждый 13-й)
    now[0] += 20  # кадров долго не было — история ограничена 12 с назад от нового кадра
    hub.publish(b"new")
    assert [j for _, j in hub._history] == [b"new"]
    age, jpg = hub.frame_before(5)
    assert jpg == b"new" and age == 0.0  # раньше нет — самый близкий
