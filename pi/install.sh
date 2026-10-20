#!/usr/bin/env bash
# fallout.nav installer: turns a Raspberry Pi OS (Debian 13 trixie, 64-bit) on a CM4 into a fnav device.
#
# Run from the root of the copied repository:   sudo ./pi/install.sh
# Idempotent: it is safe to run it again (updates the app and replaces managed blocks, never duplicates lines).
# Log: /var/log/fnav-install.log
#
# Environment (all optional):
#   FNAV_PASSWORD        web UI password (otherwise asked interactively when /etc/fnav/secrets.env does not exist)
#   MEDIAMTX_VERSION     MediaMTX release to install (default: see below)
#   FNAV_HOSTNAME        set the hostname (e.g. "fnav" so that http://fnav.local works); not touched if empty
#   FNAV_TIMEZONE        set the time zone, e.g. Europe/Warsaw (recording file names use local time)
#   FNAV_WIFI_SSID, FNAV_WIFI_PSK   create an autoconnect Wi-Fi profile (when none with that name exists)
#   FNAV_SKIP_APT=1      do not run apt (offline re-run)
#
# Options:
#   --rollback-boot      restore config.txt / cmdline.txt from the .fnav-orig backups and exit
#   -h, --help
set -Eeuo pipefail

MEDIAMTX_VERSION="${MEDIAMTX_VERSION:-1.21.2}"
LOG=/var/log/fnav-install.log
BOOT_DIR=/boot/firmware
[ -d "$BOOT_DIR" ] || BOOT_DIR=/boot
APP=/opt/fnav/app
VENV=/opt/fnav/venv
SECRETS=/etc/fnav/secrets.env
EDID=/lib/firmware/edid/fnav-576p50.bin
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

NEED_REBOOT=0
WARNINGS=()

