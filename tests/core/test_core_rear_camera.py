"""Задняя камера в fnav-core: маршруты /api/camera/rear/…, логин/пароль RTSP, записи rear/ДАТА/…, сигнализация
защищает и снимает обе камеры, миграция настроек (клавиша 4 — камеры)."""

from __future__ import annotations

import asyncio
import json
import os
import time

import httpx
import pytest
from core_helpers import FakeUpstream, login, make_opts, running_app
from test_core_alarm import make_alarm, settle
from test_core_recordings import write_rec

from fnav import paths
from fnav.core import secretsfile as sf
from fnav.core.alarm import Detection
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


REAR_CUR = "rear/2026-11-02/2026-11-02_10-00-00.ts"
FRONT_CUR = "2026-11-02/2026-11-02_10-00-03.ts"


def test_rear_segment_being_written_is_in_progress_by_its_status(tmp_path):
    """Задняя по Wi-Fi молчит 3–15 с, её файл давно не менялся, но открыт: «идёт запись» — по статусу её сервиса
    (current_fn), а не только по mtime: удалять нельзя (иначе пишется в удалённый файл), HLS — без ENDLIST."""
    root = tmp_path / "rec"
    old = time.time() - 10  # 10 с без новых кадров
    for rel in (REAR_CUR, FRONT_CUR):
        write_rec(root / rel, 3, sidecar=True, end=False)
        os.utime(root / rel, (old, old))
    current: list[str] = []
    rec = Recordings(root, current_fn=lambda: current)
    assert {i["path"]: i["in_progress"] for i in rec._scan(None)} == {REAR_CUR: False, FRONT_CUR: False}
    current[:] = [FRONT_CUR, REAR_CUR]
    assert all(i["in_progress"] for i in rec._scan(None))
    assert rec._one(REAR_CUR, None)["in_progress"] is True
    assert "#EXT-X-ENDLIST" not in rec._hls(REAR_CUR, None)
    with pytest.raises(RecordingError) as e:
        asyncio.run(rec.delete(REAR_CUR, current=None, force=True))
    assert e.value.status == 409 and (root / REAR_CUR).is_file()
    current.clear()  # сегмент закрыт
    assert "#EXT-X-ENDLIST" in rec._hls(REAR_CUR, None)
    asyncio.run(rec.delete(REAR_CUR, current=None, force=False))
    assert not (root / REAR_CUR).exists()


async def test_core_knows_both_cameras_current_segments(data_dir):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/status":
            rear = req.url.port == 8083
            return httpx.Response(200, json=CameraStatus(
                state="recording", recording=True, camera="rear" if rear else "front",
                current_segment=REAR_CUR if rear else FRONT_CUR).model_dump(mode="json"))
        return httpx.Response(200, json={"ok": True})

    root = paths.recordings_dir()
    old = time.time() - 10
    write_rec(root / REAR_CUR, 3, sidecar=True, end=False)
    os.utime(root / REAR_CUR, (old, old))
    async with running_app(make_opts(local_transport=FakeUpstream(handler).transport)) as c:
        await login(c)
        await c.patch("/api/settings", json={"rear_camera": {"enabled": True, "url": "rtsp://10.0.0.9/s"}})
        core = c.app.state.core
        await core.camera_rear.poll()
        assert core.current_segments() == [FRONT_CUR, REAR_CUR]
        items = {i["path"]: i for i in (await c.get("/api/recordings")).json()}
        assert items[REAR_CUR]["in_progress"] is True
        assert (await c.delete(f"/api/recordings/file/{REAR_CUR}?force=1")).status_code == 409
        assert "#EXT-X-ENDLIST" not in (await c.get(f"/api/recordings/hls/{REAR_CUR}.m3u8")).text


async def test_core_saves_the_rear_address_found_by_mac(data_dir):
    """Сервис задней нашёл камеру по MAC на новом адресе (discovered_host) — ядро записывает хост в url и lores_url
    (сервис камеры settings.json не пишет); дальше статус без discovered_host — больше ничего не меняется."""
    found = {"host": "10.150.83.23"}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.port == 8083 and req.url.path == "/status":
            return httpx.Response(200, json=CameraStatus(state="recording", camera="rear",
                                                         discovered_host=found["host"]).model_dump(mode="json"))
        return httpx.Response(200, json={"ok": True})

    async with running_app(make_opts(local_transport=FakeUpstream(handler).transport)) as c:
        await login(c)
        await c.patch("/api/settings", json={"rear_camera": {
            "enabled": True, "url": "rtsp://172.23.204.5:8554/profile0", "lores_url": "rtsp://172.23.204.5:8554/profile1"}})
        core = c.app.state.core
        await core.camera_rear.poll()
        for _ in range(50):
            if core.store.get().rear_camera.url != "rtsp://172.23.204.5:8554/profile0":
                break
            await asyncio.sleep(0.01)
        rc = core.store.get().rear_camera
        assert (rc.url, rc.lores_url) == ("rtsp://10.150.83.23:8554/profile0", "rtsp://10.150.83.23:8554/profile1")
        assert load_settings_file(paths.settings_file())[0].rear_camera.url == rc.url
        found["host"] = None
        await core.camera_rear.poll()
        await asyncio.sleep(0.05)
        assert core.store.get().rear_camera.url == rc.url


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


