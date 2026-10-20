import os
import time

import httpx
import pytest
from core_helpers import FakeUpstream, login, make_opts, running_app

from fnav import paths
from fnav.core.mockcam import _demo_ts
from fnav.core.recordings import RecordingError, parse_range, safe_rel_path, ts_duration
from fnav.models import CameraStatus

CURRENT = "2026-10-20/2026-10-20_16-50-00.ts"


def make_files(root):
    files = {
        "2026-10-19/2026-10-19_10-00-00.ts": b"A" * 1000,
        "2026-10-20/2026-10-20_16-40-00.ts": bytes(range(256)) * 40,
        CURRENT: b"C" * 500,
        "nosync/boot-abcd1234_0001.ts": b"N" * 188,
    }
    old = time.time() - 3600
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        os.utime(p, (old, old))
    nos = root / "nosync/boot-abcd1234_0001.ts"
    os.utime(nos, (1_760_000_000, 1_760_000_000))  # 2025 — «старый» файл без даты
    (root / "2026-10-19/2026-10-19_10-00-00.ts.lock").touch()
    (root / "2026-10-19/notes.txt").write_text("x")
    return files


def camera_handler(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/status":
        return httpx.Response(200, json=CameraStatus(state="recording", recording=True,
                                                     current_segment=CURRENT).model_dump(mode="json"))
    if req.url.path == "/protect-current":
        p = paths.recordings_dir() / CURRENT
        p.with_name(p.name + ".lock").touch()
        return httpx.Response(200, json={"ok": True, "segment": CURRENT})
    return httpx.Response(404)


def test_safe_paths():
    assert str(safe_rel_path("2026-10-20/2026-10-20_16-40-00.ts")) == "2026-10-20/2026-10-20_16-40-00.ts"
    for bad in ["../secrets.env", "2026-10-20/../../x.ts", "/etc/passwd", "C:/x/y.ts", "a\\b.ts",
                "2026-10-20/x.lock", "x.ts", "a/b/c.ts", "", "..", "2026-10-20/.hidden.ts", "./x.ts",
                "2026-10-20/..ts/../a.ts"]:
        with pytest.raises(RecordingError):
            safe_rel_path(bad)


def test_parse_range():
    assert parse_range(None, 100) is None
    assert parse_range("bytes=0-9", 100) == (0, 9)
    assert parse_range("bytes=90-", 100) == (90, 99)
    assert parse_range("bytes=-10", 100) == (90, 99)
    assert parse_range("bytes=50-1000", 100) == (50, 99)
    assert parse_range("items=1-2", 100) is None
    with pytest.raises(RecordingError):
        parse_range("bytes=100-", 100)


def test_ts_duration(tmp_path):
    p = tmp_path / "a.ts"
    p.write_bytes(_demo_ts(seconds=7))
    assert ts_duration(p) == 7.0
    p.write_bytes(b"\x00" * 1000)
    assert ts_duration(p) is None


async def test_recordings_api(data_dir):
    root = paths.recordings_dir()
    files = make_files(root)
    cam = FakeUpstream(camera_handler)
    async with running_app(make_opts(local_transport=cam.transport)) as c:
        assert (await c.get("/api/recordings")).status_code == 401
        await login(c)
        items = (await c.get("/api/recordings")).json()
        paths_ = [i["path"] for i in items]
        assert set(paths_) == set(files)
        assert paths_[-1] == "nosync/boot-abcd1234_0001.ts"  # сортировка по mtime для файлов без даты
        assert paths_[:3] == [CURRENT, "2026-10-20/2026-10-20_16-40-00.ts", "2026-10-19/2026-10-19_10-00-00.ts"]
        by = {i["path"]: i for i in items}
        assert by[CURRENT]["in_progress"] is True
        assert by["2026-10-19/2026-10-19_10-00-00.ts"]["protected"] is True
        assert by["2026-10-20/2026-10-20_16-40-00.ts"] | {} == {
            "path": "2026-10-20/2026-10-20_16-40-00.ts", "date": "2026-10-20", "start": "2026-10-20T16:40:00",
            "size_bytes": 10240, "duration_s": None, "protected": False, "in_progress": False}
        assert by["nosync/boot-abcd1234_0001.ts"]["start"] is None

        # скачивание целиком и по Range
        rel = "2026-10-20/2026-10-20_16-40-00.ts"
        data = files[rel]
        r = await c.get(f"/api/recordings/file/{rel}")
        assert r.status_code == 200 and r.content == data
        assert r.headers["content-type"] == "video/mp2t" and r.headers["accept-ranges"] == "bytes"
        r = await c.get(f"/api/recordings/file/{rel}", headers={"Range": "bytes=100-199"})
        assert r.status_code == 206 and r.content == data[100:200]
        assert r.headers["content-range"] == f"bytes 100-199/{len(data)}"
        r = await c.get(f"/api/recordings/file/{rel}", headers={"Range": "bytes=-5"})
        assert r.content == data[-5:]
        r = await c.get(f"/api/recordings/file/{rel}", headers={"Range": "bytes=99999-"})
        assert r.status_code == 416
        r = await c.get(f"/api/recordings/file/{rel}?download=1")
        assert "attachment" in r.headers["content-disposition"]
        r = await c.head(f"/api/recordings/file/{rel}")
        assert r.status_code == 200 and r.headers["content-length"] == str(len(data))

        # выход за каталог
        (paths.data_dir() / "secret.ts").write_bytes(b"x")
        for bad in ["..%2Fsecret.ts", "2026-10-20/..%2F..%2Fsecret.ts", "%2E%2E/secret.ts",
                    "2026-10-20%5C..%5C..%5Csecret.ts", "nosync/../../secret.ts"]:
            r = await c.get(f"/api/recordings/file/{bad}")
            assert r.status_code in (400, 404), bad
            assert r.content != b"x"
        assert (await c.get("/api/recordings/file/2026-10-20/none.ts")).status_code == 404

        # удаление
        r = await c.delete(f"/api/recordings/file/{CURRENT}")
        assert r.status_code == 409
        r = await c.delete("/api/recordings/file/2026-10-19/2026-10-19_10-00-00.ts")
        assert r.status_code == 409
        r = await c.delete("/api/recordings/file/2026-10-19/2026-10-19_10-00-00.ts?force=1")
        assert r.status_code == 200
        assert not (root / "2026-10-19/2026-10-19_10-00-00.ts").exists()
        assert not (root / "2026-10-19/2026-10-19_10-00-00.ts.lock").exists()
        assert (root / "2026-10-19").exists()  # не пустой (notes.txt) — не удаляем
        r = await c.delete(f"/api/recordings/file/{rel}")
        assert r.status_code == 200 and not (root / rel).exists()

        # защита
        r = await c.post("/api/recordings/protect", json={"path": "nosync/boot-abcd1234_0001.ts",
                                                          "protected": True})
        assert r.status_code == 200
        assert (root / "nosync/boot-abcd1234_0001.ts.lock").exists()
        await c.post("/api/recordings/protect", json={"path": "nosync/boot-abcd1234_0001.ts", "protected": False})
        assert not (root / "nosync/boot-abcd1234_0001.ts.lock").exists()
        r = await c.post("/api/recordings/protect", json={"path": "../x.ts", "protected": True})
        assert r.status_code == 400

        r = await c.post("/api/recordings/protect-current")
        assert r.json()["segment"] == CURRENT
        assert (root / (CURRENT + ".lock")).exists()


async def test_protect_current_unavailable_non_mock(data_dir):
    from core_helpers import FakeUpstream as FU

    from fnav.mock.sensors import MockSensorHub

    opts = make_opts(mock=False, local_transport=FU().transport, hub_factory=MockSensorHub)
    async with running_app(opts, client_addr=("127.0.0.1", 1)) as c:
        core = c.app.state.core
        res = await core.handle_button("press")
        assert res is None
        st = core.camera.status
        assert st.state == "error"
        assert core.hub.led == "blink_fast"


async def test_button_press_mock(data_dir):
    async with running_app() as c:
        core = c.app.state.core
        assert core.hub.led == "on"  # mock-камера «пишет»
        await login(c)
        r = await c.post("/api/mock/button", json={"event": "press"})
        seg = r.json()["result"]["segment"]
        assert seg and (paths.recordings_dir() / (seg + ".lock")).exists()
