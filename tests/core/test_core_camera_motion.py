"""Ядро сообщает камере «едем/стоим» (предел выдержки ночью): гистерезис по скорости и POST /motion."""

from __future__ import annotations

import json

import httpx

from fnav.core.camera import PARKED_AFTER_S, CameraService, MotionState


def test_motion_hysteresis():
    m = MotionState()
    assert m.update(None, 0.0) is None  # скорости нет и не было
    assert m.update(0.0, 1.0) is False  # первая оценка — сразу
    assert m.update(2.0, 2.0) is True  # поехали — сразу
    assert m.update(1.0, 3.0) is True  # между порогами — как было
    assert m.update(0.2, 4.0) is True  # медленно, но меньше 10 с (светофор)
    assert m.update(None, 10.0) is True  # нет фикса — прежнее решение
    assert m.update(0.1, 4.0 + PARKED_AFTER_S) is False
    assert m.update(1.0, 20.0) is False


async def test_poll_sends_motion_until_the_camera_reports_it(tmp_path):
    sent: list = []
    camera_moving: list = [None]
    speed = [0.0]

    def h(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/status":
            return httpx.Response(200, json={"state": "recording", "recording": True, "moving": camera_moving[0]})
        if req.url.path == "/motion":
            body = json.loads(req.content)
            sent.append(body["moving"])
            camera_moving[0] = body["moving"]
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    cam = CameraService(mock=False, recordings_root=tmp_path, transport=httpx.MockTransport(h),
                        get_speed=lambda: speed[0])
    st = await cam.poll()
    assert sent == [False] and st.moving is False
    await cam.poll()
    assert sent == [False]  # камера уже знает — не повторяем
    camera_moving[0] = None  # камеру перезапустили
    await cam.poll()
    assert sent == [False, False]
    speed[0] = 5.0
    st = await cam.poll()
    assert sent == [False, False, True] and st.moving is True
    await cam.stop()
