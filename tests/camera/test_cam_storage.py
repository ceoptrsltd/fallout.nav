"""Очистка по свободному месту, защита .lock, нехватка места → ошибка/пауза записи."""

from __future__ import annotations

import os
from pathlib import Path

from fnav.camera.service import NO_SPACE, CameraService
from fnav.camera.storage import cleanup, list_recordings, repair_latest, segment_reserve_bytes, stats

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
