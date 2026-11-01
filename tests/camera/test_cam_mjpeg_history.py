"""MJPEG-хаб: история кадров (охрана берёт кадр за 5 с до события), свежий кадр новому зрителю, прореживание по
меткам кадров."""

from __future__ import annotations

from fnav.camera import mjpeg
from fnav.camera.mjpeg import PART_HEAD, JpegWorker, MjpegHub, mjpeg_part


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


async def test_new_viewer_gets_a_fresh_frame_not_a_stale_one():
    hub = MjpegHub()
    assert hub.start_seq() == -1  # кадров ещё нет — первый, какой придёт
    hub.publish(b"a")
    assert await hub.next_frame(hub.start_seq(), timeout=0.01) == (1, b"a")  # свежий — сразу
    hub.jpeg_t -= 0.7  # без зрителей кодер пишет 1 к/с: последнему кадру 0,7 с
    seq = hub.start_seq()
    assert seq == 1 and await hub.next_frame(seq, timeout=0.01) is None  # не показывать его — ждать следующий
    hub.publish(b"b")
    assert await hub.next_frame(seq, timeout=0.01) == (2, b"b")


def test_jpeg_worker_thins_by_frame_time_not_by_arrival():
    hub = MjpegHub()
    hub.clients = 1  # зритель — полная частота
    w = JpegWorker(hub, fps=15, quality=70)  # поток кодера не запущен: только решение «нужен ли кадр»
    t0 = 1_000_000_000
    # кадры камеры 15 к/с с её метками приходят пачками (Wi-Fi) — по меткам нужны все
    assert all(w.wants_frame(t0 + i * 66_667) for i in range(30))
    hub.rates = {object(): 5.0}  # единственный зритель просит 5 к/с (удалённо)
    assert sum(w.wants_frame(t0 + 3_000_000 + i * 66_667) for i in range(30)) == 10
    assert w.wants_frame(t0 - 5_000_000)  # метки пошли заново (переподключение) — не замолкать


def test_mjpeg_part_carries_the_next_parts_headers():
    # Chromium показывает часть, только получив заголовки следующей: они идут сразу за кадром
    assert PART_HEAD == b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
    assert mjpeg_part(b"\xff\xd8x\xff\xd9") == b"\xff\xd8x\xff\xd9\r\n" + PART_HEAD
