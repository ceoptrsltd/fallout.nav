"""Трек GPS к записям (fnav/core/recgps.py): ~1 точка/с в <сегмент>.ts.gps текущего сегмента каждой камеры, отдача
плееру (GET /api/recordings/gps/…, GPX), удаление вместе с записью, список и объём записей его не видят."""

from __future__ import annotations

import asyncio
import json
import os
import time
import xml.etree.ElementTree as ET

import httpx
from core_helpers import FakeUpstream, login, make_opts, running_app

from fnav import paths
from fnav.camera.gpstrack import encode, gps_path, make_sample, read_track
from fnav.core.recgps import CARRY_S, RecGps, current_segment, sample_of
from fnav.core.recordings import Recordings
from fnav.models import CameraStatus, GpsFix

T0 = 1_792_507_200.0
A = "2026-10-20/2026-10-20_16-40-00.ts"
B = "2026-10-20/2026-10-20_16-50-00.ts"
R = "rear/2026-10-20/2026-10-20_16-40-03.ts"


def touch(root, rel, data=b"\x47" * 188, mtime=None):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def pt(t: float, speed: float = 10.0) -> dict:
    return make_sample(t, 52.2 + (t - T0) * 1e-4, 21.0, alt=100.0, speed=speed, course=90.0, fix="3d")


def test_sample_of_fix():
    f = GpsFix(fix="3d", lat=52.2, lon=21.0, alt_m=101.0, speed_mps=12.0, course_deg=45.0, age_s=0.4)
    assert sample_of(f, T0) == {"t": T0 - 0.4, "lat": 52.2, "lon": 21.0, "alt": 101.0, "speed": 12.0,
                                "course": 45.0, "fix": "3d"}
    assert sample_of(GpsFix(fix="none", lat=52.2, lon=21.0), T0) is None  # последняя известная позиция — не точка
    assert sample_of(f.model_copy(update={"age_s": 5.0}), T0) is None  # gpsd молчит
    assert sample_of(f.model_copy(update={"lat": None}), T0) is None


def test_writer_follows_the_current_segment(tmp_path):
    clock = {"t": T0}
    w = RecGps(lambda: tmp_path, lambda: GpsFix(), (), clock=lambda: clock["t"])
    touch(tmp_path, A)
    touch(tmp_path, R)
    w.write([A, R], pt(T0))
    clock["t"] = T0 + 1
    w.write([A, R], pt(T0 + 1))
    w.write([A, R], pt(T0 + 1))  # GPS не обновился — повтора нет
    w.write([A, R], None)  # нет фикса — ничего
    # смена сегмента передней: новый начинается с последней точки прошлого
    clock["t"] = T0 + 2
    w.write([B, R], pt(T0 + 2))  # B ещё не создан — файл трека не появляется
    assert not gps_path(tmp_path / B).exists()
    touch(tmp_path, B)
    clock["t"] = T0 + 3
    w.write([B, R], pt(T0 + 3))
    assert [p["t"] for p in read_track(tmp_path / A)] == [T0, T0 + 1]
    assert [p["t"] for p in read_track(tmp_path / B)] == [T0 + 2, T0 + 3]
    assert [p["t"] for p in read_track(tmp_path / R)] == [T0, T0 + 1, T0 + 2, T0 + 3]
    assert w._files.keys() == {B, R}  # A закрыт
    w.write([], None)
    assert w._files == {}
    # прошлая точка старше CARRY_S — в новый сегмент не переносится
    c = tmp_path / "2026-10-20/2026-10-20_17-00-00.ts"
    touch(tmp_path, "2026-10-20/2026-10-20_17-00-00.ts")
    clock["t"] = T0 + 3 + CARRY_S + 1
    w.write(["2026-10-20/2026-10-20_17-00-00.ts"], pt(clock["t"]))
    assert [p["t"] for p in read_track(c)] == [clock["t"]]
    w.write([], None)
    # путь не записи — не пишется (и не падает)
    w.write(["../x.ts", "2026-10-20/../../x.ts"], pt(T0 + 100))
    assert w._files == {}