log()  { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
step() { printf '\n[%s] ==> %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '[%s] WARNING: %s\n' "$(date +%H:%M:%S)" "$*" >&2; WARNINGS+=("$*"); }
die()  { printf '[%s] ERROR: %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 1; }
trap 'printf "ERROR: command failed at line %s (see %s)\n" "$LINENO" "$LOG" >&2' ERR

usage() { sed -n '2,/^set -Eeuo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; }

ROLLBACK=0
case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    --rollback-boot) ROLLBACK=1 ;;
    "") ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
esac

[ "$(id -u)" -eq 0 ] || die "run as root: sudo ./pi/install.sh"

install -d -m 0755 "$(dirname "$LOG")"
touch "$LOG"; chmod 0640 "$LOG"
exec > >(tee -a "$LOG") 2>&1
log "fallout.nav installer started (repo: $REPO)"

# ------------------------------------------------------------------------------------------
backup_once() { # backup_once FILE: keep the very first original as FILE.fnav-orig
    [ -f "$1" ] && [ ! -e "$1.fnav-orig" ] && cp -p "$1" "$1.fnav-orig" && log "backup: $1.fnav-orig"
    return 0
}

# (no "grep -q" after a pipe: with pipefail an early-exiting grep can turn the pipeline into a failure)
unit_exists() { [ -n "$(systemctl list-unit-files "$1" 2>/dev/null | grep "^$1" || true)" ]; }

if [ "$ROLLBACK" -eq 1 ]; then
    step "Restoring boot files from backups"
    for f in "$BOOT_DIR/config.txt" "$BOOT_DIR/cmdline.txt"; do
        if [ -f "$f.fnav-orig" ]; then cp -p "$f.fnav-orig" "$f"; log "restored $f"; else warn "no backup for $f"; fi
    done
    log "Reboot to apply."
    exit 0
fi

# ------------------------------------------------------------------------------------------
step "Checking the system"
[ "$(uname -m)" = "aarch64" ] || die "64-bit ARM (aarch64) is required, this is $(uname -m)"
[ -f "$REPO/pyproject.toml" ] && [ -d "$REPO/pi" ] || die "run this script from a full copy of the repository"
# shellcheck disable=SC1091
. /etc/os-release
log "OS: ${PRETTY_NAME:-unknown}"
[ "${VERSION_CODENAME:-}" = "trixie" ] || warn "tested on Debian 13 (trixie), this is '${VERSION_CODENAME:-?}'"
command -v systemctl >/dev/null || die "systemd is required"

# ------------------------------------------------------------------------------------------
# Password: ask first so the long steps below can run unattended.
PASSWORD=""
if [ -s "$SECRETS" ]; then
    log "$SECRETS exists - keeping the current web UI password"
else
    step "Web UI password"
    PASSWORD="${FNAV_PASSWORD:-}"
    unset FNAV_PASSWORD
    if [ -z "$PASSWORD" ]; then
        [ -t 0 ] || die "no TTY: set FNAV_PASSWORD or run interactively (sudo -E FNAV_PASSWORD=... ./pi/install.sh)"
        for _ in 1 2 3; do
            IFS= read -r -s -p "Choose the web UI password (min 6 characters): " p1 < /dev/tty; echo
            IFS= read -r -s -p "Repeat the password: " p2 < /dev/tty; echo
            if [ "${#p1}" -lt 6 ]; then echo "Too short."; continue; fi
            if [ "$p1" != "$p2" ]; then echo "Passwords do not match."; continue; fi
            PASSWORD="$p1"; break
        done
        unset p1 p2
        [ -n "$PASSWORD" ] || die "no valid password entered"
    fi
    [ "${#PASSWORD}" -ge 6 ] || die "the password must be at least 6 characters"
fi

# ------------------------------------------------------------------------------------------
step "Hostname, time zone"
if [ -n "${FNAV_HOSTNAME:-}" ] && [ "$(hostname)" != "$FNAV_HOSTNAME" ]; then
    if grep -q '^127\.0\.1\.1' /etc/hosts; then
        sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$FNAV_HOSTNAME/" /etc/hosts
    else
        printf '127.0.1.1\t%s\n' "$FNAV_HOSTNAME" >> /etc/hosts
    fi
    hostnamectl set-hostname "$FNAV_HOSTNAME"
    log "hostname set to $FNAV_HOSTNAME"
else
    [ "$(hostname)" = "fnav" ] || log "hostname is '$(hostname)'; set FNAV_HOSTNAME=fnav if you want http://fnav.local"
fi
if [ -n "${FNAV_TIMEZONE:-}" ]; then timedatectl set-timezone "$FNAV_TIMEZONE"; fi
log "time zone: $(timedatectl show -p Timezone --value 2>/dev/null || echo unknown)"

# ------------------------------------------------------------------------------------------
step "Disabling the desktop (tty1/HDMI belong to fnav-display)"
if unit_exists lightdm.service; then
    systemctl disable lightdm.service 2>/dev/null || true
    log "lightdm disabled"
fi
if [ "$(systemctl get-default)" != "multi-user.target" ]; then
    systemctl set-default multi-user.target
    log "default target: multi-user.target"
fi
# Desktop leftovers of the full Raspberry Pi OS image: printing, desktop automount/accounts/power,
# VNC, GPU test, colour management, PackageKit. Masked so D-Bus activation cannot start them either.
DESKTOP_SYSTEM_UNITS=(cups.service cups.socket cups-browsed.service accounts-daemon.service udisks2.service
    upower.service colord.service packagekit.service wayvnc-control.service wayvnc.service glamor-test.service)
for u in "${DESKTOP_SYSTEM_UNITS[@]}"; do
    if unit_exists "$u" && [ "$(systemctl is-enabled "$u" 2>/dev/null)" != "masked" ]; then
        systemctl disable --now "$u" >/dev/null 2>&1 || true
        systemctl mask "$u" >/dev/null 2>&1 && log "masked $u"
    fi
done
# Per-user desktop session services (audio server, keyring, portals) that start for every login (also ssh).
DESKTOP_USER_UNITS=(pipewire.service pipewire.socket pipewire-pulse.service pipewire-pulse.socket wireplumber.service
    filter-chain.service mpris-proxy.service gnome-keyring-daemon.service gnome-keyring-daemon.socket
    gcr-ssh-agent.service gcr-ssh-agent.socket xdg-desktop-portal-rewrite-launchers.service fbd-alert-slider.service)
for u in "${DESKTOP_USER_UNITS[@]}"; do
    if [ -e "/usr/lib/systemd/user/$u" ] && [ "$(systemctl --global is-enabled "$u" 2>/dev/null)" != "masked" ]; then
        systemctl --global mask "$u" >/dev/null 2>&1 && log "masked user unit $u"
    fi
done

# ------------------------------------------------------------------------------------------
step "Packages (apt)"
if [ "${FNAV_SKIP_APT:-0}" = "1" ]; then
    log "FNAV_SKIP_APT=1 - skipping"
else
    export DEBIAN_FRONTEND=noninteractive
    APT=(apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)
    "${APT[@]}" update || die "apt-get update failed - is the device online?"
    REQUIRED=(
        python3-venv python3-pip python3-numpy python3-picamera2 python3-serial
        python3-lgpio python3-gpiozero python3-gps
        gpsd gpsd-clients chrony ffmpeg i2c-tools
        cage seatd fonts-dejavu-core avahi-daemon rsync curl ca-certificates sudo network-manager
    )
    "${APT[@]}" install "${REQUIRED[@]}"
    # Packages whose name differs between releases/repos: first one that installs wins.
    install_any() { local p; for p in "$@"; do if "${APT[@]}" install "$p"; then return 0; fi; done; return 1; }
    command -v chromium >/dev/null || command -v chromium-browser >/dev/null \
        || install_any chromium chromium-browser || die "cannot install Chromium (tried: chromium, chromium-browser)"
    command -v rpicam-hello >/dev/null || install_any rpicam-apps-lite rpicam-apps libcamera-apps-lite \
        || warn "rpicam-apps not installed (only needed for diagnostics: rpicam-hello --list-cameras)"
    dpkg -s python3-av >/dev/null 2>&1 || install_any python3-av || warn "python3-av not installed - RTSP relay will use ffmpeg only"
    dpkg -s python3-simplejpeg >/dev/null 2>&1 || install_any python3-simplejpeg         || warn "python3-simplejpeg not installed - MJPEG encoding will fall back to Pillow"
    dpkg -s polkitd >/dev/null 2>&1 || install_any polkitd policykit-1 || warn "polkit not installed - nmcli from fnav may be denied"
fi
# On trixie the package is "chromium"; make sure the unit's "chromium" resolves either way.
if ! command -v chromium >/dev/null && command -v chromium-browser >/dev/null; then
    ln -sf "$(command -v chromium-browser)" /usr/local/bin/chromium
    log "symlink /usr/local/bin/chromium -> chromium-browser"
fi
command -v cage >/dev/null || die "cage is not installed"
command -v chromium >/dev/null || die "chromium is not installed"

# ------------------------------------------------------------------------------------------
step "User, groups, directories"
if ! id fnav >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/fnav --no-create-home --shell /usr/sbin/nologin \
        --comment "fallout.nav services" fnav
    log "user fnav created"
fi
passwd -l fnav >/dev/null 2>&1 || true   # no password login
for g in video render i2c gpio dialout systemd-journal netdev input tty; do
    if ! getent group "$g" >/dev/null; then
        case "$g" in i2c|gpio|netdev) groupadd --system "$g"; log "group $g created" ;; *) continue ;; esac
    fi
    usermod -aG "$g" fnav
