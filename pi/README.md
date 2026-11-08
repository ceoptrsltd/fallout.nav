# fallout.nav: Raspberry Pi system layer (`pi/`)

Turns a Raspberry Pi OS (Debian 13 "trixie", 64-bit) on a CM4 + Waveshare CM4-NANO-B into the fnav device:
dashcam recorder (front + rear camera), navigation display on composite (NTSC 480i/60 straight from the CM4's
pin 111 into a 5" B/W CRT), alarm, radio and web UI.

## What is in here

| Path | Purpose |
|---|---|
| `install.sh` | one-shot idempotent installer (run on the device as root) |
| `deploy.sh` | PC -> device code deploy (Git Bash / Linux / macOS; tar over ssh, no rsync needed) |
| `boot/config.txt.fnav` | block placed between `# >>> fnav` / `# <<< fnav` in `/boot/firmware/config.txt` |
| `boot/cmdline.txt.fnav` | extra kernel command line tokens (composite mode and margins, console blanking) |
| `tools/gen-edid.py` | a 720x576@50 EDID for the alternative HDMI->AV converter setup (unused with composite) |
| `systemd/` | `fnav-core`, `fnav-camera`, `fnav-camera-rear`, `fnav-watch`, `fnav-display`, `fnav-radio`, `fnav-games`, `fnav-tunnel`, `fnav-bt-reset`, `mediamtx` units; drop-ins (watchdog, journald, seatd, bluealsa, NM wait-online) |
| `mediamtx.yml` | MediaMTX config (`/etc/fnav/mediamtx.yml`) |
| `sysctl/90-fnav.conf` | fast dirty-page writeback (video survives power loss) |
| `sudoers/fnav`, `polkit/` | the only privileges the `fnav` user gets (polkit: NetworkManager, the system time zone) |
| `gpsd/`, `chrony/` | gpsd on `/dev/gps0` (UART5), chrony with NTP + GPS (SHM) time |
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
   `FNAV_TIMEZONE=Europe/Warsaw` (the starting zone: later it follows the van, see "Time"), `MEDIAMTX_VERSION`,
   `FNAV_WIFI_SSID` + `FNAV_WIFI_PSK`.
3. **Reboot**: `sudo reboot`. The boot configuration (UART, I2C, PWM, camera overlay, KMS, composite mode) only applies after it.
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
2. Disables the desktop: `systemctl disable lightdm`, `set-default multi-user.target`, so tty1 and the video output belong to `fnav-display`.
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

- `enable_uart=0` + `dtoverlay=uart5`: the GPS on the full PL011 UART5 (GPIO12/13, pins 32/33; udev names it `/dev/gps0`).
  The primary UART0 stays with the on-board Bluetooth, which plays the radio on a Bluetooth speaker.
- `dtparam=i2c_arm=on` (+ `i2c-dev` module): compass IST8310 on I2C-1.
- `dtoverlay=i2c3,pins_4_5`: MPU-6050 shock / level sensor on its own I2C-3 (GPIO4/5, pins 7/29).
- `dtoverlay=pwm,pin=19,func=2`: hardware PWM1 on GPIO19 (pin 35) for the hazard-lights RC relay switch.
- `camera_auto_detect=0` + `dtoverlay=ov5647,cam0`: the OV5647 on CSI0.
- `dtoverlay=vc4-kms-v3d,cma-256,composite` + `enable_tvout=1`: KMS with the composite encoder (on a CM4 this switches
  HDMI off), 256 MB CMA for camera buffers; `gpu_mem=128` for the H.264 encoder; `disable_fw_kms_setup=1`: the mode
  comes only from `cmdline.txt`.
- `core_freq_min=500`: a fixed core clock; dynamic core scaling disturbs the composite picture every few seconds.
- `dtparam=audio=off`: no analog audio (also frees the PWM block).
- `dtoverlay=dwc2,dr_mode=host`: USB through the DWC2 controller. The internal xHCI of the stock `[cm4] otg_mode=1`
  dies ~10 s after the macro pad enumerates ("HC died"); the two are mutually exclusive, so the installer comments
  out `otg_mode=1`.

`cmdline.txt` (single line; the installer removes `console=serial0,115200` / `ttyAMA0` / `ttyS0` consoles and `splash`, then adds):

- `video=Composite-1:720x480@60ie,tv_mode=NTSC,margin_left=62,margin_right=86,margin_top=26,margin_bottom=26`:
  NTSC 480i/60, because the RV-50 holds vertical sync at 60 Hz while PAL 576i/50 jumped. At 60 Hz the monitor shows
  only the middle ~80 % x 90 %, so the margins shrink the picture into that area (measured with
  `test-pattern.html?mode=frame`). The display page scales its 768x576 canvas to 720x480.
- `consoleblank=0 vt.global_cursor_default=0`: no console blanking, no blinking cursor.

The serial login consoles are masked (`serial-getty@ttyAMA0`, `serial-getty@serial0`): there is no serial console,
UART0 belongs to the Bluetooth and UART5 to the GPS.
The originals are saved once as `config.txt.fnav-orig`, `cmdline.txt.fnav-orig` (same directory), `gpsd.fnav-orig`, `chrony.conf.fnav-orig`.

## Time (gpsd + chrony)

There is no RTC. chrony takes time from NTP when the phone hotspot is up (primary source) and from the GPS (NMEA through gpsd's
SHM 0, no PPS, accuracy ~0.1-0.5 s) when it is not. `makestep 1 -1` lets it step the clock at any moment (a GPS fix may
come minutes after boot); the stock `makestep 1 3` line in `/etc/chrony/chrony.conf` is commented out because it would override it.
Check: `chronyc sources -v`, `chronyc tracking`, `timedatectl show -p NTPSynchronized`. The `offset 0.15` of the GPS source is a starting
value: if `chronyc sourcestats` shows a steady offset of the GPS source against NTP, adjust `pi/chrony/fnav.conf`.

The time zone follows the van (recording names, push texts and the screen clock are local time): when the weather
forecast for the GPS position names another zone twice in a row, `fnav-core` runs `timedatectl set-timezone` itself,
allowed by `polkit/50-fnav-timedate.rules` (only `org.freedesktop.timedate1.set-timezone`, only for `fnav`; installed
by `fnav-sync-system`, so by every `deploy.sh`; polkitd picks it up without a restart). Switch it off in the web UI
(System -> "Time zone follows the van"). Check: `timedatectl show -p Timezone`, `journalctl -u fnav-core | grep 'time zone'`,
`sudo -u fnav timedatectl --no-ask-password set-timezone Europe/Warsaw` (must not ask for a password).

## Checking that it works

```bash
systemctl status fnav-core fnav-camera fnav-camera-rear fnav-watch mediamtx fnav-display fnav-radio fnav-tunnel gpsd chrony --no-pager
journalctl -u fnav-core -b --no-pager | tail -50            # also: -u fnav-camera, -u fnav-watch, -u mediamtx, -u fnav-display
systemctl is-active fnav-core fnav-camera fnav-watch mediamtx fnav-display
curl -s http://127.0.0.1/api/session                         # {"authenticated": false}
curl -s http://127.0.0.1:8081/status                         # camera status
curl -s http://127.0.0.1:8082/status                         # alarm vision (fnav-watch): watching only while armed
ss -ltnup | grep -E ':(80|8081|8554|8889|8189|2947)\b'
ls /dev/gps0 /dev/i2c-1 /dev/i2c-3 /dev/gpiochip0 && i2cdetect -y 1  # expect 0x0e; i2cdetect -y 3: 0x68
gpsmon /dev/gps0      # or: cgps -s
rpicam-hello --list-cameras
chronyc sources -v
cat /boot/firmware/cmdline.txt; sudo cat /sys/class/drm/card*-Composite-1/status   # "connected"
modetest -c 2>/dev/null | grep -A3 Composite                 # (libdrm-tests) 720x480 mode
sudo sysctl vm.dirty_expire_centisecs vm.dirty_writeback_centisecs
sudo -u fnav sudo -n -l                                      # lists only the allowed systemctl commands
```

If the screen stays black: `journalctl -u fnav-display -b`; run `sudo systemctl restart fnav-display`; common causes are
the page not served yet (the unit waits up to 120 s for `http://127.0.0.1/display/`), seatd not running (`systemctl status seatd`),
or a wrong video mode (`dmesg | grep -i -E 'vc4|drm'`). Extra Chromium flags can be tried without editing the unit:
set `FNAV_CHROMIUM_EXTRA=...` in `/etc/fnav/display.env` (e.g. `--use-gl=angle --use-angle=gles`) and restart `fnav-display`.
If `cage` cannot get a seat through seatd on your image, the alternative is a logind session: replace `Requires=seatd.service` /
`LIBSEAT_BACKEND=seatd` in the unit with `PAMName=login` (see the cage wiki, "Running Cage on systemd").

## Day-to-day deployment from the PC

```bash
export FNAV_HOST=172.23.204.16 FNAV_USER=falloutnav
./pi/deploy.sh                          # code + system files + deps (if pyproject.toml changed) + restart fnav-core/camera/watch/display
./pi/deploy.sh --only "fnav-core"       # restart only core
./pi/deploy.sh --no-restart             # just copy
./pi/deploy.sh --dry-run                # show what would be sent (and what is left out)
FNAV_RESTART="fnav-core fnav-watch" ./pi/deploy.sh   # same as --only; --only / --no-restart win over it
```

`deploy.sh` needs key-based ssh and passwordless `sudo` for the login user; the upload is a tar stream (no rsync on Windows).
It packs the files git tracks, with their working-tree content (uncommitted edits to tracked files deploy too), plus
the generated `web/admin/version.json`; untracked files stay on the PC and are listed ("Not deployed: untracked here"),
so `git add` a new file before deploying it. `/opt/fnav/app` mirrors the pack: files that are no longer in it are
removed there.
Restarting `fnav-camera` closes the current recording segment cleanly and starts a new one.
A unit added after the first install (`fnav-watch`) is enabled by the deploy itself; `install.sh` enables all of them.

The pack carries `web/admin/version.json` (written by `tools/web-version.sh`, not in git): an admin already open on a
phone notices the new version and shows "A new version is available" with a Reload button (docs/contracts/remote.md,
section 6). The remote admin (`<server>/fnadmin/`) is served from a separate copy of `web/` on the server: after a
regular deploy `deploy.sh` updates it too (`tools/remote.sh web`) when `tools/.remote.env` exists on this PC, and only
prints a reminder otherwise; `FNAV_WEB_COPY=no` skips it. `tools/remote.sh deploy` (through the server) uploads it itself.

## Rollback

```bash
sudo ./pi/install.sh --rollback-boot   # restore config.txt and cmdline.txt from the .fnav-orig backups, then reboot
# Remove the services (data in /var/lib/fnav and the secrets are kept):
sudo systemctl disable --now fnav-display fnav-games fnav-radio fnav-tunnel fnav-watch fnav-camera fnav-camera-rear fnav-bt-reset fnav-core mediamtx
sudo rm /etc/systemd/system/{fnav-core,fnav-camera,fnav-camera-rear,fnav-watch,fnav-display,fnav-games,fnav-radio,fnav-tunnel,fnav-bt-reset,mediamtx}.service
sudo rm /etc/systemd/system/{seatd.service.d,NetworkManager-wait-online.service.d}/fnav.conf
sudo rm /etc/sudoers.d/fnav /etc/polkit-1/rules.d/50-fnav-{networkmanager,timedate}.rules /etc/udev/rules.d/90-fnav.rules \
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
- `fnav-watch` (camera on alarm duty, `python -m fnav.watch`, `127.0.0.1:8082`) runs with `CPUWeight=200`, `Nice=0`
  (above the screen, below the recorder's Nice -5): while armed it is the alarm and must get its core even when the
  screen is busy; disarmed it sleeps. One inference thread. Frames come from
  `fnav-camera` through `/dev/shm/fnav-lores` (copied only while fnav-watch is watching, i.e. while the alarm is armed and
  "camera watch" is on). Not watching it sleeps (no frames, no models loaded). Models: `fnav/watch/models/` (OpenCV DNN,
  `opencv-python-headless` from the `pi` extra). Tuning view: `curl -o d.jpg http://127.0.0.1:8082/debug.jpg`, numbers:
  `curl http://127.0.0.1:8082/debug.json`; offline: `python -m fnav.watch.replay <recording.ts> --out /tmp/w`.
- The kiosk page should set `cursor: none`; cage also draws no cursor without a pointer device.
- MediaMTX version is pinned in `install.sh` (`MEDIAMTX_VERSION`); `mediamtx.yml` is written for v1.12.x and MediaMTX refuses unknown keys, so
  after bumping the version check `journalctl -u mediamtx`. The checksum protects against corrupted downloads, not against a compromised release.
- Hardware watchdog: `RuntimeWatchdogSec=10s` (systemd resets the board if PID 1/the kernel hangs). Memory limits are set for core, mediamtx
  and display; the camera is deliberately not memory-limited and has `OOMScoreAdjust=-900`. If cgroup memory accounting is disabled on your
  kernel command line the limits are simply not enforced.
