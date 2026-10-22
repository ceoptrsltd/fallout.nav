#!/usr/bin/env bash
# Install or update fnav-relay on the server (Ubuntu 24.04, Python 3.12, stdlib only). Idempotent.
#
# Usage (as root, from a copy of the repository's server/ directory):
#   sudo ./install.sh
#
# What it does:
#   - system user fnav-relay;
#   - code: server/fnav_relay -> /opt/fnav-relay/fnav_relay (owned by root, read-only for the service);
#   - /etc/fnav-relay/relay.env (0640 root:fnav-relay) with a freshly generated FNAV_RELAY_DEVICE_TOKEN
#     if the file or the token is missing; existing values are kept;
#   - systemd unit fnav-relay.service: install, enable, restart, health check.
# It does not create the ssh tunnel user and does not touch nginx.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR=/opt/fnav-relay
ENV_DIR=/etc/fnav-relay
ENV_FILE="$ENV_DIR/relay.env"
UNIT=fnav-relay.service

[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo $0)" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
[ -f "$HERE/fnav_relay/__main__.py" ] || { echo "fnav_relay/ not found next to $0" >&2; exit 1; }

# ── user ──
if ! id -u fnav-relay >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/fnav-relay --no-create-home --shell /usr/sbin/nologin fnav-relay
    echo "created user fnav-relay"
fi

# ── code (atomic swap) ──
install -d -m 0755 -o root -g root "$APP_DIR"
rm -rf "$APP_DIR/fnav_relay.new"
cp -r "$HERE/fnav_relay" "$APP_DIR/fnav_relay.new"
find "$APP_DIR/fnav_relay.new" -name '__pycache__' -prune -exec rm -rf {} +
chown -R root:root "$APP_DIR/fnav_relay.new"
find "$APP_DIR/fnav_relay.new" -type d -exec chmod 0755 {} +
find "$APP_DIR/fnav_relay.new" -type f -exec chmod 0644 {} +
rm -rf "$APP_DIR/fnav_relay.old"
[ -d "$APP_DIR/fnav_relay" ] && mv "$APP_DIR/fnav_relay" "$APP_DIR/fnav_relay.old"
mv "$APP_DIR/fnav_relay.new" "$APP_DIR/fnav_relay"
rm -rf "$APP_DIR/fnav_relay.old"
python3 -m compileall -q "$APP_DIR/fnav_relay"  # сервис сам байткод не пишет (ProtectSystem=strict)

# ── settings ──
install -d -m 0750 -o root -g fnav-relay "$ENV_DIR"
if [ ! -f "$ENV_FILE" ]; then
    (umask 027; cat > "$ENV_FILE" <<'EOF'
# fnav-relay settings (docs/contracts/remote.md, section 2)
FNAV_RELAY_LISTEN=127.0.0.1:8095
FNAV_RELAY_DB=/var/lib/fnav-relay/track.db
FNAV_RELAY_RADIUS_M=50
FNAV_RELAY_STOP_MIN_S=300
FNAV_RELAY_COOKIE_PATH=/fnadmin/
EOF
    )
    echo "created $ENV_FILE"
fi
if ! grep -qE '^FNAV_RELAY_DEVICE_TOKEN=.+' "$ENV_FILE"; then
    sed -i '/^FNAV_RELAY_DEVICE_TOKEN=/d' "$ENV_FILE"
    echo "FNAV_RELAY_DEVICE_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" >> "$ENV_FILE"
    echo "generated a new device token"
fi
chown root:fnav-relay "$ENV_FILE"
chmod 0640 "$ENV_FILE"

# ── service ──
install -m 0644 -o root -g root "$HERE/systemd/$UNIT" "/etc/systemd/system/$UNIT"
systemctl daemon-reload
systemctl enable "$UNIT" >/dev/null
systemctl restart "$UNIT"

LISTEN="$(sed -n 's/^FNAV_RELAY_LISTEN=//p' "$ENV_FILE" | tail -1)"
LISTEN="${LISTEN:-127.0.0.1:8095}"
ok=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if python3 - "$LISTEN" <<'EOF' 2>/dev/null
import sys, urllib.request
host, _, port = sys.argv[1].rpartition(":")
host = host.strip("[]") or "127.0.0.1"
if ":" in host:
    host = f"[{host}]"
with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=2) as r:
    sys.exit(0 if r.status == 200 else 1)
EOF
    then ok=1; break; fi
    sleep 0.5
done
if [ -z "$ok" ]; then
    echo "fnav-relay did not answer on $LISTEN/healthz" >&2
    journalctl -u "$UNIT" -n 30 --no-pager >&2 || true
    exit 1
fi

echo "fnav-relay is running on $LISTEN"
echo "Device token: FNAV_RELAY_DEVICE_TOKEN in $ENV_FILE (copy it to the device's /etc/fnav/remote.env)"