async def test_storage_limit_status_reaches_the_admin(data_dir):
    """Защищённые теснят петлю (camera.md «Минимум петли»): поля статуса сервиса камеры доходят до админки."""
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.port == 8081 and req.url.path == "/status":
            st = CameraStatus(state="recording", protected_gb=9.84, loop_minutes=61, over_limit=True)
            return httpx.Response(200, json=st.model_dump(mode="json"))
        raise httpx.ConnectError("refused", request=req)

    async with running_app(make_opts(local_transport=FakeUpstream(handler).transport)) as c:
        await login(c)
        st = (await c.get("/api/camera/status")).json()
        assert (st["protected_gb"], st["loop_minutes"], st["over_limit"]) == (9.84, 61, True)


async def test_alarm_protects_and_snaps_both_cameras(tmp_path):
    svc, _, _ = make_alarm(tmp_path, arm_delay_s=0, vision=VisionSettings(enabled=True),
                           protect=lambda: _ok("2026-10-28/f.ts"))

    befores: list = []

    async def snap_front(before_s=0.0):
        befores.append(before_s)
        return b"\xff\xd8front\xff\xd9"

    async def snap_rear(before_s=0.0):
        befores.append(before_s)
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
    assert befores == [5.0, 5.0]  # снимки обеих камер — за 5 с до события
    await svc.clear_events()
    assert svc.snapshot_path(ev.id, "rear") is None
    await svc.stop()


async def _ok(seg: str) -> dict:
    return {"ok": True, "segment": seg}


async def test_alarm_takes_nothing_from_a_camera_without_video(tmp_path):
    """Камеры нет (задняя выключена) или видео с неё не идёт — у события ни её снимка (стоп-кадра из памяти
    сервиса), ни её записи. Камера пропала (video_lost) — её последние кадры и последний закрытый сегмент."""
    svc, clk, _ = make_alarm(tmp_path, arm_delay_s=0, video_loss_s=20)
    asked: list = []

    def cam(name: str):
        async def snap(before_s=0.0):
            asked.append((name, "snap", before_s))
            return b"\xff\xd8" + name.encode() + b"\xff\xd9"

        async def protect(recent=False):
            asked.append((name, "protect", recent))
            return {"ok": True, "segment": f"{name}.ts"}

        return snap, protect

    svc.snapshot, svc.protect = cam("front")
    svc.snapshot_rear, svc.protect_rear = cam("rear")
    video = {"front": True, "rear": False}
    svc.get_video = lambda: dict(video)
    await svc.start(background=False)
    await svc.arm()
    await svc._fire(Detection("impact", {"peak_g": 0.5}))  # задняя без видео — у неё ничего не спрашиваем
    assert sorted(asked) == [("front", "protect", False), ("front", "snap", 5.0)]
    ev = svc.events[0]
    assert ev.data["snapshot"] is True and "snapshot_rear" not in ev.data and "segment_rear" not in ev.data
    asked.clear()
    del video["rear"]  # задняя выключена в настройках
    video["front"] = False  # и передней нет (не подключена / сервис не работает)
    await svc._fire(Detection("tilt", {"tilt_deg": 4.0}))
    assert asked == [] and not ({"snapshot", "segment", "snapshot_rear", "segment_rear"} & set(svc.events[0].data))
    # пропала задняя: её кадр перед пропажей (since − 5 с) и последний сегмент; передняя — как обычно
    video.update(front=True, rear=False)
    asked.clear()
    await svc._fire(Detection("video_lost", {"camera": "rear", "since": clk.wall - 21}))
    assert sorted(asked) == [("front", "protect", False), ("front", "snap", 5.0), ("rear", "protect", True),
                             ("rear", "snap", 26.0)]
    assert svc.events[0].data["snapshot_rear"] is True and svc.events[0].data["segment_rear"] == "rear.ts"
    await svc.stop()


def test_settings_migration_key4_to_camera_switch(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"keypad": {"key4": "video_toggle", "key1": "protect"}}), encoding="utf-8")
    s, warnings = load_settings_file(f)
    assert s.keypad.key4 == "camera_switch" and s.keypad.key1 == "protect" and s.version == 2 and not warnings
    # видеовыход на другой клавише (у владельца — 5) и на удержании — тоже камеры
    f.write_text(json.dumps({"keypad": {"key4": "protect", "key5": "video_toggle", "key2_hold": "video_toggle"}}),
                 encoding="utf-8")
    s = load_settings_file(f)[0]
    assert (s.keypad.key4, s.keypad.key5, s.keypad.key2_hold) == ("protect", "camera_switch", "camera_switch")
    f.write_text(json.dumps({"version": 2, "keypad": {"key4": "video_toggle"}}), encoding="utf-8")
    assert load_settings_file(f)[0].keypad.key4 == "video_toggle"  # выбрано после миграции — не трогаем


