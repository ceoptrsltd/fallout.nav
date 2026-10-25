#!/usr/bin/env python3
"""Build the speed-limit database for the car screen from OpenStreetMap (fnav/core/speedlimit.py reads it).

Input: roads exported by osmium as GeoJSON sequence (see tools/speedlimits.sh):
    osmium tags-filter country.osm.pbf w/highway=<car roads> -o roads.osm.pbf
    osmium export roads.osm.pbf -c export.json -f geojsonseq -x print_record_separator=false -o roads.geojsonseq
    python3 build_db.py roads.geojsonseq speedlimits.sqlite --region PL

Output (SQLite, ~1 m precision):
    way(id, fwd, bwd, cls, oneway, src, pts)  fwd/bwd: km/h along / against the way's drawing direction (NULL: unknown)
                                              src: 1 maxspeed tag, 2 zone (PL:urban…), 3 default for the road class
                                              pts: delta + zigzag varint encoded 1e-5° lat/lon pairs
    cell(c, w)                                grid of CELL_DEG degrees -> ways that cross it
    meta(k, v)

Standard library only: runs on the server next to osmium. Data © OpenStreetMap contributors, ODbL.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import re
import sqlite3
import sys
import time

CELL_DEG = 0.005
Q = 1e5  # 1e-5 degree ≈ 1 m

# road classes the screen knows; the index goes into the database
CLASSES = ["motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "secondary",
           "secondary_link", "tertiary", "tertiary_link", "unclassified", "residential", "living_street"]

# implicit limits (maxspeed=PL:urban, maxspeed:type=PL:rural, …); a passenger car / van up to 3.5 t
ZONES = {
    "PL": {"urban": 50, "rural": 90, "motorway": 140, "expressway": 120, "living_street": 20, "zone30": 30,
           "zone20": 20, "zone10": 10},
}
# no maxspeed and no zone: only where the class alone says it
DEFAULTS = {"PL": {"motorway": 140, "residential": 50, "living_street": 20}}

NUM = re.compile(r"^\s*(\d{1,3})\s*(km/h|kmh|kph|mph)?\s*$", re.IGNORECASE)


def parse_speed(v: str | None, region: str) -> tuple[int | None, int]:
    """'50' / '30 mph' / 'PL:urban' -> (km/h, src); (None, 0) when unknown or 'none' / 'signals' / 'walk'."""
    if not v:
        return None, 0
    v = v.split(";")[0].strip()
    m = NUM.match(v)
    if m:
        n = int(m.group(1))
        if (m.group(2) or "").lower() == "mph":
            n = round(n * 1.609344)
        return (n, 1) if 5 <= n <= 160 else (None, 0)
    if ":" in v:
        cc, zone = v.split(":", 1)
        z = ZONES.get(cc.upper(), {}).get(zone.strip().lower())
        if z:
            return z, 2
    return None, 0


def way_limits(tags: dict, region: str) -> tuple[int | None, int | None, int]:
    """(forward km/h, backward km/h, src) for a way."""
    both, src = parse_speed(tags.get("maxspeed"), region)
    if both is None:
        for k in ("maxspeed:type", "source:maxspeed", "zone:maxspeed"):
            both, src = parse_speed(tags.get(k), region)
            if both is not None:
                break
    fwd, sf = parse_speed(tags.get("maxspeed:forward"), region)
    bwd, sb = parse_speed(tags.get("maxspeed:backward"), region)
    fwd, bwd = fwd if fwd is not None else both, bwd if bwd is not None else both
    src = max(src, sf, sb) if (sf or sb) else src
    if fwd is None and bwd is None:
        d = DEFAULTS.get(region, {}).get(tags.get("highway", ""))
        if d:
            return d, d, 3
    return fwd, bwd, src


def oneway(tags: dict) -> int:
    o = (tags.get("oneway") or "").lower()
    if o in ("yes", "true", "1"):
        return 1
    if o in ("-1", "reverse"):
        return -1
    if o == "no":
        return 0
    if tags.get("junction") in ("roundabout", "circular") or tags.get("highway") in ("motorway", "motorway_link"):
        return 1
    return 0


def zigzag(n: int) -> int:
    return (n << 1) ^ (n >> 63)


def varint(n: int, out: bytearray) -> None:
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return


def encode_pts(coords: list[tuple[int, int]]) -> bytes:
    out = bytearray()
    plat = plon = 0
    for lat, lon in coords:
        varint(zigzag(lat - plat), out)
        varint(zigzag(lon - plon), out)
        plat, plon = lat, lon
    return bytes(out)


def cell_key(cy: int, cx: int) -> int:
    return (cy + 20000) * 100000 + (cx + 40000)


def cells_of(coords: list[tuple[int, int]]) -> set[int]:
    """Grid cells the polyline passes (segment bounding boxes; segments are short, so this is tight enough)."""
    step = CELL_DEG * Q
    out: set[int] = set()
    for (la1, lo1), (la2, lo2) in itertools.pairwise(coords):
        y0, y1 = sorted((math.floor(la1 / step), math.floor(la2 / step)))
        x0, x1 = sorted((math.floor(lo1 / step), math.floor(lo2 / step)))
        for cy in range(y0, y1 + 1):
            for cx in range(x0, x1 + 1):
                out.add(cell_key(cy, cx))
    return out


def build(src, dst: str, region: str) -> dict:
    db = sqlite3.connect(dst)
    db.executescript("""
        PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;
        DROP TABLE IF EXISTS way; DROP TABLE IF EXISTS cell; DROP TABLE IF EXISTS meta;
        CREATE TABLE way(id INTEGER PRIMARY KEY, fwd INTEGER, bwd INTEGER, cls INTEGER, oneway INTEGER,
                         src INTEGER, pts BLOB);
        CREATE TABLE cell(c INTEGER, w INTEGER, PRIMARY KEY (c, w)) WITHOUT ROWID;
        CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
    """)
    stats = {"ways": 0, "tagged": 0, "zone": 0, "default": 0, "unknown": 0, "skipped": 0}
    ways, cells = [], []
    for line in src:
        line = line.strip().lstrip("\x1e")
        if not line:
            continue
        f = json.loads(line)
        g, tags = f.get("geometry") or {}, f.get("properties") or {}
        cls = tags.get("highway")
        if g.get("type") != "LineString" or cls not in CLASSES or tags.get("area") == "yes":
            stats["skipped"] += 1
            continue
        coords = [(round(lat * Q), round(lon * Q)) for lon, lat in g["coordinates"]]
        if len(coords) < 2:
            stats["skipped"] += 1
            continue
        wid = int(str(tags.get("@id", f.get("id", stats["ways"] + 1))).lstrip("w"))
        fwd, bwd, s = way_limits(tags, region)
        stats[{1: "tagged", 2: "zone", 3: "default"}.get(s, "unknown")] += 1
        stats["ways"] += 1
        ways.append((wid, fwd, bwd, CLASSES.index(cls), oneway(tags), s, encode_pts(coords)))
        cells.extend((c, wid) for c in cells_of(coords))
        if len(ways) >= 20000:
            db.executemany("INSERT OR REPLACE INTO way VALUES (?,?,?,?,?,?,?)", ways)
            db.executemany("INSERT OR IGNORE INTO cell VALUES (?,?)", cells)
            ways.clear()
            cells.clear()
    db.executemany("INSERT OR REPLACE INTO way VALUES (?,?,?,?,?,?,?)", ways)
    db.executemany("INSERT OR IGNORE INTO cell VALUES (?,?)", cells)
    meta = {"region": region, "built": time.strftime("%Y-%m-%d"), "cell_deg": str(CELL_DEG), "q": str(int(Q)),
            "classes": ",".join(CLASSES), "source": "OpenStreetMap (ODbL)", **{k: str(v) for k, v in stats.items()}}
    db.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
    db.commit()
    db.execute("VACUUM")
    db.close()
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("roads", help="osmium export GeoJSON sequence ('-' for stdin)")
    ap.add_argument("out", help="SQLite file to write")
    ap.add_argument("--region", default="PL", help="country code for implicit limits (PL:urban …)")
    a = ap.parse_args()
    if a.roads == "-":
        stats = build(sys.stdin, a.out, a.region.upper())
    else:
        with open(a.roads, encoding="utf-8") as src:
            stats = build(src, a.out, a.region.upper())
    print(json.dumps(stats))


if __name__ == "__main__":
    main()
