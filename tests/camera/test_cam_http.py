"""HTTP API 8081 на настоящем uvicorn + сервис с синтетическим источником; RTSP-публикация; повтор при ошибке."""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from camhelpers import decode_file

from fnav.camera.recorder import TimeSync
from fnav.camera.rtsp import RtspPublisher
from fnav.camera.server import create_app
from fnav.camera.service import CameraService
from fnav.camera.sources import FakeSource
from fnav.camera.storage import list_recordings
from fnav.camera.tsmux import is_clean


def small_factory(settings, sink):
    return FakeSource(settings.camera, sink, size=(160, 120), lores_size=(96, 72))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(fn, timeout=15.0, step=0.1):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("timeout waiting for condition")


@pytest.fixture()
def server(tmp_path: Path):
    settings = tmp_path / "settings.json"
    svc = CameraService(root=tmp_path / "rec", settings_path=settings, source_factory=small_factory,
                        rtsp_url=None, time_sync=TimeSync(lambda: True))
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(create_app(svc), host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    wait_for(lambda: srv.started)
    base = f"http://127.0.0.1:{port}"
    try:
        yield base, svc, settings
    finally:
        srv.should_exit = True
        th.join(20)


def test_status_snapshot_protect(server):
    base, svc, _ = server
    st = wait_for(lambda: (r := httpx.get(base + "/status").json())["state"] == "recording" and r)
    assert st["recording"] and st["current_segment"].endswith(".ts")
    assert st["output_size"] == "160x120" and st["error"] is None and st["time_synced"] is True
    assert set(st) >= {"fps_actual", "bitrate_kbps", "frames_dropped", "free_gb", "recordings_count"}

    r = httpx.get(base + "/snapshot.jpg", timeout=10)
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.content[:2] == b"\xff\xd8" and r.content[-2:] == b"\xff\xd9"

    r = httpx.post(base + "/protect-current").json()
    assert r["ok"] is True and r["segment"] == st["current_segment"]
    assert (svc.root / (r["segment"] + ".lock")).exists()

    st2 = wait_for(lambda: (r := httpx.get(base + "/status").json())["fps_actual"] and r)
    assert 10 <= st2["fps_actual"] <= 20


def test_mjpeg_gives_frames_to_two_clients(server):
    base, _, _ = server
    wait_for(lambda: httpx.get(base + "/status").json()["state"] == "recording")

    def grab(out: list):
        with httpx.stream("GET", base + "/mjpeg", timeout=10) as r:
            assert r.headers["content-type"].startswith("multipart/x-mixed-replace; boundary=frame")
            buf = b""
            for chunk in r.iter_bytes():
                buf += chunk
                if buf.count(b"\xff\xd9") >= 3:
                    break
        out.append(buf)

    results: list[bytes] = []
    ths = [threading.Thread(target=grab, args=(results,)) for _ in range(2)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(20)
    assert len(results) == 2
    for buf in results:
        parts = [p for p in buf.split(b"--frame\r\n") if p]
        jpegs = [p.split(b"\r\n\r\n", 1)[1] for p in parts if b"\r\n\r\n" in p]
        complete = [j for j in jpegs if j.startswith(b"\xff\xd8") and b"\xff\xd9" in j]
        assert len(complete) >= 2


def test_reload_restarts_on_camera_change(server):
    base, svc, settings = server
    st = wait_for(lambda: (r := httpx.get(base + "/status").json())["state"] == "recording" and r)
    first = st["current_segment"]
    # без изменений — без перезапуска
    assert httpx.post(base + "/reload").json() == {"ok": True, "restarted": False}
    settings.write_text(json.dumps({"camera": {"bitrate_kbps": 800}, "storage": {"min_free_gb": 0.5}}))
    r = httpx.post(base + "/reload", timeout=10).json()
    assert r == {"ok": True, "restarted": True}
    assert svc.settings.camera.bitrate_kbps == 800
    time.sleep(1.1)  # новый сегмент получит другое имя (секунды)
    wait_for(lambda: (s := httpx.get(base + "/status").json())["state"] == "recording"
             and s["current_segment"] != first)
    old = svc.root / first
    assert is_clean(old) and decode_file(old)[1] == 0  # старый сегмент закрыт корректно

    assert httpx.post(base + "/restart").json() == {"ok": True}
    wait_for(lambda: httpx.get(base + "/status").json()["state"] == "recording")


async def test_protect_without_segment(tmp_path: Path):
    svc = CameraService(root=tmp_path, settings_path=tmp_path / "s.json", rtsp_url=None)
    transport = httpx.ASGITransport(app=create_app(svc, manage_service=False))
    async with httpx.AsyncClient(transport=transport, base_url="http://cam") as c:
        r = await c.post("/protect-current")
        assert r.status_code == 409 and r.json() == {"ok": False, "error": "No segment is being recorded"}
        assert (await c.get("/status")).json()["state"] == "stopped"


def test_source_failure_sets_error_and_retries(tmp_path: Path):
    calls = {"n": 0}

    def flaky(settings, sink):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("camera not found")
        return small_factory(settings, sink)

    svc = CameraService(root=tmp_path, settings_path=tmp_path / "s.json", source_factory=flaky,
                        rtsp_url=None, retry_s=0.5, time_sync=TimeSync(lambda: True))
    svc.start()
    try:
        st = wait_for(lambda: (s := svc.status()).state == "error" and s, timeout=5, step=0.02)
        assert st.error == "Camera: camera not found"
        wait_for(lambda: svc.status().state == "recording")
        assert svc.status().error is None
    finally:
        svc.stop()
    f = list_recordings(tmp_path)[0]
    assert is_clean(f)


def test_rtsp_publisher_feeds_subprocess(tmp_path: Path, frames_10s):
    out = tmp_path / "relay.ts"
    pub = RtspPublisher(str(out), cmd_factory=lambda url: [sys.executable, "-m", "fnav.camera.rtsp_relay", url,
                                                           "--format", "mpegts"])
    pub.start()
    for au, key, ts in frames_10s[3:60]:  # начинаем не с ключевого — публикатор его дождётся
        pub.push(au, key, ts)
        time.sleep(0.005)
    time.sleep(1.0)
    pub.stop()
    n, errors, frames = decode_file(out)
    assert pub.connects == 1
    assert errors == 0 and n == 60 - 15 and frames[0].key_frame


def test_rtsp_failure_does_not_block(tmp_path: Path, frames_10s):
    pub = RtspPublisher("rtsp://127.0.0.1:1/none", retry_s=0.2, queue_frames=5,
                        cmd_factory=lambda url: [sys.executable, "-c", "import sys; sys.exit(3)"])
    pub.start()
    t0 = time.monotonic()
    for _ in range(4):
        for au, key, ts in frames_10s:
            pub.push(au, key, ts)
        time.sleep(0.3)
    assert time.monotonic() - t0 < 3.0  # push никогда не блокирует
    wait_for(lambda: pub.connects >= 2, timeout=5)
    pub.stop()
    assert pub.last_error and "exited" in pub.last_error or "closed" in (pub.last_error or "")