done
log "fnav groups: $(id -nG fnav)"

install -d -m 0755 -o root -g root /opt/fnav "$APP"
install -d -m 0755 -o fnav -g fnav /var/lib/fnav /var/cache/fnav /var/cache/fnav/pycache
install -d -m 0755 -o fnav -g fnav /var/lib/fnav/recordings /var/lib/fnav/tiles
install -d -m 0750 -o fnav -g fnav /etc/fnav

# ------------------------------------------------------------------------------------------
step "Application code -> $APP"
if [ "$REPO" = "$APP" ]; then
    log "running from $APP - nothing to copy"
else
    RS=(-rlt --delete --chown=root:root --chmod=D755,F644
        --exclude=/.git/ --exclude=/.devdata/ --exclude=/tests/ --exclude=__pycache__/ --exclude='*.pyc'
        --exclude=/.pytest_cache/ --exclude=/.ruff_cache/ --exclude=/.venv/ --exclude=/venv/
        --exclude=/node_modules/ --exclude=/.claude/ --exclude=/secrets.env)
    rsync "${RS[@]}" "$REPO"/ "$APP"/
fi
chmod 0755 "$APP"/pi/install.sh "$APP"/pi/deploy.sh "$APP"/pi/bin/* 2>/dev/null || true
log "code in place: $(find "$APP" -type f | wc -l) files"

# ------------------------------------------------------------------------------------------
step "Python virtualenv -> $VENV"
if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv --system-site-packages "$VENV"
    log "venv created"
fi
# Must see apt's picamera2/libcamera/lgpio/gps/numpy
sed -i 's/^include-system-site-packages *=.*/include-system-site-packages = true/' "$VENV/pyvenv.cfg"
bash "$APP/pi/bin/fnav-pip-sync"
"$VENV/bin/python" -I -c 'import fastapi, uvicorn, pydantic, httpx, numpy, websockets; print("python deps OK")'
"$VENV/bin/python" -I -c 'import picamera2' 2>/dev/null && log "picamera2: importable" \
    || warn "picamera2 is not importable from the venv (check python3-picamera2 and include-system-site-packages)"

