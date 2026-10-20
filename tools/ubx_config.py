#!/usr/bin/env python3
"""Configure the Holybro M8N (u-blox NEO-M8N) over UBX: baud rate, nav rate, constellations, dynamic model, save.

gpsd owns the serial port, so by default it is stopped for the duration (sudo systemctl stop gpsd.socket
gpsd.service) and started again afterwards; gpsd then auto-detects the new baud rate.

    python tools/ubx_config.py                                  # values from settings.json (or defaults)
    python tools/ubx_config.py --rate 5 --gnss gps,glonass --dyn automotive
    python tools/ubx_config.py --probe                          # only detect baud / version / rate
    python tools/ubx_config.py --no-gpsd --device /dev/ttyUSB0  # gpsd not involved

Needs pyserial (apt: python3-serial) and access to the port (group dialout).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def load_settings():
    from fnav.models import Settings

    try:
        from fnav.paths import settings_file

        p = settings_file()
        if p.exists():
            return Settings.model_validate_json(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"(settings.json unreadable, using defaults: {e})")
    return Settings()


def probe(device: str, baud: int, manage_gpsd: bool) -> dict:
    from fnav.sensors import m8n, ubx

    res: dict = {"ok": False}
    stopped = False
    if manage_gpsd:
        ok, info = m8n.gpsd_control("stop")
        print(f"stopping gpsd: {info}")
        if not ok:
            return {"ok": False, "error": f"failed to stop gpsd: {info}"}
        stopped = True
    try:
        port = m8n.open_serial(device, baud)
        try:
            c = m8n.M8NConfigurator(port, log=print)
            found = c.detect_baud(first=baud)
            if found is None:
                res["error"] = "GPS receiver not connected or not responding"
                return res
            res["baud"] = found
            ver = c.poll(ubx.CLS_MON, ubx.ID_MON_VER)
            if ver:
                res["version"] = ubx.parse_mon_ver(ver.payload)
            g = c.poll(ubx.CLS_CFG, ubx.ID_CFG_GNSS)
            if g:
                res["constellations"] = ubx.parse_cfg_gnss(g.payload).enabled_names()
            res["rate_hz_measured"] = round(c.measure_rate(3.0), 2)
            res["nmea_sample"] = c.nmea_seen[-5:]
            res["ok"] = True
        finally:
            port.close()
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{e.__class__.__name__}: {e}"
    finally:
        if stopped:
            ok, info = m8n.gpsd_control("start")
            print(f"starting gpsd: {info}")
    return res


def main(argv: list[str] | None = None) -> int:
    from fnav.sensors import m8n, ubx

    s = load_settings().gps
    ap = argparse.ArgumentParser(description="Configure u-blox NEO-M8N via UBX")
    ap.add_argument("--device", default=s.device)
    ap.add_argument("--baud", type=int, default=s.baud, help="target baud rate (default from settings)")
    ap.add_argument("--rate", type=int, default=s.rate_hz, help="navigation rate, Hz")
    ap.add_argument("--gnss", default=",".join(s.constellations), help="e.g. gps,glonass,galileo")
    ap.add_argument("--dyn", default=s.dyn_model, choices=sorted(ubx.DYN_MODELS))
    ap.add_argument("--pvt", choices=["on", "off"], help="enable/disable UBX NAV-PVT output (default: untouched)")
    ap.add_argument("--no-save", action="store_true", help="do not save to flash (CFG-CFG)")
    ap.add_argument("--no-gpsd", action="store_true", help="do not stop/start gpsd")
    ap.add_argument("--probe", action="store_true", help="only detect baud rate, firmware, constellations, rate")
    a = ap.parse_args(argv)

    if a.probe:
        res = probe(a.device, a.baud, not a.no_gpsd)
    else:
        res = m8n.configure_receiver(
            a.device, a.baud, a.rate, [x.strip() for x in a.gnss.split(",") if x.strip()], a.dyn,
            save=not a.no_save, enable_pvt=None if a.pvt is None else a.pvt == "on",
            manage_gpsd=not a.no_gpsd, log=print,
        )
    print(json.dumps(res, indent=2, ensure_ascii=False, default=str))
    for w in res.get("warnings", []):
        print(f"WARNING: {w}")
    print("RESULT:", "OK" if res.get("ok") else f"FAILED - {res.get('error', 'see steps above')}")
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
