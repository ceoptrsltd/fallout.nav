"""Задняя камера: поиск по MAC (адрес у точки доступа телефона меняется), состояние между запусками
(camera_rear.json: MAC, измеренный битрейт). Таблицы соседей — подставные."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fnav.camera.discover import (
    CameraFinder,
    RearState,
    is_unreachable,
    parse_ip_neigh,
    parse_proc_arp,
    read_bitrate,
    replace_host,
)
from fnav.camera.service import REAR_DIR, CameraService
from fnav.models import RearCameraSettings, Settings

MAC = "a4:cf:12:34:56:78"
OLD, NEW, ME = "172.23.204.5", "10.150.83.23", "10.150.83.20"

IP_NEIGH = f"""10.150.83.1 dev wlan0 lladdr 3a:aa:bb:cc:dd:ee REACHABLE
{OLD} dev wlan0 FAILED
10.150.83.7 dev wlan0  INCOMPLETE
{NEW} dev wlan0 lladdr A4:CF:12:34:56:78 STALE
"""

PROC_ARP = f"""IP address       HW type     Flags       HW address            Mask     Device
10.150.83.1      0x1         0x2         3a:aa:bb:cc:dd:ee     *        wlan0
{NEW}    0x1         0x2         a4:cf:12:34:56:78     *        wlan0
10.150.83.9      0x1         0x0         00:00:00:00:00:00     *        wlan0
"""


def test_neighbour_tables_and_urls():
    assert parse_ip_neigh(IP_NEIGH) == {"10.150.83.1": ("3a:aa:bb:cc:dd:ee", "REACHABLE"), NEW: (MAC, "STALE")}
    assert parse_proc_arp(PROC_ARP) == {"10.150.83.1": ("3a:aa:bb:cc:dd:ee", "REACHABLE"), NEW: (MAC, "REACHABLE")}
    assert replace_host(f"rtsp://{OLD}:8554/profile0", NEW) == f"rtsp://{NEW}:8554/profile0"
    assert replace_host(f"rtsp://admin:x@{OLD}/s", NEW) == f"rtsp://admin:x@{NEW}/s"
    assert replace_host("not a url", NEW) == "not a url"


def test_only_unreachable_errors_count():
    """Искать по MAC — только когда камеры нет по адресу; логин, кодек, оборванный поток — камера на месте."""
    for msg in (f"[Errno 113] No route to host: 'rtsp://admin:***@{OLD}:8554/profile0'",
                "[Errno 110] Connection timed out", "[Errno 111] Connection refused",
                "[Errno 101] Network is unreachable", "[Errno 1414092869] Immediate exit requested"):
        assert is_unreachable(msg), msg
    for msg in ("Server returned 401 Unauthorized (authorization failed)", "video codec is hevc",
                "Invalid data found when processing input", "stream ended", None, ""):
        assert not is_unreachable(msg), msg


class Net:
    """Подставная сеть: таблица соседей ядра; обход подсети «будит» камеру на новом адресе."""

    def __init__(self) -> None:
        self.table: dict[str, tuple[str, str]] = {OLD: (MAC, "REACHABLE")}
        self.after_sweep: dict[str, tuple[str, str]] = {}
        self.sweeps: list[str] = []
        self.t = [1000.0]

    def neighbours(self):
        return dict(self.table)

    def sweep(self, local: str) -> int:
        self.sweeps.append(local)
        self.table.update(self.after_sweep)
        return 253

    def finder(self, state: RearState) -> CameraFinder:
        return CameraFinder(state, neighbours_fn=self.neighbours, sweep_fn=self.sweep,
                            local_ip_fn=lambda _h: ME, mono=lambda: self.t[0], sleep=lambda _s: None)


def test_finder_learns_the_mac_and_finds_the_camera_at_a_new_address(tmp_path: Path):
    net = Net()
    state = RearState(tmp_path / "camera_rear.json")
    f = net.finder(state)
    assert f.find(OLD) is None and not net.sweeps  # MAC ещё не известен — искать нечего
    assert f.learn("cam.local") is None  # имя — его найдёт резолвер
    assert f.learn(OLD) == MAC
    assert json.loads((tmp_path / "camera_rear.json").read_text()) == {"mac": MAC, "host": OLD}
    # точка доступа выдала другую подсеть: старый адрес мёртв, камера — на новом; старая запись в таблице (не в
    # нашей /24) и чужой MAC — не кандидаты
    net.table = {OLD: (MAC, "STALE"), "10.150.83.1": ("3a:aa:bb:cc:dd:ee", "REACHABLE")}
    net.after_sweep = {NEW: (MAC, "REACHABLE"), "10.150.83.40": (MAC, "STALE")}
    assert f.find(OLD) == NEW and net.sweeps == [ME]  # REACHABLE — лучше STALE
    assert RearState(tmp_path / "camera_rear.json").get()["host"] == NEW
    assert f.find(NEW) is None and len(net.sweeps) == 1  # не чаще раза в минуту
    net.t[0] += 61
    net.table = {NEW: (MAC, "STALE")}
    net.after_sweep = {}
    assert f.find(NEW) is None and len(net.sweeps) == 2  # камера на своём адресе, просто не отвечает — не новость


def test_rear_state_bitrate_is_read_only_when_fresh(tmp_path: Path):
    p = tmp_path / "camera_rear.json"
    st = RearState(p)
    st.update(mac=MAC, host=OLD)
    t = [0.0]
    st.save_bitrate(812.4, mono=lambda: t[0])
    assert read_bitrate(p) == 812.4
    data = json.loads(p.read_text())
    assert data["mac"] == MAC and data["bitrate_kbps"] == 812.4
    mtime = p.stat().st_mtime_ns
    t[0] = 30.0
    st.save_bitrate(830.0, mono=lambda: t[0])  # +2 % и недавно — файл не трогаем
    assert p.stat().st_mtime_ns == mtime and read_bitrate(p) == 812.4
    st.save_bitrate(1200.0, mono=lambda: t[0])  # заметно больше — сразу
    assert read_bitrate(p) == 1200.0
    assert read_bitrate(p, now=data["bitrate_at"] + 7200) is None  # давно не писала — оценка из настроек
    assert read_bitrate(tmp_path / "none.json") is None


class _Src:
    """Источник-заглушка: запоминает адреса, с которыми его создали; кадров не даёт."""

    sensor_mode = output_size = None

    def __init__(self, rear: RearCameraSettings, made: list) -> None:
        made.append((rear.url, rear.lores_url))

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def request_keyframe(self) -> None: ...


def test_service_reconnects_to_the_address_found_by_mac(tmp_path: Path):
    """Неудачи «недоступна» подряд → поиск по MAC → переподключение по новому адресу (url и lores_url), в статусе
    discovered_host; ядро записало адрес в настройки → /reload без переподключения. Ошибка логина — счёт заново."""
    settings = tmp_path / "settings.json"
    rear = RearCameraSettings(enabled=True, url=f"rtsp://{OLD}:8554/profile0", lores_url=f"rtsp://{OLD}:8554/profile1")
    settings.write_text(Settings(rear_camera=rear).model_dump_json(), encoding="utf-8")
    made: list = []
    svc = CameraService(root=tmp_path / "rec" / REAR_DIR, settings_path=settings, rtsp_url=None, camera_id="rear",
                        source_factory=lambda s, _sink: _Src(s.rear_camera, made), ring_path=tmp_path / "ring",
                        rear_state_path=tmp_path / "camera_rear.json", sound_dir=tmp_path / "clips")
    net = Net()
    svc.finder = net.finder(svc.rear_state)
    svc.finder.learn(OLD)
    net.table = {}
    net.after_sweep = {NEW: (MAC, "REACHABLE")}
    svc.start()
    try:
        assert _wait(lambda: len(made) == 1)
        svc.on_connect_failed("[Errno 113] No route to host")
        svc.on_connect_failed("Server returned 401 Unauthorized")  # камера ответила — не подряд
        svc.on_connect_failed("[Errno 113] No route to host")
        svc.on_connect_failed("[Errno 110] Connection timed out")
        time.sleep(0.2)
        assert not net.sweeps
        svc.on_connect_failed("[Errno 111] Connection refused")
        assert _wait(lambda: len(made) >= 2), made
        assert made[-1] == (f"rtsp://{NEW}:8554/profile0", f"rtsp://{NEW}:8554/profile1")
        assert svc.status().discovered_host == NEW and net.sweeps == [ME]
        # ядро записало найденный адрес в настройки: камера уже там — без переподключения, discovered_host снят
        n = len(made)
        settings.write_text(Settings(rear_camera=rear.model_copy(update={
            "url": f"rtsp://{NEW}:8554/profile0", "lores_url": f"rtsp://{NEW}:8554/profile1"})).model_dump_json(),
            encoding="utf-8")
        assert svc.reload() is False
        assert svc.status().discovered_host is None
        time.sleep(0.3)
        assert len(made) == n
    finally:
        svc.stop()


def _wait(pred, timeout=10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_discovery_runs_off_the_reader_thread(tmp_path: Path):
    """Обход подсети (секунды) — не в потоке чтения помощника: on_connect_failed возвращается сразу."""
    settings = tmp_path / "settings.json"
    settings.write_text(Settings(rear_camera=RearCameraSettings(enabled=True, url=f"rtsp://{OLD}/s"))
                        .model_dump_json(), encoding="utf-8")
    svc = CameraService(root=tmp_path / "rec", settings_path=settings, rtsp_url=None, camera_id="rear",
                        ring_path=tmp_path / "ring", rear_state_path=tmp_path / "st.json", sound_dir=tmp_path / "c")
    gate = threading.Event()
    net = Net()
    svc.finder = net.finder(svc.rear_state)
    svc.finder.learn(OLD)
    svc.finder.sleep = lambda _s: gate.wait(5)
    t0 = time.monotonic()
    for _ in range(3):
        svc.on_connect_failed("No route to host")
    assert time.monotonic() - t0 < 0.5 and _wait(lambda: net.sweeps)
    gate.set()
    assert _wait(lambda: not svc._discovering)


def test_sweeps_slow_down_while_the_camera_stays_away(tmp_path: Path):
    """Камеры нет долго (выключена, питание от зажигания): первые SWEEP_FAST_N обходов — раз в минуту, дальше — раз
    в 5 мин; камера снова работает (learn) — следующая пропажа опять с частых обходов."""
    from fnav.camera.discover import SWEEP_FAST_N, SWEEP_MIN_S, SWEEP_SLOW_S

    net = Net()
    f = net.finder(RearState(tmp_path / "camera_rear.json"))
    f.learn(OLD)
    net.table = {}
    for _ in range(SWEEP_FAST_N):
        assert f.sweep_interval() == SWEEP_MIN_S and f.may_sweep()
        assert f.find(OLD) is None
        net.t[0] += SWEEP_MIN_S
    assert len(net.sweeps) == SWEEP_FAST_N and f.sweep_interval() == SWEEP_SLOW_S
    assert not f.may_sweep() and f.find(OLD) is None and len(net.sweeps) == SWEEP_FAST_N  # минута — уже рано
    net.t[0] += SWEEP_SLOW_S
    assert f.may_sweep()
    net.table = {OLD: (MAC, "REACHABLE")}
    f.learn(OLD)  # камера снова работает
    assert f.sweep_interval() == SWEEP_MIN_S


def _rear_svc(tmp_path: Path) -> CameraService:
    settings = tmp_path / "settings.json"
    settings.write_text(Settings(rear_camera=RearCameraSettings(enabled=True, url=f"rtsp://{OLD}:8554/profile0"))
                        .model_dump_json(), encoding="utf-8")
    return CameraService(root=tmp_path / "rec", settings_path=settings, rtsp_url=None, camera_id="rear",
                         ring_path=tmp_path / "ring", rear_state_path=tmp_path / "st.json", sound_dir=tmp_path / "c")


def test_rear_retries_back_off_to_five_minutes(tmp_path: Path):
    """Задней нет: 5, 10, 20, 40, 80, 160 с, дальше — раз в 5 мин (а не каждые полминуты весь день)."""
    from fnav.camera.service import REAR_RETRY_MAX_S

    svc = _rear_svc(tmp_path)
    got = []
    for n in range(1, 10):
        svc._fails = n
        got.append(svc._retry_delay())
    assert got == [5, 10, 20, 40, 80, 160, REAR_RETRY_MAX_S, REAR_RETRY_MAX_S, REAR_RETRY_MAX_S] and REAR_RETRY_MAX_S == 300


def test_backoff_ends_as_soon_as_the_camera_answers(tmp_path: Path, monkeypatch):
    """Пауза до 5 мин не держит включённую камеру: раз в REAR_PROBE_S — тихая проверка порта; ответил — сразу новая
    попытка с короткими паузами. Порт отвечал и до паузы (логин, кодек) — ждём паузу целиком."""
    from fnav.camera import service

    monkeypatch.setattr(service, "REAR_PROBE_S", 0.02)
    svc = _rear_svc(tmp_path)
    calls: list[tuple[str, int]] = []
    answers = iter([False, False, False, True])
    svc.probe_fn = lambda host, port: calls.append((host, port)) or next(answers, True)
    svc._fails = 7
    t0 = time.monotonic()
    svc._backoff_wait(5.0)
    assert time.monotonic() - t0 < 1.0, "should return when the port answers, not after 5 s"
    assert calls[0] == (OLD, 8554) and len(calls) == 4 and svc._fails == 0

    svc.probe_fn = lambda host, port: True  # порт отвечает, камера всё равно не пускает — ждать паузу
    t0 = time.monotonic()
    svc._backoff_wait(0.2)
    assert time.monotonic() - t0 >= 0.19