# ------------------------------------------------------------------------------------------
step "Web UI secrets -> $SECRETS"
if [ -n "$PASSWORD" ]; then
    (
        umask 077
        cd "$APP"
        # The password travels through stdin only (never argv/environment of the child, never the log).
        printf '%s' "$PASSWORD" | runuser -u fnav -- env \
            PYTHONPATH="$APP" PYTHONPYCACHEPREFIX=/var/cache/fnav/pycache \
            "$VENV/bin/python" -m fnav.core.passwd --secrets "$SECRETS"
    ) || die "fnav.core.passwd failed (is the backend present in the repo?)"
    unset PASSWORD
fi
[ -s "$SECRETS" ] || die "$SECRETS was not created"
grep -q '^FNAV_PASSWORD_HASH=' "$SECRETS" && grep -q '^FNAV_SESSION_SECRET=' "$SECRETS" \
    || die "$SECRETS lacks FNAV_PASSWORD_HASH / FNAV_SESSION_SECRET"
chown fnav:fnav "$SECRETS"; chmod 0600 "$SECRETS"
log "secrets file OK (0600, owner fnav)"

# ------------------------------------------------------------------------------------------
step "MediaMTX v$MEDIAMTX_VERSION"
install_mediamtx() {
    local ver="$MEDIAMTX_VERSION" cur=""
    if [ -x /usr/local/bin/mediamtx ]; then cur="$(/usr/local/bin/mediamtx --version 2>/dev/null | head -n1 || true)"; fi
    if [ "$cur" = "v$ver" ] || [ "$cur" = "$ver" ]; then log "already installed: $cur"; return 0; fi
    local tmp base name hash asset=""
    tmp="$(mktemp -d)"
    base="https://github.com/bluenviron/mediamtx/releases/download/v${ver}"
    curl -fL --retry 3 --connect-timeout 15 -o "$tmp/checksums.sha256" "$base/checksums.sha256" \
        || { rm -rf "$tmp"; die "cannot download $base/checksums.sha256 (wrong MEDIAMTX_VERSION or no internet?)"; }
    # The arm64 asset is called linux_arm64 or linux_arm64v8 depending on the release.
    for suffix in linux_arm64 linux_arm64v8; do
        name="mediamtx_v${ver}_${suffix}.tar.gz"
        hash="$(awk -v n="$name" '{f=$2; sub(/^\*/,"",f); if (f==n) print $1}' "$tmp/checksums.sha256")"
        if [ -n "$hash" ]; then asset="$name"; break; fi
    done
    [ -n "$asset" ] || { rm -rf "$tmp"; die "no linux arm64 asset in checksums.sha256 of MediaMTX v$ver"; }
    curl -fL --retry 3 --connect-timeout 15 -o "$tmp/$asset" "$base/$asset" || { rm -rf "$tmp"; die "download of $asset failed"; }
    ( cd "$tmp" && echo "$hash  $asset" | sha256sum -c - ) || { rm -rf "$tmp"; die "sha256 mismatch for $asset"; }
    tar -xzf "$tmp/$asset" -C "$tmp" mediamtx
    install -m 0755 -o root -g root "$tmp/mediamtx" /usr/local/bin/mediamtx.new
    mv -f /usr/local/bin/mediamtx.new /usr/local/bin/mediamtx
    rm -rf "$tmp"
    log "installed $(/usr/local/bin/mediamtx --version 2>/dev/null | head -n1 || echo "v$ver")"
}
install_mediamtx

