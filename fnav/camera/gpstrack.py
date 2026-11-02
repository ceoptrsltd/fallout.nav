"""Трек GPS записи: боковой файл ``<запись>.ts.gps`` рядом с сегментом (общий для E и A, как tsindex.py).

Пишет fnav-core (fnav/core/recgps.py) — примерно раз в секунду строку JSON на точку GPS, пока сегмент текущий:

    {"t": 1792507200.12, "lat": 52.22977, "lon": 21.01178, "alt": 112.4, "speed": 13.42, "course": 135.2, "fix": "3d"}

``t`` — время точки (unix, UTC: плеер сверяет его с началом записи ``start_ts``), ``speed`` — м/с, ``course`` — °,
``alt`` — м (нет — null). Точки без фикса не пишутся. Удаляется вместе с записью (очистка и восстановление
fnav-camera — storage.remove_sidecar, ``DELETE`` fnav-core). Оборванная последняя строка (питание пропало при записи)
при чтении пропускается. Контракт — docs/contracts/camera.md «Трек GPS записи».
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

GPS_SUFFIX = ".gps"
READ_MAX_BYTES = 4 * 1024 * 1024  # час записи — ~350 КБ; больше не читаем
GPX_GAP_S = 10.0  # точек не было дольше — в GPX новый кусок трека (trkseg)
FIXES = ("2d", "3d")


def gps_path(ts: Path) -> Path:
    return ts.with_name(ts.name + GPS_SUFFIX)


def _num(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def make_sample(t: float, lat: object, lon: object, *, alt: object = None, speed: object = None,
                course: object = None, fix: object = "3d") -> dict | None:
    """Точка трека (округлённая: ~10 см, 0,01 м/с, 0,1°) или None — без времени, координат или фикса."""
    t_, la, lo = _num(t), _num(lat), _num(lon)
    if t_ is None or t_ <= 0 or la is None or lo is None or not (-90 <= la <= 90 and -180 <= lo <= 180):
        return None
    if fix not in FIXES:
        return None
    a, s, c = _num(alt), _num(speed), _num(course)
    return {"t": round(t_, 2), "lat": round(la, 6), "lon": round(lo, 6),
            "alt": round(a, 1) if a is not None else None,
            "speed": round(max(0.0, s), 2) if s is not None else None,
            "course": round(c % 360.0, 1) if c is not None else None, "fix": fix}


def clean(raw: object) -> dict | None:
    """Строка файла (уже разобранный JSON) → точка или None (не наша / битая)."""
    if not isinstance(raw, dict):
        return None
    return make_sample(raw.get("t"), raw.get("lat"), raw.get("lon"), alt=raw.get("alt"), speed=raw.get("speed"),
                       course=raw.get("course"), fix=raw.get("fix"))


def encode(sample: dict) -> bytes:
    return (json.dumps(sample, separators=(",", ":")) + "\n").encode()


def read_track(ts: Path) -> list[dict]:
    """Точки трека записи по времени (повторы времени — одна); файла нет — пусто."""
    try:
        with open(gps_path(ts), "rb") as f:
            data = f.read(READ_MAX_BYTES)
    except OSError:
        return []
    out: dict[float, dict] = {}
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        try:
            s = clean(json.loads(line))
        except ValueError:  # оборванная строка
            continue
        if s is not None:
            out[s["t"]] = s
    return [out[k] for k in sorted(out)]


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def to_gpx(samples: list[dict], name: str) -> str:
    """GPX 1.1: трек с высотой и временем; скорость (м/с) и курс — в Garmin TrackPointExtension v2. Перерыв в точках
    дольше GPX_GAP_S — новый trkseg."""
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           ('<gpx version="1.1" creator="fallout.nav" xmlns="http://www.topografix.com/GPX/1/1"'
            ' xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v2">'),
           f"<trk><name>{escape(name)}</name>"]
    prev: float | None = None
    for s in samples:
        if prev is None or s["t"] - prev > GPX_GAP_S:
            out.append(("</trkseg>" if prev is not None else "") + "<trkseg>")
        prev = s["t"]
        pt = f'<trkpt lat={quoteattr(format(s["lat"], ".6f"))} lon={quoteattr(format(s["lon"], ".6f"))}>'
        if s.get("alt") is not None:
            pt += f"<ele>{s['alt']}</ele>"
        pt += f"<time>{_iso(s['t'])}</time>"
        ext = ""
        if s.get("speed") is not None:
            ext += f"<gpxtpx:speed>{s['speed']}</gpxtpx:speed>"
        if s.get("course") is not None:
            ext += f"<gpxtpx:course>{s['course']}</gpxtpx:course>"
        if ext:
            pt += f"<extensions><gpxtpx:TrackPointExtension>{ext}</gpxtpx:TrackPointExtension></extensions>"
        out.append(pt + "</trkpt>")
    if prev is not None:
        out.append("</trkseg>")
    out.append("</trk></gpx>")
    return "\n".join(out) + "\n"
