"""Часы задней камеры (rtspsrc.Clock): метки камеры → наши монотонные часы. По Wi-Fi кадры приходят пачками —
метки должны сохранять шаг камеры (без ложной паузы и без «склейки» в 1 мкс); заново — только при новой шкале меток.
Звук идёт вторым RTSP-сеансом со своими часами: после пропусков он не должен расходиться с видео."""

from __future__ import annotations

import itertools
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fnav.camera import rtsp_pull as rp
from fnav.camera.rtspsrc import SLEW, Clock, RtspCameraSource
from fnav.models import RearCameraSettings

FRAME_US = 66_667  # 15 к/с


def run(clock: Clock, now: list[float], frames: list[tuple[int, float]]) -> list[int]:
    """[(метка камеры, мкс; приход, с)] → метки на наших часах."""
    out = []
    for cam, at in frames:
        now[0] = at
        out.append(clock.map(cam))
    return out


def stream(n: int, *, step_us: int = FRAME_US, base_us: int = 5_000_000, delay_s: float = 0.05,
           jitter_s: float = 0.01, gaps: tuple[tuple[float, float], ...] = (), backlog_s: float = 0.0,
           start_s: float = 100.0, seed: int = 1, rate: float = 1.0) -> list[tuple[int, float]]:
    """Поток камеры: кадр i снят в start_s + i·шаг (наши часы), метка камеры — base_us + i·шаг·rate (свой кварц).
    Приход — съёмка + delay_s + дрожание. gaps — пропуски Wi-Fi (начало, длина): снятое за пропуск TCP придержал и
    отдал пачкой в конце (по 5 мс на кадр). backlog_s — при подключении камера сразу отдаёт кадры за столько секунд
    из буфера (пачкой)."""
    rnd = random.Random(seed)
    out: list[tuple[int, float]] = []
    burst_at = start_s + backlog_s + delay_s
    for i in range(n):
        shot = start_s + i * step_us / 1e6
        at = shot + delay_s + rnd.uniform(0, jitter_s)
        if shot < start_s + backlog_s:
            at = burst_at + i * 0.001
        for g0, glen in gaps:
            if start_s + g0 <= shot < start_s + g0 + glen:
                k = int((shot - start_s - g0) * 1e6 / step_us)
                at = start_s + g0 + glen + delay_s + k * 0.005
        if out and at < out[-1][1]:
            at = out[-1][1]
        out.append((base_us + round(i * step_us * rate), at))
    return out


def test_wifi_burst_keeps_the_camera_spacing():
    """Пропуск Wi-Fi на 3 с, потом пачка: раньше пачка уезжала на 3 с позже (ложная пауза), а после догона ~2 с
    видео получали шаг 1 мкс. Теперь шаг — камеры, метка кадра из пропуска — там, где его сняли."""
    now = [0.0]
    c = Clock(lambda: now[0])
    frames = stream(30 * 15, gaps=((10.0, 3.0),))
    ts = run(c, now, frames)
    steps = [b - a for a, b in itertools.pairwise(ts)]
    assert min(steps) >= FRAME_US * (1 - SLEW) - 1 and max(steps) <= FRAME_US * 1.01, (min(steps), max(steps))
    assert c.anchors == 1
    # кадр из середины пропуска (11 с) — через 2 с после кадра на 9 с, как сняты (раньше — ~5 с)
    assert abs((ts[11 * 15] - ts[9 * 15]) - 2_000_000) < 10_000
    assert abs((ts[-1] - ts[0]) - (frames[-1][0] - frames[0][0])) < 20_000  # весь поток — длиной как у камеры


def test_new_timeline_reanchors_without_going_back():
    now = [100.0]
    c = Clock(lambda: now[0])
    assert c.map(5_000_000) == 100_000_000
    now[0] = 100.04
    assert c.map(5_040_000) == 100_040_000  # шаг камеры, не момент прихода
    assert c.map(5_040_000) == 100_040_001  # та же метка — всё равно растёт
    now[0] = 103.0
    late = c.map(5_080_000)  # опоздал на 2,9 с (пачка после пропуска) — шаг камеры, не привязка заново
    assert abs(late - 100_080_000) < 100 and c.anchors == 1
    now[0] = 104.0
    assert c.map(200_000) == 104_000_000 and c.anchors == 2  # метки пошли заново (камера перезапустила поток)
    now[0] = 104.05
    assert c.map(3_600_250_000) == 104_050_000 and c.anchors == 3  # скачок вперёд на час
    now[0] = 104.06
    c.reset()  # помощник переподключился: новая шкала
    assert c.map(10) == 104_060_000 and c.anchors == 4
    assert c.map(-1) == 104_060_001  # без метки — по нашим часам, но не назад
    now[0] = 150.0
    assert c.map(20) == 150_000_000 and c.anchors == 5  # опоздание > 30 с — это не пачка