# ------------------------------------------------------------------------------------------
step "systemd units, sysctl, sudoers, polkit, udev"
bash "$APP/pi/bin/fnav-sync-system" --from "$APP"

# ------------------------------------------------------------------------------------------
step "gpsd and chrony"
backup_once /etc/default/gpsd
if ! cmp -s "$APP/pi/gpsd/gpsd.default" /etc/default/gpsd; then
    install -m 0644 -o root -g root "$APP/pi/gpsd/gpsd.default" /etc/default/gpsd
    GPSD_CHANGED=1
else
    GPSD_CHANGED=0
fi
install -d -m 0755 /etc/chrony/conf.d
CHRONY_CHANGED=0
if ! cmp -s "$APP/pi/chrony/fnav.conf" /etc/chrony/conf.d/fnav.conf 2>/dev/null; then
    install -m 0644 -o root -g root "$APP/pi/chrony/fnav.conf" /etc/chrony/conf.d/fnav.conf
    CHRONY_CHANGED=1
fi
if [ -f /etc/chrony/chrony.conf ]; then
    backup_once /etc/chrony/chrony.conf
    grep -Eq '^[[:space:]]*confdir[[:space:]]+/etc/chrony/conf\.d' /etc/chrony/chrony.conf \
        || { echo 'confdir /etc/chrony/conf.d' >> /etc/chrony/chrony.conf; CHRONY_CHANGED=1; }
    # a later "makestep 1 3" in the main file would override our "makestep 1 -1"
    if grep -Eq '^[[:space:]]*makestep[[:space:]]' /etc/chrony/chrony.conf; then
        sed -i -E 's|^[[:space:]]*(makestep[[:space:]].*)$|# \1   # disabled by fnav: see conf.d/fnav.conf|' /etc/chrony/chrony.conf
        CHRONY_CHANGED=1
    fi
fi
# Recording file names depend on the clock, so let timedatectl rely on chrony, not on a leftover timesyncd.
systemctl disable --now systemd-timesyncd.service 2>/dev/null || true
log "gpsd config: $([ "$GPSD_CHANGED" = 1 ] && echo updated || echo unchanged); chrony config: $([ "$CHRONY_CHANGED" = 1 ] && echo updated || echo unchanged)"

# ------------------------------------------------------------------------------------------
step "Boot configuration ($BOOT_DIR)"
CFG="$BOOT_DIR/config.txt"; CMD="$BOOT_DIR/cmdline.txt"
[ -f "$CFG" ] && [ -f "$CMD" ] || die "$CFG or $CMD not found"

