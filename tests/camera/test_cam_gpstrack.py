"""Трек GPS записи (<запись>.ts.gps, fnav/camera/gpstrack.py): формат, GPX, удаление вместе с записью."""

from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

from fnav.camera import gpstrack as gt
from fnav.camera.storage import cleanup, list_recordings, remove_sidecar, repair_latest, stats
from fnav.camera.tsindex import sidecar_path

T0 = 1_792_507_200.0
GPX = "{http://www.topografix.com/GPX/1/1}"
TPX = "{http://www.garmin.com/xmlschemas/TrackPointExtension/v2}"


def test_make_sample_validates_and_rounds():
    s = gt.make_sample(T0 + 0.123456, 52.2297701234, 21.0117809876, alt=112.44, speed=13.4249, course=-45.0,
                       fix="3d")
    assert s == {"t": T0 + 0.12, "lat": 52.22977, "lon": 21.011781, "alt": 112.4, "speed": 13.42, "course": 315.0,
                 "fix": "3d"}
    assert gt.make_sample(T0, 52.0, 21.0, fix="2d")["speed"] is None  # нет скорости — null, точка остаётся
    for bad in ({"fix": "none"}, {"lat": None}, {"lat": 91.0}, {"lon": float("nan")}, {"t": 0},
                {"lat": True}):
        kw = {"t": T0, "lat": 52.0, "lon": 21.0, "fix": "3d", **bad}
        assert gt.make_sample(kw.pop("t"), kw.pop("lat"), kw.pop("lon"), **kw) is None, bad


def test_read_track_skips_a_torn_line_and_sorts(tmp_path: Path):
    ts = tmp_path / "2026-10-20" / "2026-10-20_16-40-00.ts"
    ts.parent.mkdir()
    ts.write_bytes(b"\x47" * 188)
    pts = [gt.make_sample(T0 + i, 52.0 + i * 1e-4, 21.0, speed=10.0, course=90.0) for i in (2, 0, 1, 1)]
    data = b"".join(gt.encode(p) for p in pts) + b"not json\n" + b'{"t": 1, "lat": 0}\n' + b'{"t": 17925'
    gt.gps_path(ts).write_bytes(data)
    assert gt.gps_path(ts).name == "2026-10-20_16-40-00.ts.gps"
    track = gt.read_track(ts)
    assert [p["t"] for p in track] == [T0, T0 + 1, T0 + 2]  # по времени, повтор — один раз, мусор и обрывок — мимо
    assert gt.read_track(tmp_path / "none.ts") == []


def test_gpx_has_time_elevation_speed_and_splits_on_gaps():
    pts = [gt.make_sample(T0 + t, 52.0 + t * 1e-4, 21.0, alt=100.0, speed=12.5, course=180.0) for t in (0, 1, 2, 30)]
    pts.append(gt.make_sample(T0 + 31, 52.1, 21.1, fix="2d"))
    root = ET.fromstring(gt.to_gpx(pts, "2026-10-20/a&b.ts"))
    assert root.tag == GPX + "gpx" and root.get("version") == "1.1"
    trk = root.find(GPX + "trk")
    assert trk.find(GPX + "name").text == "2026-10-20/a&b.ts"
    segs = trk.findall(GPX + "trkseg")
    assert [len(s.findall(GPX + "trkpt")) for s in segs] == [3, 2]  # перерыв 28 с — новый кусок
    p0 = segs[0].find(GPX + "trkpt")
    assert (p0.get("lat"), p0.get("lon")) == ("52.000000", "21.000000")
    assert p0.find(GPX + "time").text == "2026-10-20T14:40:00.000Z"
    assert p0.find(GPX + "ele").text == "100.0"
    ext = p0.find(f"{GPX}extensions/{TPX}TrackPointExtension")
    assert ext.find(TPX + "speed").text == "12.5" and ext.find(TPX + "course").text == "180.0"
    last = segs[1].findall(GPX + "trkpt")[-1]
    assert last.find(GPX + "ele") is None and last.find(GPX + "extensions") is None
    empty = ET.fromstring(gt.to_gpx([], "x"))
    assert empty.find(GPX + "trk").findall(GPX + "trkseg") == []


def _rec(root: Path, rel: str, size: int, mtime: float) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x47" * size)
    gt.gps_path(p).write_bytes(gt.encode(gt.make_sample(T0, 52.0, 21.0)) * 1000)  # трек тяжелее записи
    os.utime(p, (mtime, mtime))
    return p


def test_track_does_not_count_and_goes_with_the_recording(tmp_path: Path):
    old = _rec(tmp_path, "2026-10-19/2026-10-19_10-00-00.ts", 188 * 10, 1_700_000_000)
    new = _rec(tmp_path, "2026-10-20/2026-10-20_10-00-00.ts", 188 * 10, 1_700_000_600)
    assert list_recordings(tmp_path) == [old, new]  # .ts.gps — не запись
    assert stats(tmp_path) == (2, 188 * 20)  # и не её объём
    res = cleanup(tmp_path, target_free_bytes=10, free_fn=lambda _p: 0 if old.exists() else 100)
    assert res.deleted == [old]
    assert not gt.gps_path(old).exists() and not old.parent.exists()  # каталог дня пуст — удалён
    assert gt.gps_path(new).exists()
    sidecar_path(new).write_bytes(b"fnav-tsindex 2\n")
    remove_sidecar(new)
    assert not sidecar_path(new).exists() and not gt.gps_path(new).exists()
    remove_sidecar(new)  # нечего удалять — без ошибки


def test_repair_drops_the_track_of_an_empty_recording(tmp_path: Path):
    p = _rec(tmp_path, "a/b.ts", 0, 1_700_000_000)
    repair_latest(tmp_path)
    assert not p.exists() and not gt.gps_path(p).exists()


def test_encode_is_one_json_line():
    line = gt.encode(gt.make_sample(T0, 52.0, 21.0, speed=1.0))
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    assert json.loads(line)["speed"] == 1.0