async def test_client_arms_the_sound_watch_and_collects_loud_sounds(tmp_path):
    """Микрофон задней камеры: ядро включает громкие звуки по охране и забирает новые (seq), повторы — нет."""
    from fnav.core.camera import CameraService as CamClient

    cam = {"armed": False, "seq": 0, "events": []}
    posts: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/status":
            return httpx.Response(200, json=CameraStatus(
                state="recording", camera="rear", audio="pcm_mulaw 8000 Hz", sound_armed=cam["armed"],
                sound_seq=cam["seq"]).model_dump(mode="json"))
        if req.url.path == "/sound" and req.method == "POST":
            cam["armed"] = json.loads(req.content)["armed"]
            posts.append(cam["armed"])
            return httpx.Response(200, json={"ok": True, "armed": cam["armed"]})
        if req.url.path == "/sound/events":
            after = int(req.url.params.get("after", 0))
            return httpx.Response(200, json={"events": [e for e in cam["events"] if e["seq"] > after],
                                             "seq": cam["seq"]})
        raise httpx.ConnectError("refused", request=req)

    armed = [False]
    got: list = []
    c = CamClient(mock=False, recordings_root=tmp_path, base_url="http://cam:8083", camera_id="rear",
                  transport=httpx.MockTransport(handler), get_enabled=lambda: True,
                  get_sound_armed=lambda: armed[0], on_sound=got.append)
    st = await c.poll()
    assert posts == [] and st.audio == "pcm_mulaw 8000 Hz"  # охраны нет — сервис и так не пишет
    armed[0] = True
    st = await c.poll()
    assert posts == [True] and st.sound_armed is True
    ev = {"seq": 1, "start": 1000.0, "dur_s": 1.1, "peak_db": -14.4, "clip": "20261101-192914-000.wav",
          "clip_start": 995.0, "clip_dur_s": 9.1}
    cam["events"].append(ev)
    cam["seq"] = 1
    await c.poll()
    await c.poll()
    assert got == [ev]  # один раз
    cam["seq"], cam["events"] = 1, [dict(ev, clip="20261101-193001-000.wav")]  # сервис камеры перезапустился
    c._sound_seq = 5  # ядро помнит больше, чем у сервиса — начинаем с начала
    await c.poll()
    assert [e["clip"] for e in got] == ["20261101-192914-000.wav", "20261101-193001-000.wav"]
    armed[0] = False
    await c.poll()
    assert posts == [True, False]
    await c.stop()


async def test_client_knows_whether_video_flows(tmp_path):
    """Охрана: видео идёт — сервис отвечает, статус свежий, последний кадр не старше 3 с."""
    from fnav.core.camera import CameraService as CamClient

    cam = {"age": 0.4, "up": True}

    def handler(req: httpx.Request) -> httpx.Response:
        if not cam["up"]:
            raise httpx.ConnectError("refused", request=req)
        return httpx.Response(200, json=CameraStatus(state="recording", camera="rear",
                                                     video_age_s=cam["age"]).model_dump(mode="json"))

    c = CamClient(mock=False, recordings_root=tmp_path, base_url="http://cam:8083", camera_id="rear",
                  transport=httpx.MockTransport(handler), get_enabled=lambda: True)
    assert not c.has_video()  # ещё не опрашивали
    await c.poll()
    assert c.has_video()
    cam["age"] = 3.5  # кадры кончились
    await c.poll()
    assert not c.has_video()
    cam["age"] = None  # конвейер перезапущен, кадров нет
    await c.poll()
    assert not c.has_video()
    cam["age"] = 0.1
    await c.poll()
    assert c.has_video()
    c.status_mono -= 6  # опрос завис — о видео ничего не известно
    assert not c.has_video()
    cam["up"] = False  # сервис не отвечает
    await c.poll()
    assert not c.has_video()
    await c.stop()


async def test_client_passes_the_services_no_video_answers(tmp_path):
    """Сервис камеры отвечает, но кадра или записи нет (видео не идёт): ошибка — его словами, а не «сервис не
    отвечает»; охрана просит последнюю закрытую запись (recent=1)."""
    from fnav.core.camera import CameraService as CamClient
    from fnav.core.camera import CameraUnavailable

    seen: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.path, dict(req.url.params)))
        if req.url.path == "/snapshot.jpg":
            return httpx.Response(503, json={"ok": False, "error": "No video from the camera"})
        if req.url.path == "/protect-current" and req.url.params.get("recent") == "1":
            return httpx.Response(200, json={"ok": True, "segment": "rear/2026-11-02/2026-11-02_10-00-00.ts"})
        return httpx.Response(409, json={"ok": False, "error": "No segment is being recorded"})

    c = CamClient(mock=False, recordings_root=tmp_path, base_url="http://cam:8083", camera_id="rear",
                  transport=httpx.MockTransport(handler), get_enabled=lambda: True)
    with pytest.raises(CameraUnavailable, match="No video from the camera"):
        await c.snapshot(before_s=5)
    assert seen[-1] == ("/snapshot.jpg", {"before": "5"})
    with pytest.raises(CameraUnavailable, match="No segment is being recorded"):
        await c.protect_current()
    assert (await c.protect_current(recent=True))["segment"].startswith("rear/")
    await c.stop()