def test_late_first_frames_converge_without_squashing():
    """При подключении камера отдаёт секунду из буфера пачкой: привязка по первому кадру на 1 с «позже» правды. Сдвиг
    сходится к наименьшей задержке не быстрее 5 % шага (запись на время схождения на 5 % быстрее), без склейки."""
    now = [0.0]
    c = Clock(lambda: now[0])
    frames = stream(60 * 15, backlog_s=1.0)
    ts = run(c, now, frames)
    steps = [b - a for a, b in itertools.pairwise(ts)]
    assert min(steps) >= FRAME_US * (1 - SLEW) - 1 and c.anchors == 1
    lag = [(at * 1e6 - t) / 1e3 for t, (_c, at) in zip(ts, frames, strict=False)][-15 * 10:]
    assert all(0 <= x <= 15 for x in lag), (min(lag), max(lag))  # потом — по самым быстрым кадрам (≤ дрожания)


def test_follows_crystal_drift_both_ways():
    for rate in (0.9999, 1.0001):  # кварц камеры на 100 ppm медленнее / быстрее нашего
        now = [0.0]
        c = Clock(lambda now=now: now[0])
        frames = stream(20 * 60 * 15, rate=rate, jitter_s=0.005)
        ts = run(c, now, frames)
        lag = [(at * 1e6 - t) / 1e3 for t, (_c, at) in zip(ts, frames, strict=False)]
        assert all(-1 <= x <= 25 for x in lag[15 * 60:]), (rate, min(lag), max(lag))  # не уходит за 20 мин
        assert c.anchors == 1


def test_audio_session_stays_aligned_with_video_through_gaps():
    """Видео — основной поток, звук — второй RTSP-сеанс со своими метками. Каждые часы держатся за наименьшую задержку
    своего потока, поэтому пропуски Wi-Fi и пачка при подключении их не разводят: расхождение — только разница
    наименьших задержек потоков (здесь 80 − 40 мс), и после каждого пропуска оно прежнее."""
    now = [0.0]
    video, audio = Clock(lambda: now[0]), Clock(lambda: now[0])
    gaps = ((30.0, 3.0), (70.0, 2.0), (100.0, 4.0))
    vf = stream(120 * 15, base_us=7_000_000, delay_s=0.08, jitter_s=0.02, gaps=gaps, backlog_s=1.0, seed=2)
    af = stream(120 * 25, step_us=40_000, base_us=123_000_000, delay_s=0.04, jitter_s=0.01, gaps=gaps, seed=3)
    events = sorted([(at, "v", i, cam) for i, (cam, at) in enumerate(vf)] +
                    [(at, "a", i, cam) for i, (cam, at) in enumerate(af)])
    vt, at_ = {}, {}
    for t, kind, i, cam in events:
        now[0] = t
        if kind == "v":
            vt[i] = video.map(cam)
        else:
            at_[i] = audio.map(cam)

    def skew_ms(sec: float) -> float:  # видео − звук для одного и того же момента съёмки (кратно 0,2 с)
        n = round(sec / 0.2)
        return (vt[3 * n] - at_[5 * n]) / 1e3

    for sec in (29.0, 34.0, 69.0, 73.0, 99.0, 105.0, 119.0):
        assert 20 <= skew_ms(sec) <= 60, (sec, skew_ms(sec))
    assert abs(skew_ms(34.0) - skew_ms(29.0)) < 15 and abs(skew_ms(105.0) - skew_ms(99.0)) < 15
    assert video.anchors == 1 and audio.anchors == 1


def test_aac_uses_the_pcm_offset():
    """AAC — на той же шкале, что PCM этого помощника (метки AAC — по отсчётам от начала PCM): сдвиг PCM, как бы он
    ни менялся, без задержки кодера; после переподключения — тоже."""
    now = [10.0]

    class Sink:
        def __init__(self) -> None:
            self.pcm: list[int] = []

        def on_audio(self, pcm, ts):
            self.pcm.append(ts)

    sink = Sink()
    rear = RearCameraSettings(enabled=True, url="rtsp://10.0.0.5/s", lores_url="rtsp://10.0.0.5/s2")
    src = RtspCameraSource(rear, sink, mono=lambda: now[0])
    aac: list[int] = []
    src.start_aac(lambda adts, ts, silent: aac.append(ts))
    for k in range(20):
        now[0] = 10.0 + k * 0.064 + (0.03 if k == 0 else 0.0)  # первый кусок PCM опоздал — потом сдвиг поправится
        src._on_sub_msg(rp.T_AUDIO, 0, 1_000_000 + k * 64_000, b"\0\0" * 512)
        now[0] += 0.13  # AAC приходит позже PCM (задержка кодера) — на метку это не влияет
        src._on_sub_msg(rp.T_AAC, 0, 1_000_000 + k * 64_000, b"adts")
    assert aac == sink.pcm
    src._audio_clock.reset()  # помощник второго потока переподключился
    now[0] = 50.0
    src._on_sub_msg(rp.T_AUDIO, 0, 0, b"\0\0" * 512)
    src._on_sub_msg(rp.T_AAC, 0, 0, b"adts")
    assert aac[-1] == sink.pcm[-1] == 50_000_000
