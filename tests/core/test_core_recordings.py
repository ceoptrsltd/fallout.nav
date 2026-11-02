import os
import time

import httpx
import pytest
from core_helpers import FakeUpstream, login, make_opts, running_app

from fnav import paths
from fnav.camera.tsindex import SidecarWriter, sidecar_path
from fnav.camera.tsmux import PTS_START, TsMuxer
from fnav.core.mockcam import _demo_ts
from fnav.core.recordings import RecordingError, hls_playlist, parse_range, safe_rel_path, ts_duration
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
            "path": "2026-10-20/2026-10-20_16-40-00.ts", "camera": "front", "date": "2026-10-20",
            "start": "2026-10-20T16:40:00", "start_ts": None,  # без .ts.idx истинное начало не известно
            "size_bytes": 10240, "duration_s": None, "protected": False, "in_progress": False, "audio": False, "events": [],
            "marks": []}
        assert by["nosync/boot-abcd1234_0001.ts"]["start"] is None

        # одна запись (кнопка Play у события на странице охраны) — то же, что в списке
        for rel in ("2026-10-20/2026-10-20_16-40-00.ts", "2026-10-19/2026-10-19_10-00-00.ts", CURRENT):
            r = await c.get(f"/api/recordings/item/{rel}")
            assert r.status_code == 200 and r.json() == by[rel], rel
        assert (await c.get("/api/recordings/item/2026-10-20/2026-10-20_23-59-59.ts")).status_code == 404
        assert (await c.get("/api/recordings/item/..%2F..%2Fetc%2Fpasswd")).status_code in (400, 404)

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


SPS_IDR = b"\x00\x00\x00\x01\x67\x42\x00\x1e\x00\x00\x00\x01\x65"
SLICE = b"\x00\x00\x00\x01\x41"


