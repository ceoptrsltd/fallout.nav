"""Разрешённая скорость: сборка базы из выгрузки OSM (tools/speedlimits/build_db.py) и поиск дороги под колёсами."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

from fnav.core import speedlimit as sl
from fnav.models import GpsFix

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("build_db", ROOT / "tools" / "speedlimits" / "build_db.py")
build_db = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_db)

LAT = 52.0
DLAT_15M = 15 / 110_540  # 15 m north


def feat(wid: int, coords: list[tuple[float, float]], **tags) -> str:
    return json.dumps({"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[lo, la] for la, lo in coords]},
                       "properties": {"@id": wid, **tags}})


@pytest.fixture
def db(tmp_path) -> Path:
    east = [(LAT, 21.000), (LAT, 21.005), (LAT, 21.010)]
    north15 = [(LAT + DLAT_15M, 21.000), (LAT + DLAT_15M, 21.010)]
    lines = [
        feat(1, east, highway="residential", maxspeed="30"),
        feat(2, north15, highway="primary", maxspeed="70", oneway="yes"),  # drawn eastwards
        feat(3, [(52.01, 21.0), (52.02, 21.0)], highway="secondary", **{"maxspeed:forward": "60",
                                                                          "maxspeed:backward": "40"}),
        feat(4, [(52.03, 21.0), (52.03, 21.01)], highway="tertiary", maxspeed="PL:urban"),
        feat(5, [(52.04, 21.0), (52.04, 21.01)], highway="residential"),  # no tags: PL default 50
        feat(6, [(52.05, 21.0), (52.05, 21.01)], highway="tertiary"),  # no tags: unknown
        feat(7, [(52.06, 21.0), (52.06, 21.01)], highway="footway"),  # not a car road
        feat(8, [(52.07, 21.0), (52.07, 21.01)], highway="primary", maxspeed="35 mph"),
    ]
    path = tmp_path / "speedlimits.sqlite"
    stats = build_db.build(io.StringIO("\n".join(lines)), str(path), "PL")
    assert stats == {"ways": 7, "tagged": 4, "zone": 1, "default": 1, "unknown": 1, "skipped": 1}
    return path


def svc_for(path: Path) -> sl.SpeedLimitService:
    s = sl.SpeedLimitService(path, lambda: GpsFix(), clock=lambda: 100.0)
    assert s.db.ensure_open()
    return s


def at(s: sl.SpeedLimitService, lat: float, lon: float, course: float | None = None, speed: float = 15.0):
    r = s.update(GpsFix(fix="3d", lat=lat, lon=lon, course_deg=course, speed_mps=speed))
    return (r.kmh, r.src) if r else None


def test_codec_roundtrip() -> None:
    pts = [(5_200_000, 2_100_000), (5_200_013, 2_099_990), (5_199_001, 2_100_500)]
    assert sl.decode_pts(build_db.encode_pts(pts)) == [(la / 1e5, lo / 1e5) for la, lo in pts]


def test_parse_speed() -> None:
    p = build_db.parse_speed
    assert p("50", "PL") == (50, 1) and p("30 mph", "PL") == (48, 1) and p("PL:rural", "PL") == (90, 2)
    assert p("none", "PL") == (None, 0) and p("signals", "PL") == (None, 0) and p("50;70", "PL") == (50, 1)


def test_match_roads(db: Path) -> None:
    s = svc_for(db)
    assert at(s, LAT + 2 / 110_540, 21.003, course=90) == (30, "osm")  # 2 m off the residential street
    s2 = svc_for(db)
    assert at(s2, LAT + DLAT_15M, 21.003, course=90) == (70, "osm")  # on the primary road
    assert at(svc_for(db), 52.03, 21.004, course=270) == (50, "zone")
    assert at(svc_for(db), 52.04, 21.004, course=90) == (50, "default")
    assert at(svc_for(db), 52.05, 21.004, course=90) is None  # tertiary without tags: no sign
    assert at(svc_for(db), 52.06, 21.004, course=90) is None  # a footway is not in the base
    assert at(svc_for(db), 52.07, 21.004, course=90) == (56, "osm")
    assert at(svc_for(db), 52.5, 21.5) is None  # nothing around


def test_direction_specific_limits_and_oneway(db: Path) -> None:
    assert at(svc_for(db), 52.015, 21.0, course=0) == (60, "osm")  # along the drawing direction (north)
    assert at(svc_for(db), 52.015, 21.0, course=180) == (40, "osm")  # against it
    # halfway between the two parallel roads, driving west: the primary is one-way east -> the residential street
    s = svc_for(db)
    assert at(s, LAT + DLAT_15M / 2, 21.003, course=270) == (30, "osm")


def test_sticks_to_the_current_road(db: Path) -> None:
    s = svc_for(db)
    assert at(s, LAT + 1 / 110_540, 21.003, course=90) == (30, "osm")
    # drifting towards the parallel road: GPS noise, still the same road until the other one is clearly closer
    assert at(s, LAT + 9 / 110_540, 21.004, course=90) == (30, "osm")
    assert at(s, LAT + 14 / 110_540, 21.005, course=90) == (70, "osm")


def test_no_fix_holds_then_clears(db: Path) -> None:
    t = {"now": 100.0}
    s = sl.SpeedLimitService(db, lambda: GpsFix(), clock=lambda: t["now"])
    s.db.ensure_open()
    assert s.update(GpsFix(fix="3d", lat=LAT, lon=21.003, course_deg=90, speed_mps=10)).kmh == 30
    t["now"] += 5
    assert s.update(GpsFix()).kmh == 30  # a short GPS gap (tunnel): keep the sign
    t["now"] += 10
    assert s.update(GpsFix()) is None