# 1) EDID for the fixed 720x576@50 output (works without a connected monitor / without converter EDID)
install -d -m 0755 /lib/firmware/edid
EDID_TMP="$(mktemp)"
python3 -I "$APP/pi/tools/gen-edid.py" "$EDID_TMP" || die "EDID generation failed"
python3 -I "$APP/pi/tools/gen-edid.py" --check "$EDID_TMP" >/dev/null || die "generated EDID is invalid"
EDID_CHANGED=0
if ! cmp -s "$EDID_TMP" "$EDID" 2>/dev/null; then
    install -m 0644 -o root -g root "$EDID_TMP" "$EDID"; EDID_CHANGED=1; NEED_REBOOT=1
    log "EDID written: $EDID"
fi
rm -f "$EDID_TMP"
if dpkg -s initramfs-tools >/dev/null 2>&1; then
    # vc4 may load from the initramfs, where /lib/firmware of the rootfs is not visible yet
    install -d -m 0755 /etc/initramfs-tools/hooks
    if ! cmp -s "$APP/pi/initramfs/fnav-edid" /etc/initramfs-tools/hooks/fnav-edid 2>/dev/null || [ "$EDID_CHANGED" = 1 ]; then
        install -m 0755 -o root -g root "$APP/pi/initramfs/fnav-edid" /etc/initramfs-tools/hooks/fnav-edid
        update-initramfs -u -k all || warn "update-initramfs failed"
    fi
fi

# 2) config.txt: replace the block between the markers, comment out conflicting lines elsewhere
backup_once "$CFG"
TMP="$(mktemp)"
sed -E '/^# >>> fnav[[:space:]]*$/,/^# <<< fnav[[:space:]]*$/d' "$CFG" > "$TMP"
KEYS='enable_uart|camera_auto_detect|display_auto_detect|disable_fw_kms_setup|gpu_mem|hdmi_force_hotplug|hdmi_group|hdmi_mode|enable_tvout'
sed -E -i \
    -e "s/^[[:space:]]*((${KEYS})(:[0-9])?[[:space:]]*=.*)$/# fnav-disabled: \1/" \
    -e "s/^[[:space:]]*(dtparam=(i2c_arm|audio)([=,[:space:]].*)?)$/# fnav-disabled: \1/" \
    -e "s/^[[:space:]]*(dtoverlay=(vc4-f?kms-v3d|ov5647|disable-bt)([,[:space:]].*)?)$/# fnav-disabled: \1/" \
    "$TMP"
[ -z "$(tail -c1 "$TMP")" ] || echo >> "$TMP"
cat "$APP/pi/boot/config.txt.fnav" >> "$TMP"
if ! cmp -s "$TMP" "$CFG"; then cat "$TMP" > "$CFG"; NEED_REBOOT=1; log "config.txt updated"; else log "config.txt unchanged"; fi
rm -f "$TMP"

# 3) cmdline.txt: no serial console, our video= / drm.edid_firmware= / consoleblank=, no plymouth splash
backup_once "$CMD"
mapfile -t ADD < <(sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' "$APP/pi/boot/cmdline.txt.fnav" | grep -Ev '^(#|$)')
read -r -a TOKENS <<< "$(tr '\n' ' ' < "$CMD")"
OUT=()
for t in "${TOKENS[@]}"; do
    skip=0
    case "$t" in
        console=serial0,*|console=ttyAMA0,*|console=ttyS0,*|splash) skip=1 ;;
        # video output is fully ours (cmdline.txt.fnav): drop stale tokens of the other output type
        video=*|drm.edid_firmware=*|vc4.tv_norm=*) skip=1 ;;
    esac
    for a in "${ADD[@]}"; do
        case "$a" in video=*) key="${a%%:*}:" ;; *) key="${a%%=*}=" ;; esac
        case "$t" in "$key"*) skip=1 ;; esac
    done
    [ "$skip" -eq 1 ] || OUT+=("$t")
done
OUT+=("${ADD[@]}")
NEW_CMD="${OUT[*]}"
if [ "$NEW_CMD" != "$(tr '\n' ' ' < "$CMD" | sed 's/[[:space:]]*$//')" ]; then
    printf '%s\n' "$NEW_CMD" > "$CMD"; NEED_REBOOT=1; log "cmdline.txt updated"