def write_rec(path, seconds: int, *, sidecar: bool, end: bool = True) -> list[int]:
    """Запись как у регистратора: 15 кадров/с, ключевой раз в секунду, PAT/PMT перед ним (+ .ts.idx).
    Возвращает смещения ключевых кадров."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mux = TsMuxer()
    side = SidecarWriter(path) if sidecar else None
    keys = []
    with open(path, "wb") as f:
        f.write(mux.tables())
        for i in range(seconds * 15):
            key = i % 15 == 0
            pts = PTS_START + i * 6000
            if key:
                keys.append(f.tell())
                if side:
                    side.add(f.tell(), pts)
            au = (SPS_IDR if key else SLICE) + bytes([i & 0xFF]) * (3000 if key else 700)
            f.write(mux.mux(au, pts, keyframe=key))
        if end:
            f.write(mux.end_marker())
    if side:
        side.close()
    return keys


def parse_m3u8(text: str) -> tuple[dict, list[tuple[float, int, int, str]]]:
    """→ (теги, [(длительность, смещение, длина, адрес)])."""
    tags, parts, dur, rng = {}, [], 0.0, (0, 0)
    for line in text.splitlines():
        if line.startswith("#EXTINF:"):
            dur = float(line[8:].rstrip(","))
        elif line.startswith("#EXT-X-BYTERANGE:"):
            n, o = line[17:].split("@")
            rng = (int(n), int(o))
        elif line.startswith("#"):
            k, _, v = line.partition(":")
            tags[k] = v
        elif line:
            parts.append((dur, rng[1], rng[0], line))
    return tags, parts


def test_hls_playlist_text():
    text = hls_playlist([(0, 1128, 3.0), (1128, 564, 2.5)], "../../file/d/a.ts", live=False)
    assert text.splitlines() == [
        "#EXTM3U", "#EXT-X-VERSION:4", "#EXT-X-TARGETDURATION:3", "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:EVENT", "#EXT-X-INDEPENDENT-SEGMENTS", "#EXT-X-START:TIME-OFFSET=0",
        "#EXTINF:3.000,", "#EXT-X-BYTERANGE:1128@0", "../../file/d/a.ts",
        "#EXTINF:2.500,", "#EXT-X-BYTERANGE:564@1128", "../../file/d/a.ts",
        "#EXT-X-ENDLIST",
    ]
    assert text.endswith("#EXT-X-ENDLIST\n")
    live = hls_playlist([(0, 1128, 3.4)], "x.ts", live=True).splitlines()
    assert "#EXT-X-ENDLIST" not in live and "#EXT-X-TARGETDURATION:4" in live
    assert "#EXT-X-TARGETDURATION:1" in hls_playlist([], "x.ts", live=True).splitlines()


async def test_recordings_hls(data_dir):
    root = paths.recordings_dir()
    closed = "2026-10-20/2026-10-20_16-40-00.ts"
    old = "2026-10-19/2026-10-19_10-00-00.ts"  # записан до появления .ts.idx
    keys = write_rec(root / closed, 20, sidecar=True)
    write_rec(root / old, 7, sidecar=False)
    live_keys = write_rec(root / CURRENT, 10, sidecar=True, end=False)
    t_old = time.time() - 3600
    for rel in (closed, old, CURRENT):
        os.utime(root / rel, (t_old, t_old))
    cam = FakeUpstream(camera_handler)
    async with running_app(make_opts(local_transport=cam.transport)) as c:
        url = f"/api/recordings/hls/{closed}.m3u8"
        assert (await c.get(url)).status_code == 401
        await login(c)
        r = await c.get(url)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "application/vnd.apple.mpegurl"
        assert r.headers["cache-control"] == "no-cache"
        tags, parts = parse_m3u8(r.text)
        assert tags["#EXT-X-VERSION"] == "4" and tags["#EXT-X-PLAYLIST-TYPE"] == "EVENT"
        assert "#EXT-X-ENDLIST" in tags and tags["#EXT-X-TARGETDURATION"] == "3"
        data = (root / closed).read_bytes()
        assert [d for d, *_ in parts] == [3.0] * 6 + [2.0]
        assert [o for _, o, _, _ in parts] == [0, *keys[3:20:3]]
        assert parts[-1][1] + parts[-1][2] == len(data)
        # относительный адрес куска → файл записи (и под префиксом удалённого доступа тоже)
        uri = parts[0][3]
        assert uri == "../../file/2026-10-20/2026-10-20_16-40-00.ts"
        relayed = httpx.URL(f"https://example.org/fnadmin/api/recordings/hls/{closed}.m3u8").join(uri)
        assert str(relayed) == f"https://example.org/fnadmin/api/recordings/file/{closed}"
        file_url = str(httpx.URL(f"http://testserver{url}").join(uri))
        for _, off, n, _ in [*parts[:3], parts[-1]]:
            rr = await c.get(file_url, headers={"Range": f"bytes={off}-{off + n - 1}"})
            assert rr.status_code == 206 and rr.content == data[off:off + n]
            assert rr.content[:3] == b"\x47\x40\x00"  # каждый кусок начинается с PAT

        # старая запись без .ts.idx — индекс поиском по файлу
        tags, parts = parse_m3u8((await c.get(f"/api/recordings/hls/{old}.m3u8")).text)
        assert [d for d, *_ in parts] == [3.0, 3.0, 1.0] and "#EXT-X-ENDLIST" in tags

        # идёт запись: без ENDLIST, только законченные куски; файл вырос — плейлист дописан
        cur_url = f"/api/recordings/hls/{CURRENT}.m3u8"
        tags, parts = parse_m3u8((await c.get(cur_url)).text)
        assert "#EXT-X-ENDLIST" not in tags
        assert [(d, o) for d, o, _, _ in parts] == [(3.0, 0), (3.0, live_keys[3]), (3.0, live_keys[6])]
        write_rec(root / CURRENT, 13, sidecar=True, end=False)
        _, parts2 = parse_m3u8((await c.get(cur_url)).text)
        assert parts2[:3] == parts and len(parts2) == 4

        # ошибки
        assert (await c.get(f"/api/recordings/hls/{closed}")).status_code == 404
        assert (await c.get("/api/recordings/hls/2026-10-20/none.ts.m3u8")).status_code == 404
        assert (await c.get("/api/recordings/hls/..%2Fsecret.ts.m3u8")).status_code in (400, 404)
        (root / "2026-10-20/2026-10-20_17-00-00.ts").write_bytes(bytes(1000))  # не MPEG-TS
        r = await c.get("/api/recordings/hls/2026-10-20/2026-10-20_17-00-00.ts.m3u8")
        assert r.status_code == 200 and "#EXTINF" not in r.text

        # удаление записи убирает и её индекс
        assert sidecar_path(root / closed).exists()
        assert (await c.delete(f"/api/recordings/file/{closed}")).status_code == 200
        assert not sidecar_path(root / closed).exists()
        assert (await c.get(url)).status_code == 404


async def test_protect_current_unavailable_non_mock(data_dir):
    from core_helpers import FakeUpstream as FU

    from fnav.mock.sensors import MockSensorHub

    opts = make_opts(mock=False, local_transport=FU().transport, hub_factory=MockSensorHub)
    async with running_app(opts, client_addr=("127.0.0.1", 1)) as c:
        core = c.app.state.core
        res = await core.handle_button("long_press")
        assert res is None
        st = core.camera.status
        assert st.state == "error"
        assert core.hub.led == "blink_fast"


async def test_button_press_mock(data_dir):
    async with running_app() as c:
        core = c.app.state.core
        assert core.hub.led == "blink_slow"  # mock-камера «пишет» → LED мигает
        await login(c)
        r = await c.post("/api/mock/button", json={"event": "long_press"})
        seg = r.json()["result"]["segment"]
        assert seg and (paths.recordings_dir() / (seg + ".lock")).exists()
        assert core.hub.led == "on"  # подтверждение нажатия — горит ровно
        core._led_timer._run()  # время подтверждения истекло
        assert core.hub.led == "blink_slow"


async def test_button_short_press_switches_cameras_video_output_stays_in_the_api(data_dir):
    """Кнопка GPS-модуля (коротко) — передняя ↔ задняя камера на экране; видеовыход — только через API / клавишу,
    если её назначить на video_toggle."""
    async with running_app() as c:
        core = c.app.state.core
        await login(c)
        r = await c.post("/api/mock/button", json={"event": "press"})  # задняя выключена — экран не меняется
        assert r.json()["result"] == {"ok": False, "camera": "front", "error": "Rear camera is off"}
        assert (await c.post("/api/camera/view", json={"camera": "rear"})).status_code == 409
        await c.patch("/api/settings", json={"rear_camera": {"enabled": True}})
        r = await c.post("/api/mock/button", json={"event": "press"})
        assert r.json()["result"] == {"ok": True, "camera": "rear"}
        disp = core.build_telemetry().display
        assert (disp.mode, disp.reason, disp.camera, disp.rear_mirror) == ("camera", "rear", "rear", True)
        assert core.build_telemetry().camera_rear is not None
        r = await c.post("/api/camera/view", json={})  # без camera — переключить
        assert r.json() == {"camera": "front", "rear_enabled": True}
        assert core.build_telemetry().display.camera == "front"
        await c.post("/api/camera/view", json={"camera": "rear"})
        await c.patch("/api/settings", json={"rear_camera": {"enabled": False}})  # выключили — экран вернулся
        assert core.view_camera == "front" and core.build_telemetry().camera_rear is None
        r = await c.get("/api/display/output")
        assert r.json()["on"] is True
        r = await c.post("/api/display/output", json={})  # без "on" — переключить
        assert r.json()["on"] is False


def test_attach_events_marks_alarm_events_inside_recordings():
    from datetime import datetime
    from types import SimpleNamespace

    from fnav.core.recordings import attach_events

    t0 = datetime(2026, 10, 27, 22, 0, 0).timestamp()  # местное время, как в имени файла

    def ev(i, dt, kind="approach", **data):
        return SimpleNamespace(id=f"e{i}", kind=kind, title=kind.title(), ts=t0 + dt, data=data)

    items = [
        {"path": "2026-10-27/2026-10-27_22-10-00.ts", "start": "2026-10-27T22:10:00", "duration_s": None,
         "in_progress": True},
        {"path": "2026-10-27/2026-10-27_22-00-00.ts", "start": "2026-10-27T22:00:00", "duration_s": 600.0,
         "in_progress": False},
        {"path": "nosync/boot-1_0001.ts", "start": None, "duration_s": 60.0, "in_progress": False},
    ]
    events = [ev(1, 327.5, snapshot=True), ev(2, 601.0, kind="impact"), ev(3, 700.0), ev(4, 30.0, kind="test"),
              ev(5, -100.0)]
    out = attach_events(items, events, now=t0 + 720)
    cur, done, nosync = out
    assert [m["id"] for m in done["events"]] == ["e1", "e2"]  # e2 — на стыке (≤ 2 с после конца)
    assert done["events"][0] == {"id": "e1", "kind": "approach", "title": "Approach", "ts": t0 + 327.5,
                                 "offset_s": 327.5, "snapshot": True}
    assert done["events"][1]["offset_s"] == 600.0  # не дальше конца записи
    assert [m["id"] for m in cur["events"]] == ["e2", "e3"]  # e2 — и в следующей (стык); идущая — до «сейчас»
    assert [m["offset_s"] for m in cur["events"]] == [1.0, 100.0]
    assert nosync["events"] == []  # время неизвестно — без меток
    # пропало видео: метка там, где пропало (data.since), а не через video_loss_s, когда подняли тревогу
    lost = ev(6, 625.0, kind="video_lost", camera="front", since=t0 + 590.0)
    done = attach_events([dict(items[1])], [lost], now=t0 + 720)[0]
    assert [(m["id"], m["offset_s"]) for m in done["events"]] == [("e6", 590.0)]


async def test_true_start_from_the_keyframe_index_across_a_time_zone_change(tmp_path):
    """Имена — по местному времени, пояс меняется в дороге: начало записи — start_ts из .ts.idx (регистратор)."""
    from types import SimpleNamespace

    from fnav.core.recordings import Recordings, attach_events, recording_at

    root = tmp_path / "rec"
    t0 = 1_793_623_210.0  # 2026-11-02 12:40:10 UTC: в Варшаве 13:40:10 — так названа запись
    warsaw = root / "2026-11-02/2026-11-02_13-40-10.ts"
    lisbon = root / "2026-11-02/2026-11-02_13-10-00.ts"  # уже в Лиссабоне (UTC+0): по имени «раньше», на деле позже
    for p, start in ((warsaw, t0), (lisbon, t0 + 1790)):
        write_rec(p, 4, sidecar=False)
        SidecarWriter(p, start).close()
        os.utime(p, (start + 600, start + 600))
    old = root / "2026-11-01/2026-11-01_09-00-00.ts"  # записана до start_ts в индексе
    write_rec(old, 4, sidecar=True)
    os.utime(old, (t0 - 86400, t0 - 86400))
    recs = Recordings(root)
    items = await recs.list()
    assert [(i["path"].split("/")[1], i["start_ts"]) for i in items] == [
        ("2026-11-02_13-10-00.ts", t0 + 1790), ("2026-11-02_13-40-10.ts", t0), ("2026-11-01_09-00-00.ts", None)]
    assert items[1]["start"] == "2026-11-02T13:40:10"  # местное время записи — как было
    ev = SimpleNamespace(id="e1", kind="impact", title="Impact", ts=t0 + 3, data={})
    marks = {i["path"]: i["events"] for i in attach_events(items, [ev], now=t0 + 4000)}
    assert [m["offset_s"] for m in marks["2026-11-02/2026-11-02_13-40-10.ts"]] == [3.0]  # в любом поясе системы
    assert marks["2026-11-02/2026-11-02_13-10-00.ts"] == []
    assert recording_at(items, t0 + 2, "front", now=t0 + 4000) == {"path": "2026-11-02/2026-11-02_13-40-10.ts",
                                                                     "offset_s": 2.0}
    # только что начатая: индекса ещё нет — не запоминаем «нет начала», следующий список его найдёт
    cur = "2026-11-02/2026-11-02_14-00-00.ts"
    write_rec(root / cur, 2, sidecar=False)
    assert (await recs.item(cur, current=cur))["start_ts"] is None
    SidecarWriter(root / cur, t0 + 4790).close()
    assert (await recs.item(cur, current=cur))["start_ts"] == t0 + 4790
