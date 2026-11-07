"""Статус системы: Wi-Fi без пересканирования эфира (раньше nmcli сканировал каждые ~34 с и замораживал Wi-Fi на 2–5 с,
задняя камера от этого обрывалась), сила сигнала — живая из /proc/net/wireless."""

from __future__ import annotations

from fnav.core import system

WIRELESS = """Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE
 face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22
 wlan0: 0000   63.  -47.  -256        0      0      0   7460      0        0
"""


def test_wifi_level_from_proc(tmp_path):
    p = tmp_path / "wireless"
    p.write_text(WIRELESS)
    assert system.wifi_level_dbm(str(p)) == -47
    assert system.wifi_level_dbm(str(p), iface="wlan1") is None
    assert system.wifi_level_dbm(str(tmp_path / "missing")) is None
    p.write_text(WIRELESS.splitlines()[0] + "\n" + WIRELESS.splitlines()[1] + "\n")
    assert system.wifi_level_dbm(str(p)) is None  # нет связи — строки интерфейса нет
    p.write_text(WIRELESS.replace("-47.", "209."))  # старый WEXT: 8 бит без знака
    assert system.wifi_level_dbm(str(p)) == -47
    assert [system.level_to_pct(d) for d in (-30, -40, -47, -70, -100, -110)] == [100, 100, 89, 50, 0, 0]


async def test_status_never_rescans_wifi(monkeypatch):
    calls: list[tuple[str, ...]] = []

    async def fake_run(*args: str, timeout: float = 5.0) -> tuple[int, str, str]:
        calls.append(args)
        if args[0] == "nmcli":
            return 0, "no:Other:40\nyes:CampHotspot:72\n", ""
        return 1, "", ""

    monkeypatch.setattr(system, "IS_LINUX", True)
    monkeypatch.setattr(system, "run", fake_run)
    monkeypatch.setattr(system, "wifi_level_dbm", lambda: -47)
    st = await system.SystemService(mock=False).refresh()
    nm = [c for c in calls if c[0] == "nmcli"]
    assert nm and all(c[-2:] == ("--rescan", "no") for c in nm), nm
    assert st.wifi_ssid == "CampHotspot" and st.wifi_signal_pct == 89  # живой уровень, не из старого списка
    monkeypatch.setattr(system, "wifi_level_dbm", lambda: None)
    st = await system.SystemService(mock=False).refresh()
    assert st.wifi_signal_pct == 72  # нет /proc/net/wireless — из списка nmcli