async def test_tick_takes_segments_from_camera_status(data_dir):
    root = paths.recordings_dir()
    touch(root, A)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/status":
            return httpx.Response(200, json=CameraStatus(state="recording", recording=True,
                                                         current_segment=A).model_dump(mode="json"))
        return httpx.Response(404)

    cam = FakeUpstream(handler)
    async with running_app(make_opts(local_transport=cam.transport)) as c:
        core = c.app.state.core
        assert current_segment(core.camera) == A
        assert current_segment(core.camera_rear) is None  # задняя выключена
        fixes = iter([GpsFix(fix="3d", lat=52.2, lon=21.0, speed_mps=8.0, age_s=0.0),
                      GpsFix(fix="3d", lat=52.2001, lon=21.0, speed_mps=9.0, age_s=0.0)])
        core.recgps.get_fix = lambda: next(fixes)
        await core.recgps.tick()
        await asyncio.sleep(0.01)
        await core.recgps.tick()
        await core.recgps.stop()
        track = read_track(root / A)
        assert [p["speed"] for p in track] == [8.0, 9.0]


def test_listing_and_total_ignore_the_track(tmp_path):
    old = time.time() - 3600
    touch(tmp_path, A, b"\x47" * 1880, mtime=old)
    gps_path(tmp_path / A).write_bytes(encode(pt(T0)) * 500)
    rec = Recordings(tmp_path)
    assert [i["path"] for i in rec._scan(None)] == [A]
    assert rec.total_bytes() == 1880


async def test_gps_api_json_gpx_and_delete(data_dir):
    root = paths.recordings_dir()
    old = time.time() - 3600
    touch(root, A, mtime=old)
    touch(root, R, mtime=old)
    touch(root, B, mtime=old)
    gps_path(root / A).write_bytes(b"".join(encode(pt(T0 + i)) for i in range(5)) + b'{"t": 1792')
    gps_path(root / R).write_bytes(encode(pt(T0)))
    cam = FakeUpstream(lambda req: httpx.Response(200, json=CameraStatus(state="stopped").model_dump(mode="json"))
                       if req.url.path == "/status" else httpx.Response(404))
    async with running_app(make_opts(local_transport=cam.transport)) as c:
        assert (await c.get(f"/api/recordings/gps/{A}")).status_code == 401
        await login(c)
        r = await c.get(f"/api/recordings/gps/{A}")
        assert r.status_code == 200
        body = r.json()
        assert body["path"] == A and [p["t"] for p in body["samples"]] == [T0 + i for i in range(5)]
        assert body["samples"][0] == pt(T0)
        assert (await c.get(f"/api/recordings/gps/{B}")).json()["samples"] == []  # трека нет — пусто
        assert (await c.get("/api/recordings/gps/2026-10-20/2026-10-20_23-59-59.ts")).status_code == 404
        assert (await c.get("/api/recordings/gps/..%2F..%2Fsecrets.env")).status_code in (400, 404)
        assert (await c.get(f"/api/recordings/gps/{A}?format=kml")).status_code == 422

        r = await c.get(f"/api/recordings/gps/{A}?format=gpx")
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/gpx+xml")
        assert 'filename="2026-10-20_16-40-00.gpx"' in r.headers["content-disposition"]
        gpx = ET.fromstring(r.content)
        assert len(gpx.findall(".//{http://www.topografix.com/GPX/1/1}trkpt")) == 5
        r = await c.get(f"/api/recordings/gps/{R}?format=gpx")
        assert 'filename="rear-2026-10-20_16-40-03.gpx"' in r.headers["content-disposition"]

        # трек удаляется вместе с записью
        assert (await c.delete(f"/api/recordings/file/{A}")).status_code == 200
        assert not (root / A).exists() and not gps_path(root / A).exists()
        assert json.loads((await c.get("/api/recordings")).content)[0]["path"] in (B, R)
