"""Задняя камера в fnav-core: маршруты /api/camera/rear/…, логин/пароль RTSP, записи rear/ДАТА/…, сигнализация
защищает и снимает обе камеры, миграция настроек (клавиша 4 — камеры)."""

from __future__ import annotations

import json
import os
import time

import httpx
from core_helpers import FakeUpstream, login, make_opts, running_app
from test_core_alarm import make_alarm, settle
from test_core_recordings import write_rec

from fnav import paths
from fnav.core import secretsfile as sf
from fnav.core.config import load_settings_file
from fnav.core.recordings import RecordingError, Recordings, camera_of, safe_rel_path
from fnav.models import CameraStatus, VisionSettings

JPEG = b"\xff\xd8rear\xff\xd9"


def test_rear_paths():
    assert str(safe_rel_path("rear/2026-10-28/2026-10-28_10-00-00.ts")) == "rear/2026-10-28/2026-10-28_10-00-00.ts"
    assert camera_of("rear/2026-10-28/x.ts") == "rear" and camera_of("2026-10-28/x.ts") == "front"
    for bad in ("front/2026-10-28/x.ts", "rear/../x.ts", "rear/2026-10-28/../../x.ts", "rear/x.ts",
                "rear/a/b/c.ts"):
        try:
            safe_rel_path(bad)
        except RecordingError:
            continue
        raise AssertionError(bad)


def test_recordings_list_both_cameras_and_hls_uri(tmp_path):
    root = tmp_path / "rec"
    old = time.time() - 3600
    for rel in ("2026-10-28/2026-10-28_10-00-00.ts", "rear/2026-10-28/2026-10-28_10-00-05.ts"):
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        write_rec(f, 3, sidecar=True)
        os.utime(f, (old, old))
    rec = Recordings(root)
    items = {i["path"]: i for i in rec._scan(None)}
    assert set(items) == {"2026-10-28/2026-10-28_10-00-00.ts", "rear/2026-10-28/2026-10-28_10-00-05.ts"}
    assert items["rear/2026-10-28/2026-10-28_10-00-05.ts"]["camera"] == "rear"
    assert items["rear/2026-10-28/2026-10-28_10-00-05.ts"]["date"] == "2026-10-28"
    assert rec.total_bytes() == sum(f.stat().st_size for f in root.rglob("*.ts"))
    assert rec.file("rear/2026-10-28/2026-10-28_10-00-05.ts").is_file()
    # плейлист …/hls/rear/ДАТА/ИМЯ.ts.m3u8 → куски …/file/rear/ДАТА/ИМЯ.ts
    assert "../../../file/rear/2026-10-28/2026-10-28_10-00-05.ts" in rec._hls("rear/2026-10-28/2026-10-28_10-00-05.ts",
                                                                              None)
    assert "../../file/2026-10-28/2026-10-28_10-00-00.ts" in rec._hls("2026-10-28/2026-10-28_10-00-00.ts", None)


async def test_rear_routes_off_then_mock(data_dir):
    async with running_app() as c:
        await login(c)
        r = await c.get("/api/camera/rear/status")
        assert r.json()["state"] == "disabled" and r.json()["camera"] == "rear" and r.json()["enabled"] is False
        r = await c.get("/api/camera/rear/snapshot.jpg")
        assert r.status_code == 503 and "off" in r.json()["detail"]
        await c.patch("/api/settings", json={"rear_camera": {"enabled": True, "url": "rtsp://10.0.0.9/s"}})
        r = await c.get("/api/camera/rear/status")
        st = r.json()
        assert st["state"] == "recording" and st["camera"] == "rear" and st["mock"] is True  # mock-камера
        assert (await c.get("/api/camera/rear/snapshot.jpg")).headers["content-type"] == "image/jpeg"
        assert (await c.get("/api/camera/status")).json()["camera"] == "front"


async def test_rear_credentials_go_to_secrets_not_settings(data_dir):
    async with running_app() as c:
        await login(c)
        r = await c.post("/api/camera/rear/credentials", json={"username": "admin", "password": "s3cret"})
        assert r.json() == {"ok": True, "username": "admin", "password_set": True}
        assert sf.read_secrets(paths.secrets_file())[sf.KEY_REAR_CAMERA_PASSWORD] == "s3cret"
        settings = (await c.get("/api/settings")).json()
        assert settings["rear_camera"]["username"] == "admin" and "s3cret" not in json.dumps(settings)
        r = await c.post("/api/camera/rear/credentials", json={"username": "viewer"})  # пароль не меняем
        assert r.json()["password_set"] is True and r.json()["username"] == "viewer"
        r = await c.post("/api/camera/rear/credentials", json={"password": ""})  # удалить
        assert r.json()["password_set"] is False


async def test_rear_whep_and_status_from_the_service(data_dir):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.port == 8083 and req.url.path == "/status":
            return httpx.Response(200, json=CameraStatus(state="live", camera="rear").model_dump(mode="json"))
        if req.url.path == "/rear/whep":
            return httpx.Response(201, content=b"v=0", headers={"Location": "/rear/whep/abc",
                                                                "Content-Type": "application/sdp"})
        raise httpx.ConnectError("refused", request=req)

    up = FakeUpstream(handler)
    async with running_app(make_opts(local_transport=up.transport)) as c:
        await login(c)
        await c.patch("/api/settings", json={"rear_camera": {"enabled": True, "url": "rtsp://10.0.0.9/s"}})
        assert (await c.get("/api/camera/rear/status")).json()["state"] == "live"
        r = await c.post("/api/camera/rear/whep", content=b"v=0", headers={"Content-Type": "application/sdp"})
        assert r.status_code == 201 and r.headers["location"] == "/api/camera/rear/whep/abc"


async def test_alarm_protects_and_snaps_both_cameras(tmp_path):
    svc, _, _ = make_alarm(tmp_path, arm_delay_s=0, vision=VisionSettings(enabled=True),
                           protect=lambda: _ok("2026-10-28/f.ts"))

    async def snap_front():
        return b"\xff\xd8front\xff\xd9"

    async def snap_rear():
        return JPEG

    svc.snapshot = snap_front
    svc.protect_rear = lambda: _ok("rear/2026-10-28/r.ts")
    svc.snapshot_rear = snap_rear
    await svc.start(background=False)
    await svc.arm()
    assert await svc.vision_event("approach", {"h": 0.4})
    await settle()
    ev = svc.events[0]
    assert ev.data["segment"] == "2026-10-28/f.ts" and ev.data["segment_rear"] == "rear/2026-10-28/r.ts"
    assert ev.data["snapshot"] is True and ev.data["snapshot_rear"] is True
    assert svc.snapshot_path(ev.id, "rear").read_bytes() == JPEG
    assert svc.snapshot_path(ev.id).read_bytes() != JPEG
    await svc.clear_events()
    assert svc.snapshot_path(ev.id, "rear") is None
    await svc.stop()


async def _ok(seg: str) -> dict:
    return {"ok": True, "segment": seg}


def test_settings_migration_key4_to_camera_switch(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"keypad": {"key4": "video_toggle", "key1": "protect"}}), encoding="utf-8")
    s, warnings = load_settings_file(f)
    assert s.keypad.key4 == "camera_switch" and s.keypad.key1 == "protect" and s.version == 2 and not warnings
    f.write_text(json.dumps({"version": 2, "keypad": {"key4": "video_toggle"}}), encoding="utf-8")
    assert load_settings_file(f)[0].keypad.key4 == "video_toggle"  # выбрано после миграции — не трогаем
