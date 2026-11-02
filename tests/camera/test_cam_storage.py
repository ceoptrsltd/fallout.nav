"""Очистка по свободному месту, защита .lock, нехватка места → ошибка/пауза записи."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fnav.camera import service as service_mod
from fnav.camera.service import NO_SPACE, CameraService
from fnav.camera.storage import (
    cleanup,
    list_recordings,
    loop_minimum,
    repair_latest,
    segment_reserve_bytes,
    stats,
)

MB = 1024 * 1024


class FakeDisk:
    """Свободное место = ёмкость − размер всех .ts под root."""

    def __init__(self, root: Path, capacity: int) -> None:
        self.root = root
        self.capacity = capacity

    def __call__(self, _path: Path) -> int:
        return self.capacity - sum(p.stat().st_size for p in self.root.rglob("*.ts"))


def make_files(root: Path, names: list[str], size: int = MB) -> list[Path]:
    out = []
    for i, n in enumerate(names):
        p = root / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\x47" * size)
        os.utime(p, (1_700_000_000 + i * 600, 1_700_000_000 + i * 600))  # по порядку: старые → новые
        out.append(p)
    return out


def test_cleanup_deletes_oldest_unprotected_only(tmp_path: Path):
    files = make_files(tmp_path, [
        "2026-10-18/2026-10-18_10-00-00.ts",  # 0 старейший, защищён
        "2026-10-18/2026-10-18_10-10-00.ts",  # 1
        "nosync/boot-aaaaaaaa_0000.ts",       # 2 (одинокий в каталоге → каталог удаляется)
        "2026-10-19/2026-10-19_10-00-00.ts",  # 3 защищён
        "2026-10-19/2026-10-19_10-10-00.ts",  # 4
        "2026-10-20/2026-10-20_10-00-00.ts",  # 5 текущий
    ])
    (files[0].with_name(files[0].name + ".lock")).touch()
    (files[3].with_name(files[3].name + ".lock")).touch()
    # текущий — самый «старый» по mtime, чтобы проверить, что его не трогаем
    os.utime(files[5], (1_600_000_000, 1_600_000_000))
    disk = FakeDisk(tmp_path, capacity=10 * MB)  # свободно 4 МБ
    res = cleanup(tmp_path, target_free_bytes=6 * MB + 1, current=files[5], free_fn=disk)
    # надо освободить > 2 МБ → удаляются 1, 2, 4 (по возрасту), но 0/3 защищены, 5 текущий
    assert [p.name for p in res.deleted] == [files[1].name, files[2].name, files[4].name]
    assert res.ok and res.free_bytes == 7 * MB
    left = {p.name for p in list_recordings(tmp_path)}
    assert left == {files[0].name, files[3].name, files[5].name}
    assert not (tmp_path / "nosync").exists()  # пустой каталог удалён
    assert (tmp_path / "2026-10-18").exists()  # там остался защищённый
    assert stats(tmp_path) == (3, 3 * MB)


def test_cleanup_noop_when_enough_space(tmp_path: Path):
    make_files(tmp_path, ["a/x.ts", "a/y.ts"])
    res = cleanup(tmp_path, 1 * MB, free_fn=FakeDisk(tmp_path, 100 * MB))
    assert res.deleted == [] and res.ok


def test_cleanup_all_protected_reports_not_ok(tmp_path: Path):
    fs = make_files(tmp_path, ["a/x.ts", "a/y.ts"])
    for p in fs:
        p.with_name(p.name + ".lock").touch()
    res = cleanup(tmp_path, 5 * MB, free_fn=FakeDisk(tmp_path, 4 * MB))
    assert res.deleted == [] and not res.ok


def test_cleanup_total_size_limit(tmp_path: Path):
    files = make_files(tmp_path, [f"d/{i}.ts" for i in range(6)])  # 6 × 1 МБ, места на диске много
    files[1].with_name(files[1].name + ".lock").touch()  # защищённые считаются в лимит, но не удаляются
    disk = FakeDisk(tmp_path, capacity=1000 * MB)
    res = cleanup(tmp_path, 1 * MB, current=files[5], free_fn=disk, max_total_bytes=3 * MB)
    assert [p.name for p in res.deleted] == ["0.ts", "2.ts", "3.ts"]
    assert res.total_bytes == 3 * MB and not res.over_limit
    # остались защищённый и текущий; лимит ниже них — удалять больше нечего
    res = cleanup(tmp_path, 1 * MB, current=files[5], free_fn=disk, max_total_bytes=1 * MB)
    assert [p.name for p in res.deleted] == ["4.ts"] and res.over_limit
    assert {p.name for p in list_recordings(tmp_path)} == {"1.ts", "5.ts"}


def lock(p: Path) -> None:
    p.with_name(p.name + ".lock").touch()


def test_cleanup_limit_keeps_the_minimum_loop_only_free_space_goes_below(tmp_path: Path):
    """Защищённые сверх лимита: лимит не сокращает петлю (незащищённые, с текущей) меньше минимума; ниже — только
    нехватка свободного места. Раньше лимит удалял все незащищённые."""
    files = make_files(tmp_path, [f"d/{i}.ts" for i in range(10)])  # 10 × 1 МБ
    for i in (0, 1, 2, 4, 5, 6):
        lock(files[i])  # 6 МБ защищённых — больше лимита (5 МБ)
    disk = FakeDisk(tmp_path, capacity=1000 * MB)
    res = cleanup(tmp_path, 1 * MB, current=files[9], free_fn=disk, max_total_bytes=5 * MB, min_loop_bytes=3 * MB)
    # петля 3, 7, 8, 9 (4 МБ): удаляется только самый старый — дальше она стала бы меньше 3 МБ
    assert [p.name for p in res.deleted] == ["3.ts"]
    assert res.over_limit and res.total_bytes == 9 * MB
    assert res.protected_bytes == 6 * MB and res.loop_bytes == 3 * MB
    # повтор: ничего больше (не дырявим петлю, удаляя более новые)
    assert cleanup(tmp_path, 1 * MB, current=files[9], free_fn=disk, max_total_bytes=5 * MB,
                   min_loop_bytes=3 * MB).deleted == []
    # диск правда кончается: свободное место важнее минимума петли (текущий не трогаем)
    disk.capacity = 9 * MB + 1 * MB
    res = cleanup(tmp_path, 3 * MB, current=files[9], free_fn=disk, max_total_bytes=5 * MB, min_loop_bytes=3 * MB)
    assert [p.name for p in res.deleted] == ["7.ts", "8.ts"] and res.loop_bytes == 1 * MB
    assert {p.name for p in list_recordings(tmp_path)} == {"0.ts", "1.ts", "2.ts", "4.ts", "5.ts", "6.ts", "9.ts"}


def test_cleanup_without_a_minimum_loop_deletes_every_unprotected(tmp_path: Path):
    """Без минимума (min_loop_bytes=0, как было): защищённые сверх лимита — удаляется вся петля, кроме текущего."""
    files = make_files(tmp_path, [f"d/{i}.ts" for i in range(5)])
    lock(files[0])
    lock(files[1])
    res = cleanup(tmp_path, 1 * MB, current=files[4], free_fn=FakeDisk(tmp_path, 1000 * MB), max_total_bytes=1 * MB)
    assert [p.name for p in res.deleted] == ["2.ts", "3.ts"] and res.over_limit


def test_loop_minimum_is_an_hour_but_at_most_half_the_limit():
    gb = 1024**3
    hour = 4_500_000_000  # передняя 6000 + задняя 4000 кбит/с
    assert loop_minimum(hour, int(9.55 * gb)) == hour
    assert loop_minimum(hour, 2 * gb) == gb  # маленький лимит: без защищённых он работает как раньше
    assert loop_minimum(hour, 0) == 0


def test_service_protected_over_the_limit_keep_the_minimum_loop_and_report_it(tmp_path: Path, monkeypatch):
    """Сервис: петля — час записи, но не больше половины лимита; передняя чистит и записи задней (её чужой текущий
    сегмент не трогает); статус — protected_gb, loop_minutes, over_limit. «ГБ» в тесте — МБ (маленькие файлы)."""
    monkeypatch.setattr(service_mod, "GB", MB)
    root = tmp_path / "rec"
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "camera": {"bitrate_kbps": 500, "segment_minutes": 1},
        "storage": {"min_free_gb": 0.5, "max_recordings_gb": 10},
        "rear_camera": {"enabled": True, "url": "rtsp://cam/s", "record": True, "bitrate_kbps": 1000},
    }), encoding="utf-8")
    prot = make_files(root, [f"2026-10-20/p{i}.ts" for i in range(8)])  # 8 «ГБ» защищённых — старые
    for p in prot:
        lock(p)
    loop = make_files(root, ["2026-10-21/f1.ts", "rear/2026-10-21/r1.ts", "2026-10-21/f2.ts", "rear/2026-10-21/r2.ts",
                             "2026-10-21/f3.ts", "rear/2026-10-21/r3.ts"])
    for i, p in enumerate(loop):
        os.utime(p, (1_750_000_000 + i, 1_750_000_000 + i))  # новее защищённых
    now = time.time()
    os.utime(loop[-1], (now, now))  # текущий сегмент задней (пишет другой процесс)
    svc = CameraService(root=root, settings_path=settings, free_fn=FakeDisk(root, 1000 * MB), rtsp_url=None,
                        ring_path=tmp_path / "ring")
    reserve = segment_reserve_bytes(500, 1)  # 3,75 МБ
    max_total = 10 * MB - reserve  # ≈ 6,4 МБ
    min_loop = max_total // 2  # час (1500 кбит/с = 675 МБ) больше половины лимита
    svc.housekeeping_once()
    # петля 6 МБ: удаляются самые старые обеих камер, пока она не меньше min_loop (≈ 3,2 МБ)
    left = {p.name for p in list_recordings(root)}
    assert left == {f"p{i}.ts" for i in range(8)} | {"f2.ts", "r2.ts", "f3.ts", "r3.ts"}
    st = svc.status()
    assert st.over_limit and st.protected_gb == 8.0
    assert st.loop_minutes == round(4 * MB / (1500 * 1000 / 8) / 60)
    assert 8 * MB + min_loop > max_total
    # сняли защиту со старых: теперь они — самые старые в петле и уходят первыми, петля цела, предупреждения нет
    for p in prot[:6]:
        p.with_name(p.name + ".lock").unlink()
    svc.housekeeping_once()
    st = svc.status()
    assert not st.over_limit and st.protected_gb == 2.0
    assert {p.name for p in list_recordings(root)} == {"p6.ts", "p7.ts", "f2.ts", "r2.ts", "f3.ts", "r3.ts"}


def test_service_no_space_error_and_resume(tmp_path: Path):
    root = tmp_path / "rec"
    fs = make_files(root, ["a/x.ts", "a/y.ts"], size=2 * MB)
    for p in fs:
        p.with_name(p.name + ".lock").touch()
    disk = FakeDisk(root, capacity=0)
    svc = CameraService(root=root, settings_path=tmp_path / "none.json", free_fn=disk, rtsp_url=None)
    s = svc.settings
    min_free = int(s.storage.min_free_gb * 1024**3)
    disk.capacity = 4 * MB + min_free - 1  # свободно чуть меньше резерва системы, всё защищено
    svc.housekeeping_once()
    st = svc.status()
    assert st.state == "error" and st.error == NO_SPACE == "No space left: all recordings are protected"
    assert st.recordings_count == 2
    # пользователь снял защиту → очистка освобождает место → запись возобновляется
    for p in fs:
        p.with_name(p.name + ".lock").unlink()
    reserve = segment_reserve_bytes(s.camera.bitrate_kbps, s.camera.segment_minutes)
    disk.capacity = 4 * MB + min_free + reserve - 1  # после удаления обоих файлов хватит на сегмент
    svc.housekeeping_once()
    st = svc.status()
    assert st.error is None and st.state == "stopped"
    assert [p.name for p in list_recordings(root)] == ["y.ts"]  # удалён только старейший


def test_repair_latest_removes_empty(tmp_path: Path):
    p = tmp_path / "a" / "b.ts"
    p.parent.mkdir()
    p.write_bytes(b"")
    repair_latest(tmp_path)
    assert not p.exists()
