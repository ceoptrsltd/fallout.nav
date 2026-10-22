"""Живое видео по требованию (/live.ts): второй энкодер на lores, MJPEG ?fps, запись не затрагивается."""

from __future__ import annotations

import asyncio
import itertools
import json
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from camhelpers import decode_file
from test_cam_http import free_port, small_factory, wait_for

from fnav.camera.live import LiveHub, LiveMux
from fnav.camera.recorder import TimeSync
from fnav.camera.server import create_app
from fnav.camera.service import CameraService
from fnav.camera.storage import list_recordings
from fnav.camera.tsmux import PID_PAT, SYNC, TS


def _serve(svc: CameraService):
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(create_app(svc), host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    wait_for(lambda: srv.started)
    return srv, th, f"http://127.0.0.1:{port}"


@pytest.fixture()
def live_server(tmp_path: Path):
    svc = CameraService(root=tmp_path / "rec", settings_path=tmp_path / "settings.json",
                        source_factory=small_factory, rtsp_url=None, time_sync=TimeSync(lambda: True),
                        live_grace_s=0.3)
    srv, th, base = _serve(svc)
    try:
        yield base, svc
    finally:
        srv.should_exit = True
        th.join(20)


def live_starts(svc: CameraService) -> int:
    return svc._p.source.live_starts  # FakeSource текущего конвейера


def grab_live(base: str, seconds: float, out: list, path: str = "/live.ts") -> None:
    buf = b""
    t0 = time.monotonic()
    with httpx.stream("GET", base + path, timeout=10) as r:
        out.append(r.status_code)
        assert r.headers["content-type"] == "video/mp2t"
        for chunk in r.iter_bytes():
            buf += chunk
            if time.monotonic() - t0 > seconds:
                break
    out.append(buf)


def test_live_ts_two_clients_and_encoder_stops(live_server, tmp_path: Path):
    base, svc = live_server
    seg0 = wait_for(lambda: (r := httpx.get(base + "/status").json())["state"] == "recording"
                    and r["current_segment"])
    assert not svc.live_running

    results: list[list] = [[], []]
    ths = [threading.Thread(target=grab_live, args=(base, 2.5, results[i])) for i in range(2)]
    for t in ths:
        t.start()
    wait_for(lambda: svc.live_running, timeout=5)
    for t in ths:
        t.join(20)
    for status, data in results:
        assert status == 200
        assert len(data) % TS == 0 and data[0] == SYNC
        assert ((data[1] & 0x1F) << 8 | data[2]) == PID_PAT  # поток начинается с PAT/PMT + ключевого кадра
        f = tmp_path / f"live{id(data)}.ts"
        f.write_bytes(data)
        n, errors, frames = decode_file(f)
        assert errors == 0 and n >= 15
        assert frames[0].key_frame and (frames[0].width, frames[0].height) == (96, 72)  # lores, не main

    # зрителей нет → энкодер выключается после паузы
    wait_for(lambda: not svc.live_running, timeout=5)
    assert live_starts(svc) == 1  # оба клиента делили один энкодер

    # запись шла всё время в тот же сегмент (синтетический источник на ПК делит GIL с клиентами теста —
    # единичные «пропуски» по меткам времени возможны; на устройстве проверяется отдельно)
    st = httpx.get(base + "/status").json()
    assert st["state"] == "recording" and st["current_segment"] == seg0 and st["frames_dropped"] <= 10

    # повторное подключение снова включает энкодер
    again: list = []
    grab_live(base, 1.0, again)
    assert again[0] == 200 and len(again[1]) > 0
    wait_for(lambda: not svc.live_running, timeout=5)


def test_live_restarts_with_pipeline_and_bitrate_change_keeps_recording(live_server):
    base, svc = live_server
    wait_for(lambda: httpx.get(base + "/status").json()["state"] == "recording")
    res: list = []
    th = threading.Thread(target=grab_live, args=(base, 4.0, res))
    th.start()
    wait_for(lambda: svc.live_running, timeout=5)
    seg = httpx.get(base + "/status").json()["current_segment"]
    # только битрейт живого видео: запись не перезапускается, live-энкодер — да
    svc.settings_path.write_text(json.dumps({"camera": {"live_bitrate_kbps": 300}}))
    assert httpx.post(base + "/reload").json() == {"ok": True, "restarted": False}
    assert live_starts(svc) == 2
    assert httpx.get(base + "/status").json()["current_segment"] == seg
    th.join(20)
    assert res[0] == 200
    f = svc.root / "live.ts"
    f.write_bytes(res[1])
    n, errors, _ = decode_file(f)
    assert errors == 0 and n >= 30


def test_live_unavailable_returns_503_and_recording_continues(tmp_path: Path):
    def factory(settings, sink):
        src = small_factory(settings, sink)

        def broken(*_a, **_kw):
            raise RuntimeError("no second encoder")

        src.start_live = broken
        return src

    svc = CameraService(root=tmp_path / "rec", settings_path=tmp_path / "s.json", source_factory=factory,
                        rtsp_url=None, time_sync=TimeSync(lambda: True), live_grace_s=0.1)
    srv, th, base = _serve(svc)
    try:
        wait_for(lambda: httpx.get(base + "/status").json()["state"] == "recording")
        r = httpx.get(base + "/live.ts", timeout=10)
        assert r.status_code == 503 and "no second encoder" in r.json()["error"]
        assert not svc.live_running and not svc.live.running
        time.sleep(1.0)
        st = httpx.get(base + "/status").json()
        assert st["state"] == "recording" and st["error"] is None
    finally:
        srv.should_exit = True
        th.join(20)
    f = list_recordings(svc.root)[0]
    assert decode_file(f)[1] == 0


def test_live_when_camera_not_running(tmp_path: Path):
    svc = CameraService(root=tmp_path, settings_path=tmp_path / "s.json", rtsp_url=None)

    async def run():
        transport = httpx.ASGITransport(app=create_app(svc, manage_service=False))
        async with httpx.AsyncClient(transport=transport, base_url="http://cam") as c:
            r = await c.get("/live.ts")
            assert r.status_code == 503 and r.json() == {"ok": False, "error": "Camera is not running"}

    asyncio.run(run())


def test_mjpeg_fps_throttle(live_server):
    base, _ = live_server
    wait_for(lambda: httpx.get(base + "/status").json()["state"] == "recording")

    def count(path: str, seconds: float) -> int:
        buf = b""
        t0 = time.monotonic()
        with httpx.stream("GET", base + path, timeout=10) as r:
            for chunk in r.iter_bytes():
                buf += chunk
                if time.monotonic() - t0 > seconds:
                    break
        return buf.count(b"--frame\r\n")

    slow = count("/mjpeg?fps=2", 2.6)
    fast = count("/mjpeg", 2.6)
    assert 3 <= slow <= 7
    assert fast >= 2 * slow


# ───────────── модули без сервера ─────────────


def test_live_mux_waits_for_key_and_keeps_pts_monotonic(frames_10s):
    mux = LiveMux(15)
    out = [mux.push(au, key, ts) for au, key, ts in frames_10s[3:40]]
    first = next(i for i, r in enumerate(out) if r is not None)
    assert all(r is None for r in out[:first]) and out[first][1] is True  # первым идёт ключевой кадр
    mux.new_session()  # «перезапуск энкодера»: метки времени начинаются заново
    out2 = [mux.push(au, key, ts) for au, key, ts in frames_10s[:30]]
    data = b"".join(r[0] for r in out + out2 if r is not None)
    import av

    p = Path(__file__).parent / "_live_mux_tmp.ts"
    try:
        p.write_bytes(data)
        with av.open(str(p)) as c:
            pts = [pkt.pts for pkt in c.demux(c.streams.video[0]) if pkt.pts is not None]
    finally:
        p.unlink(missing_ok=True)
    assert len(pts) >= 50 and all(b > a for a, b in itertools.pairwise(pts))


def test_live_hub_slow_client_resyncs_on_keyframe():
    calls: list[bool] = []

    async def run():
        hub = LiveHub(lambda w: calls.append(w), grace_s=0.05, queue_max=3)
        c = await hub.subscribe()
        hub._dispatch(b"p", False)  # до ключевого кадра — не отдаём
        assert c.queue.empty()
        for i in range(5):  # очередь на 3: переполнение → сброс, ждём ключевой
            hub._dispatch(b"K" if i == 0 else b"p", i == 0)
        assert c.need_key and c.dropped == 1 and c.queue.empty()
        hub._dispatch(b"p", False)
        assert c.queue.empty()
        hub._dispatch(b"K", True)
        assert c.queue.get_nowait() == b"K"
        hub.unsubscribe(c)
        hub.unsubscribe(c)  # повторно — безопасно
        await asyncio.sleep(0.2)
        assert not hub.running

    asyncio.run(run())
    assert calls == [True, False]


def test_jpeg_rate_follows_slowest_needed_client():
    from fnav.camera.mjpeg import JpegWorker, MjpegHub

    hub = MjpegHub()
    w = JpegWorker(hub, 15, 70)
    hub.clients = 1
    hub.rates = {1: 2.0}

    def accepted(seconds: float) -> int:
        n, t = 0, time.monotonic()
        while time.monotonic() - t < seconds:
            n += w.wants_frame()
            time.sleep(1 / 30)
        return n

    assert 2 <= accepted(1.5) <= 4  # кодируем только ~2 кадра/с, а не 15
    hub.rates[2] = None  # клиент без ограничения → полная частота
    assert accepted(1.0) >= 10