else
    log "cmdline.txt unchanged"
fi
log "cmdline: $(cat "$CMD")"

# ------------------------------------------------------------------------------------------
step "Serial console, Bluetooth, other services"
for u in serial-getty@ttyAMA0.service serial-getty@serial0.service; do
    systemctl disable --now "$u" 2>/dev/null || true
    systemctl mask "$u" 2>/dev/null || true
done
systemctl disable --now serial-getty@ttyS0.service 2>/dev/null || true
for u in hciuart.service bluetooth.service ModemManager.service triggerhappy.service; do
    if unit_exists "$u"; then systemctl disable --now "$u" 2>/dev/null || true; log "disabled $u"; fi
done

if [ -n "${FNAV_WIFI_SSID:-}" ] && [ -n "${FNAV_WIFI_PSK:-}" ]; then
    if [ -n "$(nmcli -t -f NAME connection show | grep -xF "$FNAV_WIFI_SSID" || true)" ]; then
        log "Wi-Fi profile '$FNAV_WIFI_SSID' already exists"
    else
        nmcli connection add type wifi con-name "$FNAV_WIFI_SSID" ssid "$FNAV_WIFI_SSID" \
            wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$FNAV_WIFI_PSK" \
            connection.autoconnect yes connection.autoconnect-priority 10 >/dev/null
        log "Wi-Fi profile '$FNAV_WIFI_SSID' created"
    fi
fi
unset FNAV_WIFI_PSK

# ------------------------------------------------------------------------------------------
step "Enabling and starting services"
systemctl daemon-reload
systemctl enable avahi-daemon.service chrony.service seatd.service 2>/dev/null || warn "could not enable avahi/chrony/seatd"
systemctl enable gpsd.socket gpsd.service 2>/dev/null || warn "could not enable gpsd"
systemctl enable mediamtx.service fnav-core.service fnav-camera.service fnav-display.service
[ "$CHRONY_CHANGED" = 1 ] && systemctl restart chrony.service 2>/dev/null || true
[ "$GPSD_CHANGED" = 1 ] && { systemctl restart gpsd.socket gpsd.service 2>/dev/null || true; }
# Safe to start right away; camera and display need the boot configuration (reboot) first.
systemctl restart mediamtx.service fnav-core.service || warn "mediamtx/fnav-core failed to start - see: journalctl -u fnav-core -u mediamtx"
sleep 2

# ------------------------------------------------------------------------------------------
step "Summary"
printf '  %-16s %s\n' "fnav-core" "$(systemctl is-active fnav-core 2>/dev/null || true)"
printf '  %-16s %s\n' "mediamtx" "$(systemctl is-active mediamtx 2>/dev/null || true)"
printf '  %-16s %s\n' "gpsd" "$(systemctl is-active gpsd 2>/dev/null || true)"
printf '  %-16s %s\n' "chrony" "$(systemctl is-active chrony 2>/dev/null || true)"
printf '  %-16s %s\n' "default target" "$(systemctl get-default)"
printf '  %-16s %s\n' "mediamtx version" "$(/usr/local/bin/mediamtx --version 2>/dev/null | head -n1 || echo '?')"
printf '  %-16s %s\n' "chromium" "$(command -v chromium)"
printf '  %-16s %s\n' "log" "$LOG"
if [ "${#WARNINGS[@]}" -gt 0 ]; then
    echo; echo "Warnings:"; printf '  - %s\n' "${WARNINGS[@]}"
fi
echo
if [ "$NEED_REBOOT" -eq 1 ]; then
    echo "REBOOT REQUIRED: boot configuration (UART, I2C, camera, HDMI 720x576) changed."
    echo "  sudo reboot"
else
    echo "No boot configuration changes - a reboot is not required (fnav-camera/fnav-display: sudo systemctl restart ...)."
fi
echo "After the reboot: http://$(hostname).local/  (or the device IP), see pi/README.md for checks."
