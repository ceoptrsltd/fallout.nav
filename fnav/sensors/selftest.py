"""On-device sensor self-test (GPS via gpsd, compass on I2C, button / LED / buzzer).

    sudo systemctl stop fnav-core     # free the GPIO pins (gpsd keeps running)
    /opt/fnav/venv/bin/python -m fnav.sensors.selftest
    /opt/fnav/venv/bin/python -m fnav.sensors.selftest --skip-gpio --non-interactive

Exit code 0 = every checked part works. All output is English (user-facing).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import statistics
import sys
import time
from typing import Any

from fnav.models import Settings

OK, WARN, FAIL = "OK  ", "WARN", "FAIL"


class Report:
    def __init__(self) -> None:
        self.failed = False
        self.suggest: dict[str, Any] = {}

    def line(self, status: str, text: str) -> None:
        if status == FAIL:
            self.failed = True
        print(f"[{status}] {text}", flush=True)

    @staticmethod
    def info(text: str) -> None:
        print(f"       {text}", flush=True)


def load_settings() -> Settings:
    try:
        from fnav.paths import settings_file

        p = settings_file()
        if p.exists():
            return Settings.model_validate_json(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"(settings.json unreadable, using defaults: {e})")
    return Settings()


# ───────────────────────────── GPS ─────────────────────────────


async def _gpsd_collect(host: str, port: int, timeout: float) -> dict:
    out: dict[str, Any] = {"version": None, "devices": None, "tpv": [], "sky": None, "error": None}
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
    except (OSError, TimeoutError) as e:
        out["error"] = f"gpsd not reachable at {host}:{port} ({e.__class__.__name__}) - is gpsd running?"
        return out
    writer.write(b'?WATCH={"enable":true,"json":true};\n')
    await writer.drain()
    end = time.monotonic() + timeout
    first_fix_wait = True
    try:
        while time.monotonic() < end:
            try:
                line = await asyncio.wait_for(reader.readline(), max(0.1, end - time.monotonic()))
            except TimeoutError:
                break
            if not line:
                break
            try:
                m = json.loads(line)
            except ValueError:
                continue
            cls = m.get("class")
            if cls == "VERSION":
                out["version"] = m.get("release")
            elif cls == "DEVICES":
                out["devices"] = m.get("devices", [])
            elif cls == "TPV":
                out["tpv"].append((time.monotonic(), m))
                if first_fix_wait and m.get("mode", 0) >= 2 and out["sky"] and len(out["tpv"]) >= 20:
                    break
            elif cls == "SKY" and m.get("satellites") is not None:
                out["sky"] = m
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    return out


def check_gps(rep: Report, s: Settings, host: str, port: int, timeout: float) -> None:
    print("\n=== GPS (gpsd) ===")
    r = asyncio.run(_gpsd_collect(host, port, timeout))
    if r["error"]:
        rep.line(FAIL, r["error"])
        return
    rep.line(OK, f"gpsd {r['version'] or '?'} reachable")
    devs = r["devices"] or []
    for d in devs:
        rep.info(f"device {d.get('path')} driver={d.get('driver', '?')} bps={d.get('bps', '?')} "
                 f"cycle={d.get('cycle', '?')}s")
        if d.get("bps") and d.get("bps") != s.gps.baud:
            rep.line(WARN, f"receiver runs at {d.get('bps')} bps, expected {s.gps.baud}: "
                           f"run 'python tools/ubx_config.py' (or Settings > GPS > Configure)")
    tpv = r["tpv"]
    if not tpv:
        rep.line(FAIL, "GPS receiver NOT CONNECTED: gpsd is running but gets no data "
                       "(check wiring TX->RX, power, /dev/ttyAMA0, gpsd DEVICES)")
        return
    if len(tpv) >= 3:
        span = tpv[-1][0] - tpv[0][0]
        rate = (len(tpv) - 1) / span if span > 0 else 0.0
        st = OK if rate >= 0.8 * s.gps.rate_hz else WARN
        rep.line(st, f"update rate ~{rate:.1f} Hz (expected {s.gps.rate_hz} Hz)")
    last = tpv[-1][1]
    mode = {0: "unknown", 1: "no fix", 2: "2D fix", 3: "3D fix"}.get(last.get("mode", 0), "?")
    sky = r["sky"] or {}
    sats = sky.get("satellites") or []
    used = sum(1 for x in sats if x.get("used"))
    by = {}
    for x in sats:
        by[x.get("gnssid", "?")] = by.get(x.get("gnssid", "?"), 0) + 1
    names = {0: "GPS", 1: "SBAS", 2: "Galileo", 3: "BeiDou", 5: "QZSS", 6: "GLONASS"}
    sys_str = ", ".join(f"{names.get(k, k)}:{v}" for k, v in sorted(by.items(), key=lambda kv: str(kv[0])))
    rep.line(OK if last.get("mode", 0) >= 2 else WARN,
             f"{mode}; satellites used {used} / visible {len(sats)} ({sys_str or 'none'}); hdop {sky.get('hdop')}")
    if last.get("mode", 0) >= 2:
        rep.info(f"lat {last.get('lat')} lon {last.get('lon')} speed {last.get('speed')} m/s "
                 f"track {last.get('track')} time {last.get('time')}")
    else:
        rep.info("no fix yet: put the antenna under open sky; cold start may take 30-60 s")


# ───────────────────────────── Compass ─────────────────────────────


def check_compass(rep: Report, s: Settings, n: int = 50) -> None:
    print("\n=== Compass (I2C) ===")
    from fnav.sensors import compass as cm
    from fnav.sensors.calibration import apply_calibration
    from fnav.sensors.declination import declination_deg
    from fnav.sensors.heading import deviation_deg, mag_heading_deg, norm360

    c = s.compass
    try:
        bus = cm.open_bus(c.i2c_bus)
    except ImportError:
        rep.line(FAIL, "python module smbus2 is not installed (apt install python3-smbus2)")
        return
    except OSError as e:
        rep.line(FAIL, f"I2C bus {c.i2c_bus} not available ({e}); enable I2C (dtparam=i2c_arm=on)")
        return
    try:
        try:
            who = bus.read_byte_data(0x0E, 0x00)
            st = OK if who == 0x10 else WARN
            rep.line(st, f"0x0E WHO_AM_I = 0x{who:02X} (IST8310 expects 0x10)")
        except OSError:
            rep.line(WARN, "nothing answers at 0x0E (IST8310)")
        drv = cm.detect(bus, c.chip, c.address)
        if drv is None:
            rep.line(FAIL, f"Compass NOT CONNECTED: no IST8310/QMC5883L/HMC5883L on I2C-{c.i2c_bus} "
                           "(check SDA/SCL wiring, 'python tools/i2c_scan.py')")
            return
        drv.init()
        axes = cm.axes_for(drv.name, c.axes)
        rep.line(OK, f"{drv.name} at 0x{drv.address:02X}, axes chip->FRD: {cm.axes_spec_for(drv.name, c.axes)}")
        vals = []
        errors = 0
        period = 1.0 / max(1, c.rate_hz)
        end = time.monotonic() + n * period * 4 + 2
        while len(vals) < n and time.monotonic() < end:
            try:
                v = drv.read()
                if v is not None:
                    vals.append(cm.map_axes(v, axes))
            except OSError:
                errors += 1
            time.sleep(period)
        if len(vals) < n // 2:
            rep.line(FAIL, f"only {len(vals)} of {n} samples read ({errors} I/O errors)")
            return
        cols = list(zip(*vals, strict=True))
        mean = [statistics.fmean(x) for x in cols]
        sd = [statistics.pstdev(x) for x in cols]
        mag = math.sqrt(sum(m * m for m in mean))
        rep.line(OK if errors == 0 else WARN, f"{len(vals)} samples, {errors} I/O errors")
        rep.info("mean  x={:+7.2f} y={:+7.2f} z={:+7.2f} uT (FRD, uncalibrated)".format(*mean))
        rep.info("noise x={:6.2f}  y={:6.2f}  z={:6.2f} uT".format(*sd))
        st = OK if 20 <= mag <= 100 else WARN
        rep.line(st, f"|B| = {mag:.1f} uT raw (Earth field 25-65 uT; much more = hard iron near the module, "
                     "fixed by calibration)")
        if mean[2] < 0 and abs(mean[2]) > 0.5 * mag:
            rep.line(WARN, "z points up (negative) - in the northern hemisphere it should be positive (down): "
                           "module upside down or axis table wrong")
        raw_h = mag_heading_deg(mean[0], mean[1])
        cx, cy, _ = apply_calibration(mean, c.calibration)
        cal_h = mag_heading_deg(cx, cy)
        dev = deviation_deg(cal_h, c.calibration.deviation) if cal_h is not None else 0.0
        decl = c.declination if not isinstance(c.declination, str) else declination_deg(55.75, 37.62)
        if cal_h is not None and raw_h is not None:
            true_h = norm360(cal_h + decl + dev + c.mount_offset_deg)
            cal_src = c.calibration.source or ("none" if c.calibration.fitted_at is None else "manual")
            rep.info(f"heading: raw magnetic {raw_h:.1f} deg, calibrated {cal_h:.1f}, deviation {dev:+.1f}, "
                     f"true ~{true_h:.1f} (decl {decl:.1f}, mount offset {c.mount_offset_deg}, "
                     f"calibration: {cal_src}, rms {c.calibration.rms_deg})")
        rep.info("check: turn the module clockwise (seen from above) -> heading must INCREASE; "
                 "if it decreases set compass.axes (mirror)")
    finally:
        with contextlib.suppress(Exception):
            bus.close()


# ───────────────────────────── GPIO ─────────────────────────────


def _ask(prompt: str, interactive: bool) -> str:
    if not interactive:
        return ""
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return ""


def check_gpio(rep: Report, s: Settings, interactive: bool, wait_s: float = 10.0,
               button_pull: str = "auto") -> None:
    print("\n=== Button / LED / buzzer (GPIO) ===")
    g = s.gpio
    try:
        from gpiozero import DigitalOutputDevice, InputDevice
    except ImportError:
        rep.line(FAIL, "python module gpiozero is not installed (apt install python3-gpiozero python3-lgpio)")
        return
    from fnav.sensors.gpio import probe_idle_level

    # LED
    if g.led_gpio is not None:
        try:
            led = DigitalOutputDevice(g.led_gpio, active_high=True)
        except Exception as e:  # noqa: BLE001
            rep.line(FAIL, f"LED GPIO{g.led_gpio} busy/unavailable ({e}); stop fnav-core first")
            led = None
        if led is not None:
            try:
                print(f"LED on GPIO{g.led_gpio}: phase 1 = pin LOW (3 s), phase 2 = pin HIGH (3 s). Watch the button LED.")
                led.off()
                print("  phase 1: LOW")
                time.sleep(3)
                led.on()
                print("  phase 2: HIGH")
                time.sleep(3)
                ans = _ask("Which phase was the LED lit? [1/2/n(one)]: ", interactive)
                if ans == "1":
                    rep.line(OK, "LED is active-LOW -> gpio.led_active_low = true")
                    rep.suggest["led_active_low"] = True
                elif ans == "2":
                    rep.line(OK, "LED is active-HIGH -> gpio.led_active_low = false")
                    rep.suggest["led_active_low"] = False
                elif ans in ("n", "none", "0"):
                    rep.line(FAIL, "LED not lit in either phase: LED NOT CONNECTED (check pin 7 and VDD_3V3 pin 8)")
                else:
                    rep.line(WARN, "LED polarity not confirmed (non-interactive); default is active-low")
                # finish blinking a few times
                for _ in range(6):
                    led.toggle()
                    time.sleep(0.15)
            finally:
                led.close()

    # button
    if g.button_gpio is not None:
        try:
            probe = probe_idle_level(g.button_gpio)
        except Exception as e:  # noqa: BLE001
            rep.line(FAIL, f"button GPIO{g.button_gpio} busy/unavailable ({e}); stop fnav-core first")
            probe = None
        if probe is not None:
            how = "driven by the module" if probe.driven else "floating (no external pull)"
            rep.info(f"button GPIO{g.button_gpio} idle: {how}, idle level {probe.level}")
            if probe.driven and button_pull == "auto":
                dev = InputDevice(g.button_gpio, pull_up=None, active_state=True)
            else:  # линия «висит»: по умолчанию подтяжка вниз (кнопка на 3.3 В, как у Pixhawk)
                dev = InputDevice(g.button_gpio, pull_up=(button_pull == "up"))
                rep.info(f"using internal pull-{'up' if button_pull == 'up' else 'down'}")
            try:
                idle = int(dev.pin.state)
                print(f"Press the SAFETY button within {wait_s:.0f} s...", flush=True)
                end = time.monotonic() + wait_s
                pressed = None
                while time.monotonic() < end:
                    lvl = int(dev.pin.state)
                    if lvl != idle:
                        time.sleep(0.03)
                        if int(dev.pin.state) == lvl:
                            pressed = lvl
                            break
                    time.sleep(0.005)
                if pressed is None:
                    rep.line(FAIL, "no button press detected: button NOT CONNECTED (or not pressed)")
                    if not probe.driven:
                        rep.info("if the button is wired to GND, retry with --button-pull up")
                    rep.info(f"auto-detect would use active_{'low' if probe.active_low else 'high'}")
                else:
                    al = pressed == 0
                    rep.line(OK, f"button press detected: pressed level {pressed} -> "
                                 f"gpio.button_active_low = {str(al).lower()}"
                                 + ("" if al == probe.active_low else " (auto-detect would be WRONG: set it)"))
                    rep.suggest["button_active_low"] = al
            finally:
                dev.close()

    # buzzer
    if g.buzzer_gpio is not None:
        try:
            bz = DigitalOutputDevice(g.buzzer_gpio)
            for _ in range(2):
                bz.on()
                time.sleep(0.1)
                bz.off()
                time.sleep(0.15)
            bz.close()
            ans = _ask("Did you hear two beeps? [y/n]: ", interactive)
            if ans == "n":
                rep.line(FAIL, "buzzer silent: check MOSFET wiring")
            else:
                rep.line(OK if ans == "y" else WARN, f"buzzer on GPIO{g.buzzer_gpio} pulsed")
        except Exception as e:  # noqa: BLE001
            rep.line(FAIL, f"buzzer GPIO{g.buzzer_gpio} unavailable ({e})")
    else:
        rep.info("buzzer not configured (gpio.buzzer_gpio = null)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="fallout.nav sensor self-test (run on the device)")
    ap.add_argument("--skip-gps", action="store_true")
    ap.add_argument("--skip-compass", action="store_true")
    ap.add_argument("--skip-gpio", action="store_true")
    ap.add_argument("--non-interactive", action="store_true", help="do not ask questions")
    ap.add_argument("--gps-timeout", type=float, default=15.0)
    ap.add_argument("--samples", type=int, default=50)
    ap.add_argument("--gpsd", default="127.0.0.1:2947")
    ap.add_argument("--button-pull", choices=["auto", "up", "down"], default="auto",
                    help="internal pull for the button test (auto: none if the module drives the line, else down)")
    a = ap.parse_args(argv)
    s = load_settings()
    rep = Report()
    host, _, port = a.gpsd.partition(":")
    interactive = not a.non_interactive and sys.stdin.isatty()
    if not a.skip_gps:
        check_gps(rep, s, host, int(port or 2947), a.gps_timeout)
    if not a.skip_compass:
        check_compass(rep, s, a.samples)
    if not a.skip_gpio:
        check_gpio(rep, s, interactive, button_pull=a.button_pull)
    print("\n=== Summary ===")
    if rep.suggest:
        print("Suggested settings (PATCH /api/settings):")
        print(json.dumps({"gpio": rep.suggest}))
    print("RESULT:", "FAILED" if rep.failed else "PASSED")
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
