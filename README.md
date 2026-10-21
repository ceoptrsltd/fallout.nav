# fallout.nav

**A 2001 black‑and‑white reversing monitor, a Raspberry Pi Compute Module 4 and a wasteland navigator.**

fallout.nav turns a Raspberry Pi CM4 into an in‑car computer for an old van:

- **Dashcam** — OV5647 camera, full 2592×1944 sensor (widest field of view) → hardware H.264 1440×1080 @ 15 fps,
  loop recording in 10‑minute MPEG‑TS segments with `fsync` every second: a power cut loses ≤ 2 s of video.
- **Navigation screen in a Fallout / Mad Max style** on a 5″ monochrome CRT (WAECO Mobitronic RV‑50):
  Esri satellite imagery with streets, heading‑up rotation (the vehicle icon turns first, the map catches up),
  zoom by speed, the camera view when stopped, three compass designs, a wasteland muscle‑car icon.
- **Sensors** — Holybro M8N GPS (u‑blox NEO‑M8N, 10 Hz, configured over UBX, Static Hold) and IST8310 compass
  with **automatic calibration against the GPS course while driving**; heading fusion compass ↔ GPS.
- **Web UI** (laptop over Wi‑Fi) — dashboard, live camera (WebRTC), recordings with in‑browser playback,
  navigation/compass/GPS/system settings, and a **1:1 physical‑size preview** of the car screen.
- **Composite video straight from the SoC** — a wire on CM4 pin 111 (`VDAC_COMP`), NTSC 480i/60 Hz with output
  margins and mirroring tuned for the RV‑50. The button on the GPS module switches the video output off so it
  does not bleed into the monitor's other camera input.

Project page with a live demo: **https://ptrs.ltd/apps/fallout.nav/**

## Hardware

| Part | Notes |
|---|---|
| Raspberry Pi CM4 (eMMC, Wi‑Fi, 4 GB) on Waveshare **CM4‑NANO‑B** | Raspberry Pi OS (Debian 13 trixie) |
| OV5647 camera | CSI0, `dtoverlay=ov5647,cam0` |
| Holybro M8N GPS + IST8310 compass (10‑pin JST‑GH) | UART `/dev/ttyAMA0` + I2C‑1 |
| WAECO Mobitronic RV‑50 monitor | composite input, B/W CRT |

### GPS module → 40‑pin header

| GH pin | Signal | → | 40‑pin | Pi signal |
|---|---|---|---|---|
| 1 | VCC 5V | → | 2 | 5V |
| 2 | RX (GPS input) | → | 8 | GPIO14 TXD |
| 3 | TX (GPS output) | → | 10 | GPIO15 RXD |
| 4 | SCL | → | 5 | GPIO3 SCL |
| 5 | SDA | → | 3 | GPIO2 SDA |
| 6 | SAFETY_SWITCH | → | 16 | GPIO23 — short press: video output on/off, long press: protect recording |
| 7 | SAFETY_SWITCH_LED | → | 18 | GPIO24 — blinks while recording |
| 8 | VDD_3V3 | → | 1 | 3.3V |
| 10 | GND | → | 6 | GND |

### Composite video

CM4 connector J2, **pin 111 `VDAC_COMP`** (inner/odd row, 6th from the pin‑101 end; its neighbours are
109 `PCIe_nRST` and 113 GND — "ground immediately on the far side" is the reliable check) → video pin of the
monitor input, GND → its ground. See `docs/device-report.md`.

## Software

```
fnav/core     FastAPI backend: auth, settings, 10 Hz WebSocket telemetry, nav logic, Esri tile cache, recordings, system
fnav/camera   Picamera2 pipeline, crash-safe MPEG-TS recorder, RTSP to MediaMTX (WebRTC), MJPEG for the screen
fnav/sensors  gpsd client, UBX configuration, IST8310 driver, calibration + GPS auto-calibration, heading fusion, GPIO
web/display   the car screen (Canvas renderer for Esri tiles + Pip-Boy HUD), also runs as an in-browser demo (?demo=1)
web/admin     the web UI
pi/           install.sh, deploy.sh, systemd units, boot config, MediaMTX, sudoers/polkit
site/         the project page
```

Services on the device: `fnav-core` (port 80), `fnav-camera`, `mediamtx`, `fnav-display` (cage + Chromium kiosk),
`gpsd`, `chrony`.

### Install on the device

```bash
pi/deploy.sh --stage            # from a PC (Git Bash / Linux): upload the repo to the device
ssh <device> 'cd ~/fallout.nav && sudo ./pi/install.sh && sudo reboot'
pi/deploy.sh                    # later updates
```

### Develop on a PC (no hardware)

```bash
pip install -e .[dev]
python -m fnav.core --mock --port 8000     # http://127.0.0.1:8000/admin/  (password: fnav-dev)
python -m fnav.camera --fake               # synthetic camera + recorder
python -m pytest                           # Python tests
node --test tests/display/*.test.mjs       # display tests
```

## How it was built

The project was designed and written in about a day by Claude acting as lead/integrator with seven parallel
sub‑agents (backend, map renderer, HUD design, web UI, camera/recorder, sensors, system), coordinated through
written contracts in `docs/contracts/`. Plan and decisions: `PLAN.md`; hardware bring‑up notes:
`docs/device-report.md`.

Map imagery © Esri and its data providers (Esri, Maxar, Earthstar Geographics and the GIS user community).

## License

MIT — see [LICENSE](LICENSE). Third‑party parts keep their own licenses: fonts in `web/display/ui/fonts/`
(SIL OFL 1.1, see `LICENSES.md` there) and `web/admin/vendor/mpegts.js` (Apache‑2.0).
