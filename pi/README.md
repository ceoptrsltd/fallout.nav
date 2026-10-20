# fallout.nav: Raspberry Pi system layer (`pi/`)

Turns a Raspberry Pi OS (Debian 13 "trixie", 64-bit) on a CM4 + Waveshare CM4-NANO-B into the fnav device:
dashcam recorder, navigation display on HDMI (720x576 @ 50 Hz -> HDMI->AV converter -> 5" B/W monitor) and web UI.

## What is in here

| Path | Purpose |
|---|---|
| `install.sh` | one-shot idempotent installer (run on the device as root) |
| `deploy.sh` | PC -> device code deploy (Git Bash / Linux / macOS; tar over ssh, no rsync needed) |
| `boot/config.txt.fnav` | block placed between `# >>> fnav` / `# <<< fnav` in `/boot/firmware/config.txt` |
| `boot/cmdline.txt.fnav` | extra kernel command line tokens (HDMI mode, EDID override, console blanking) |
| `tools/gen-edid.py` | generates the fixed 720x576@50 EDID (`/lib/firmware/edid/fnav-576p50.bin`) |
| `systemd/` | `fnav-core`, `fnav-camera`, `mediamtx`, `fnav-display` units; drop-ins (watchdog, journald, seatd, NM wait-online) |
| `mediamtx.yml` | MediaMTX config (`/etc/fnav/mediamtx.yml`) |
| `sysctl/90-fnav.conf` | fast dirty-page writeback (video survives power loss) |
| `sudoers/fnav`, `polkit/` | the only privileges the `fnav` user gets |
| `gpsd/`, `chrony/` | gpsd on `/dev/ttyAMA0`, chrony with NTP + GPS (SHM) time |
| `udev/`, `networkmanager/`, `chromium/`, `initramfs/` | small supporting files |
| `bin/` | helpers run on the device: `fnav-sync-system`, `fnav-pip-sync`, `fnav-deploy-remote`, `fnav-display-prestart` |

Layout on the device: code `/opt/fnav/app`, venv `/opt/fnav/venv` (with `--system-site-packages`), data
`/var/lib/fnav`, secrets and config `/etc/fnav`, MediaMTX binary `/usr/local/bin/mediamtx`, service user `fnav`.

## Installing from scratch

Prerequisites: the device boots Raspberry Pi OS (trixie, 64-bit), is on the network (Wi-Fi `PhoneHotspot` or Ethernet), has
internet access (apt, pip, GitHub for MediaMTX), your ssh key is installed, and the login user has passwordless sudo.
The full desktop image is fine: the installer disables the desktop (see below).

1. **Upload the repo** (from the PC, Git Bash):
   ```bash
   export FNAV_HOST=172.23.204.16 FNAV_USER=falloutnav      # or an ssh alias, e.g. FNAV_HOST=fnav
   ./pi/deploy.sh --stage                                     # uploads to ~/fallout.nav on the device
   ```
2. **Install** (on the device, interactive: asks for the web UI password):
   ```bash
   ssh -t $FNAV_USER@$FNAV_HOST 'cd ~/fallout.nav && sudo ./pi/install.sh'
   ```
   Shortcut doing both: `./pi/deploy.sh --install` (needs a real terminal; in mintty prefix with `winpty`).
   Optional environment: `FNAV_PASSWORD` (non-interactive), `FNAV_HOSTNAME=fnav` (so `http://fnav.local` works),
   `FNAV_TIMEZONE=Europe/Warsaw`, `MEDIAMTX_VERSION`, `FNAV_WIFI_SSID` + `FNAV_WIFI_PSK`.
3. **Reboot**: `sudo reboot`. The boot configuration (UART, I2C, camera overlay, KMS, HDMI mode) only applies after it.
4. Open `http://<device-ip>/` (or `http://fnav.local/`), log in with the password from step 2.

The installer log is `/var/log/fnav-install.log`. Re-running `install.sh` is safe: it re-syncs the code, replaces the
`# >>> fnav` block instead of appending, never duplicates cmdline tokens, keeps `/etc/fnav/secrets.env` (the password is
asked only when that file does not exist). Change the password later in the web UI, or:
`echo -n 'new password' | sudo -u fnav /opt/fnav/venv/bin/python -m fnav.core.passwd --secrets /etc/fnav/secrets.env`
(run from `/opt/fnav/app` with `PYTHONPATH=/opt/fnav/app`).

### What the installer does

1. apt packages: picamera2 + rpicam-apps, numpy, pyserial, lgpio, gpiozero, python3-gps, gpsd, chrony, ffmpeg, i2c-tools,
   **cage**, seatd, **chromium** (the package is called `chromium` on trixie; `chromium-browser` is only a fallback),
   avahi, rsync, curl, polkit, fonts.
2. Disables the desktop: `systemctl disable lightdm`, `set-default multi-user.target`, so tty1/HDMI belong to `fnav-display`.
3. User `fnav` (system user, no login) in groups video, render, i2c, gpio, dialout, systemd-journal, netdev, input, tty.
4. Code to `/opt/fnav/app` (rsync, excluding `.git`, `.devdata`, `tests`), venv, `pip install` from `pyproject.toml`
   (+ `smbus2 pygeomag pyubx2` from the `pi` extra; `gps/lgpio/gpiozero/numpy` come from apt).
5. Web UI password via `python -m fnav.core.passwd` (stdin only) -> `/etc/fnav/secrets.env` (0600, owner `fnav`).
   `/etc/fnav` is owned by `fnav` (0750) so the web UI can replace the file atomically when the password is changed.
6. MediaMTX: fixed version, sha256 verified against the release's `checksums.sha256`, skipped if already installed.
7. Boot files, systemd units, sysctl, sudoers (checked with `visudo -c` first), polkit, udev, gpsd, chrony, services enabled.
8. Prints a summary and whether a reboot is needed.

## Boot configuration (why each thing is there)

`config.txt` block (`boot/config.txt.fnav`, every line is commented in the file):

- `enable_uart=1` + `dtoverlay=disable-bt`: the full PL011 UART `/dev/ttyAMA0` on GPIO14/15 for the GPS (and no Bluetooth).
- `dtparam=i2c_arm=on` (+ `i2c-dev` module): compass IST8310 on I2C-1.
- `camera_auto_detect=0` + `dtoverlay=ov5647,cam0`: the OV5647 on CSI0.
- `dtoverlay=vc4-kms-v3d,cma-256`: KMS driver for cage/Chromium, 256 MB CMA for camera buffers; `gpu_mem=128` for the H.264 encoder.
- `disable_fw_kms_setup=1`: the firmware must not pick/override the video mode.
- `hdmi_force_hotplug=1`, `hdmi_group=1`, `hdmi_mode=17`: legacy settings for 720x576p50 (ignored by KMS, harmless).
- `enable_tvout=0`, `dtparam=audio=off`: no composite output, no analog audio.
- USB: the stock `[cm4] otg_mode=1` (xHCI host) already gives USB host on the NANO-B. `dtoverlay=dwc2,dr_mode=host`
  (Waveshare wiki) is an **alternative that excludes `otg_mode=1`**, so it is shipped commented out; use it only if USB-A
  does not work, and comment out `otg_mode=1` at the same time.

`cmdline.txt` (single line; the installer removes `console=serial0,115200` / `ttyAMA0` / `ttyS0` consoles and `splash`, then adds):

- `video=HDMI-A-1:720x576@50D`: with KMS the `hdmi_mode` lines are not enough. `D` forces the connector on and digital
  **even with nothing connected**, so cage always has an output.
- `drm.edid_firmware=HDMI-A-1:edid/fnav-576p50.bin`: an HDMI->AV converter often provides no EDID, and without EDID the kernel
  computes a CVT mode for 720x576 (not the standard 27 MHz CEA timing, which the converter may refuse). The installer generates
  a 256-byte EDID with CEA VIC 17 as the only mode (`tools/gen-edid.py`, self-checked) and puts it in `/lib/firmware/edid/`
  (and into the initramfs, if `initramfs-tools` is installed).
- `consoleblank=0 vt.global_cursor_default=0`: no console blanking, no blinking cursor.

The serial login consoles are masked (`serial-getty@ttyAMA0`, `serial-getty@serial0`), as the UART belongs to the GPS.
The originals are saved once as `config.txt.fnav-orig`, `cmdline.txt.fnav-orig` (same directory), `gpsd.fnav-orig`, `chrony.conf.fnav-orig`.

## Time (gpsd + chrony)

There is no RTC. chrony takes time from NTP when the phone hotspot is up (primary source) and from the GPS (NMEA through gpsd's
SHM 0, no PPS, accuracy ~0.1-0.5 s) when it is not. `makestep 1 -1` lets it step the clock at any moment (a GPS fix may
come minutes after boot); the stock `makestep 1 3` line in `/etc/chrony/chrony.conf` is commented out because it would override it.
Check: `chronyc sources -v`, `chronyc tracking`, `timedatectl show -p NTPSynchronized`. The `offset 0.15` of the GPS source is a starting
value: if `chronyc sourcestats` shows a steady offset of the GPS source against NTP, adjust `pi/chrony/fnav.conf`.

## Checking that it works

```bash
systemctl status fnav-core fnav-camera mediamtx fnav-display gpsd chrony --no-pager
journalctl -u fnav-core -b --no-pager | tail -50            # also: -u fnav-camera, -u mediamtx, -u fnav-display
systemctl is-active fnav-core fnav-camera mediamtx fnav-display
curl -s http://127.0.0.1/api/session                         # {"authenticated": false}
curl -s http://127.0.0.1:8081/status                         # camera status
ss -ltnup | grep -E ':(80|8081|8554|8889|8189|2947)\b'
ls /dev/ttyAMA0 /dev/i2c-1 /dev/gpiochip0 && i2cdetect -y 1  # expect 0x0e
gpsmon /dev/ttyAMA0   # or: cgps -s
rpicam-hello --list-cameras
chronyc sources -v
cat /boot/firmware/cmdline.txt; sudo cat /sys/class/drm/card*-HDMI-A-1/status   # "connected" (forced)
modetest -c 2>/dev/null | grep -A3 HDMI                      # (libdrm-tests) 720x576 mode
sudo sysctl vm.dirty_expire_centisecs vm.dirty_writeback_centisecs
sudo -u fnav sudo -n -l                                      # lists only the allowed systemctl commands
```

If the screen stays black: `journalctl -u fnav-display -b`; run `sudo systemctl restart fnav-display`; common causes are
the page not served yet (the unit waits up to 120 s for `http://127.0.0.1/display/`), seatd not running (`systemctl status seatd`),
or a wrong HDMI mode (`dmesg | grep -i -E 'vc4|drm|edid'`). Extra Chromium flags can be tried without editing the unit:
set `FNAV_CHROMIUM_EXTRA=...` in `/etc/fnav/display.env` (e.g. `--use-gl=angle --use-angle=gles`) and restart `fnav-display`.
If `cage` cannot get a seat through seatd on your image, the alternative is a logind session: replace `Requires=seatd.service` /
`LIBSEAT_BACKEND=seatd` in the unit with `PAMName=login` (see the cage wiki, "Running Cage on systemd").

## Day-to-day deployment from the PC

```bash
export FNAV_HOST=172.23.204.16 FNAV_USER=falloutnav
./pi/deploy.sh                          # code + system files + deps (if pyproject.toml changed) + restart fnav-core/camera/display
./pi/deploy.sh --only "fnav-core"       # restart only core
./pi/deploy.sh --no-restart             # just copy
./pi/deploy.sh --dry-run                # show what would be sent
```

`deploy.sh` needs key-based ssh and passwordless `sudo` for the login user; the upload is a tar stream (no rsync on Windows).
Restarting `fnav-camera` closes the current recording segment cleanly and starts a new one.

## Rollback

```bash
sudo ./pi/install.sh --rollback-boot   # restore config.txt and cmdline.txt from the .fnav-orig backups, then reboot
# Remove the services (data in /var/lib/fnav and the secrets are kept):
sudo systemctl disable --now fnav-display fnav-camera fnav-core mediamtx
sudo rm /etc/systemd/system/{fnav-core,fnav-camera,fnav-display,mediamtx}.service
sudo rm /etc/systemd/system/{seatd.service.d,NetworkManager-wait-online.service.d}/fnav.conf
sudo rm /etc/sudoers.d/fnav /etc/polkit-1/rules.d/50-fnav-networkmanager.rules /etc/udev/rules.d/90-fnav.rules \
        /etc/sysctl.d/90-fnav.conf /etc/chrony/conf.d/fnav.conf /etc/NetworkManager/conf.d/90-fnav-wifi.conf \
        /etc/systemd/system.conf.d/90-fnav-watchdog.conf /etc/systemd/journald.conf.d/90-fnav.conf
sudo cp /etc/default/gpsd.fnav-orig /etc/default/gpsd; sudo cp /etc/chrony/chrony.conf.fnav-orig /etc/chrony/chrony.conf
sudo systemctl daemon-reload
# Bring the desktop back:
sudo systemctl enable lightdm && sudo systemctl set-default graphical.target
sudo systemctl unmask serial-getty@ttyAMA0.service serial-getty@serial0.service
# Full wipe (recordings and settings!): sudo rm -rf /opt/fnav /var/lib/fnav /etc/fnav; sudo userdel fnav
```

## Notes and assumptions

- `fnav-core` is started as `python -m fnav.core` and relies on its defaults (`0.0.0.0:80`); `fnav-camera` as `python -m fnav.camera`.
- `fnav-core` runs without `NoNewPrivileges`/capability bounding because it calls `sudo -n systemctl reboot|poweroff` and `nmcli`.
- The kiosk page should set `cursor: none`; cage also draws no cursor without a pointer device.
- MediaMTX version is pinned in `install.sh` (`MEDIAMTX_VERSION`); `mediamtx.yml` is written for v1.12.x and MediaMTX refuses unknown keys, so
  after bumping the version check `journalctl -u mediamtx`. The checksum protects against corrupted downloads, not against a compromised release.
- Hardware watchdog: `RuntimeWatchdogSec=10s` (systemd resets the board if PID 1/the kernel hangs). Memory limits are set for core, mediamtx
  and display; the camera is deliberately not memory-limited and has `OOMScoreAdjust=-900`. If cgroup memory accounting is disabled on your
  kernel command line the limits are simply not enforced.
