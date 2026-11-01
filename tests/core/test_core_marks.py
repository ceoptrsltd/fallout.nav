"""Отметки движения без тревоги: журнал отметок (файл, пределы), клиент fnav-watch отдаёт "mark" не в тревогу,
метки в записях своей камеры, API страницы охраны."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import httpx
from core_helpers import login, running_app
from test_core_vision import FakeWatch

from fnav.core import marks as marks_mod
from fnav.core.marks import MarkLog
from fnav.core.recordings import attach_marks, recording_at
from fnav.core.vision import VisionClient
from fnav.models import VisionSettings


def _ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


async def test_marklog_validates_sorts_persists_and_prunes(tmp_path, monkeypatch):
    now = [_ts("2026-10-30T20:00:00")]
    path = tmp_path / "vision_marks.jsonl"
    log = MarkLog(path, clock=lambda: now[0])
    await log.start(background=False)
    assert log.items == []
    a = log.add({"what": "vehicle", "start": now[0] - 30, "dur_s": 12.34, "camera": "front"}, now[0])
    assert a == {"ts": now[0] - 30, "kind": "vehicle", "camera": "front", "dur_s": 12.3}
    log.add({"what": "motion", "start": now[0] - 60, "dur_s": 5, "camera": "rear"}, now[0])  # пришла позже — по ts
    assert log.add({"what": "approach", "start": now[0]}, now[0]) is None  # тревоги — не отметки
    assert log.add({"what": "motion", "camera": "side"}, now[0]) is None
    bad = log.add({"what": "person", "start": "x", "dur_s": 1e9}, now[0])  # кривое начало — время события
    assert bad["ts"] == now[0] and bad["dur_s"] == marks_mod.MAX_DUR_S and bad["camera"] == "front"
    assert [m["kind"] for m in log.items] == ["motion", "vehicle", "person"]
    assert [m["kind"] for m in log.between(now[0] - 40, now[0])] == ["vehicle", "person"]
    assert [m["kind"] for m in log.between(now[0] - 100, now[0], "rear")] == ["motion"]
    assert [m["kind"] for m in log.latest(2)] == ["person", "vehicle"]
    assert log.counts(now[0] - 45) == {"motion": 0, "vehicle": 1, "person": 1, "sound": 0}

    await log.flush()  # дописывает строки в конец
    assert len(path.read_text().splitlines()) == 3
    log.add({"what": "motion", "start": now[0]}, now[0])
    await log.stop()  # при остановке — тоже
    lines = path.read_text().splitlines()
    assert len(lines) == 4 and json.loads(lines[-1])["kind"] == "motion"
    path.write_text(path.read_text() + '{"ts": 17933')  # питание пропало посреди записи — строка оборвана
    again = MarkLog(path, clock=lambda: now[0])
    await again.start(background=False)
    assert again.items == log.items
    again.add({"what": "person", "start": now[0] - 1}, now[0])
    await again.flush()  # оборванный хвост: файл переписан начисто, новая отметка не склеилась с обрывком
    assert [json.loads(x)["ts"] for x in path.read_text().splitlines()] == [m["ts"] for m in again.items]
    del again.items[-2], again._ts[-2]  # как было: лишняя «person» только для проверки склейки

    now[0] += marks_mod.MARKS_MAX_AGE_S - 25  # первые две отметки старше 30 дней
    again.add({"what": "motion", "start": now[0]}, now[0])
    assert [m["kind"] for m in again.items] == ["person", "motion", "motion"]
    monkeypatch.setattr(marks_mod, "MARKS_MAX", 2)
    again.add({"what": "vehicle", "start": now[0]}, now[0])
    assert [m["kind"] for m in again.items] == ["motion", "vehicle"]
    await again.flush()  # устаревших строк мало — только дописать
    assert len(path.read_text().splitlines()) == 7
    monkeypatch.setattr(marks_mod, "COMPACT_AT", 3)
    await again.flush()  # набралось — переписать целиком
    assert [json.loads(x)["kind"] for x in path.read_text().splitlines()] == ["motion", "vehicle"]
    again.clear()
    await again.flush()
    assert path.read_text() == ""


async def test_marklog_moves_the_first_version_file(tmp_path):
    now = _ts("2026-10-30T20:00:00")
    legacy = tmp_path / "vision_marks.json"
    legacy.write_text(json.dumps([{"ts": now - 5, "kind": "vehicle", "camera": "front", "dur_s": 3.0}]))
    log = MarkLog(tmp_path / "vision_marks.jsonl", clock=lambda: now)
    await log.start(background=False)
    assert [m["kind"] for m in log.items] == ["vehicle"]
    await log.flush()
    assert not legacy.exists() and len((tmp_path / "vision_marks.jsonl").read_text().splitlines()) == 1


async def test_client_sends_marks_to_on_mark_not_to_the_alarm():
    alarms: list = []
    marks: list = []

    async def on_event(kind, data):
        alarms.append(kind)

    w = FakeWatch()
    w.watching = True
    c = VisionClient(get_settings=lambda: VisionSettings(enabled=True), get_armed=lambda: True, on_event=on_event,
                     transport=httpx.MockTransport(w), clock=lambda: 1000.0,
                     on_mark=lambda data, ts: marks.append((data["what"], ts)))
    await c.poll()
    w.add("mark", 1000.0, what="vehicle", start=990.0, dur_s=8.0, camera="front")
    w.add("loiter", 1000.0)
    await c.poll()
    assert alarms == ["loiter"] and marks == [("vehicle", 1000.0)]
    await c.stop()


def test_marks_go_to_recordings_of_their_camera():
    t0 = _ts("2026-10-30T19:59:41")
    items = [{"path": "2026-10-30/2026-10-30_19-59-41.ts", "start": "2026-10-30T19:59:41", "duration_s": 600.0},
             {"path": "rear/2026-10-30/2026-10-30_19-59-40.ts", "start": "2026-10-30T19:59:40", "duration_s": 600.0},
             {"path": "nosync/boot-1_0001.ts", "start": None, "duration_s": 60.0}]
    log = MarkLog(Path("unused.jsonl"), clock=lambda: t0 + 700)  # без start: только в памяти
    log.add({"what": "vehicle", "start": t0 + 125.0, "dur_s": 9.0, "camera": "front"}, t0 + 140)
    log.add({"what": "motion", "start": t0 + 300.0, "dur_s": 4.0, "camera": "rear"}, t0 + 320)
    attach_marks(items, log.between, now=t0 + 700)
    assert items[0]["marks"] == [{"kind": "vehicle", "ts": t0 + 125.0, "offset_s": 125.0, "dur_s": 9.0}]
    assert [m["offset_s"] for m in items[1]["marks"]] == [301.0] and items[2]["marks"] == []
    assert recording_at(items, t0 + 125.0, "front", now=t0 + 700) == {"path": items[0]["path"], "offset_s": 125.0}
    assert recording_at(items, t0 + 300.0, "rear", now=t0 + 700)["path"].startswith("rear/")
    assert recording_at(items, t0 + 5000.0, "front", now=t0 + 700) is None


async def test_marks_api_mock(data_dir):
    async with running_app() as c:
        assert (await c.get("/api/alarm/marks")).status_code == 401
        await login(c)
        st = (await c.get("/api/alarm/marks")).json()
        assert st == {"marks": [], "total": 0, "last_24h": {"motion": 0, "vehicle": 0, "person": 0, "sound": 0}}
        for kind in ("vehicle", "motion", "motion"):
            r = await c.post("/api/alarm/simulate/mark", json={"kind": kind, "dur_s": 5})
            assert r.status_code == 200 and r.json()["kind"] == kind
        assert (await c.post("/api/alarm/simulate/mark", json={"kind": "loiter"})).status_code == 422
        st = (await c.get("/api/alarm/marks", params={"limit": 2})).json()
        assert [m["kind"] for m in st["marks"]] == ["motion", "motion"] and st["total"] == 3
        assert st["last_24h"] == {"motion": 2, "vehicle": 1, "person": 0, "sound": 0}
        assert set(st["marks"][0]) == {"ts", "kind", "camera", "dur_s", "rec"}
        assert (await c.get("/api/alarm")).json()["events"] == []  # отметки — не журнал охраны
        # громкий звук (у mock — гудок): отметка с клипом, клип отдаётся, имя проверяется
        m = (await c.post("/api/alarm/simulate/mark", json={"kind": "sound", "camera": "rear", "dur_s": 1.5})).json()
        assert m["kind"] == "sound" and m["camera"] == "rear" and m["peak_db"] < 0
        assert abs(m["clip_start"] - (m["ts"] - 5)) < 0.01 and m["clip_dur_s"] == 9.5
        r = await c.get(f"/api/alarm/sound/{m['clip']}")
        assert r.status_code == 200 and r.headers["content-type"] == "audio/wav" and r.content[:4] == b"RIFF"
        assert (await c.get("/api/alarm/sound/..%2Fsettings.json")).status_code in (400, 404)
        assert (await c.get("/api/alarm/sound/20991231-235959-000.wav")).status_code == 404
        assert (await c.get("/api/alarm/marks")).json()["last_24h"]["sound"] == 1
        st = (await c.delete("/api/alarm/marks")).json()
        assert st["total"] == 0 and (await c.get("/api/alarm/marks")).json()["marks"] == []
        assert (await c.get(f"/api/alarm/sound/{m['clip']}")).status_code == 404  # клипы — вместе с отметками


async def test_sound_marks_keep_their_clip_and_reach_the_rear_recording(tmp_path):
    t0 = _ts("2026-11-01T19:28:28")
    log = MarkLog(tmp_path / "vision_marks.jsonl", clock=lambda: t0 + 700)
    await log.start(background=False)
    m = log.add({"what": "sound", "camera": "rear", "start": t0 + 46.0, "dur_s": 1.1, "peak_db": -14.44,
                 "clip": "20261101-192914-000.wav", "clip_start": t0 + 41.0, "clip_dur_s": 9.1}, t0 + 52)
    assert m == {"ts": t0 + 46.0, "kind": "sound", "camera": "rear", "dur_s": 1.1, "peak_db": -14.4,
                 "clip": "20261101-192914-000.wav", "clip_start": t0 + 41.0, "clip_dur_s": 9.1}
    bad = log.add({"what": "sound", "camera": "rear", "start": t0 + 60, "clip": "../../etc/passwd",
                   "clip_start": t0 + 55}, t0 + 70)
    assert "clip" not in bad  # имя клипа не по образцу — отметка без клипа
    await log.flush()
    again = MarkLog(tmp_path / "vision_marks.jsonl", clock=lambda: t0 + 700)
    await again.start(background=False)
    assert again.items[0] == m  # клип пережил перезапуск
    items = [{"path": "rear/2026-11-01/2026-11-01_19-28-28.ts", "start": "2026-11-01T19:28:28", "duration_s": 600.0},
             {"path": "2026-11-01/2026-11-01_19-21-01.ts", "start": "2026-11-01T19:21:01", "duration_s": 600.0}]
    attach_marks(items, again.between, now=t0 + 700)
    snd = items[0]["marks"][0]
    assert snd["clip"] == "20261101-192914-000.wav" and snd["clip_offset_s"] == 41.0 and snd["clip_dur_s"] == 9.1
    assert snd["offset_s"] == 46.0 and snd["peak_db"] == -14.4
    assert items[1]["marks"] == []  # отметки задней — только на её записях
