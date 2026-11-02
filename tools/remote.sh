#!/usr/bin/env bash
# Remote admin (https://<site>/fnadmin/) and the position log server: setup and updates from the PC
# (Git Bash on Windows or any Linux/macOS shell). See docs/contracts/remote.md.
#
#   tools/remote.sh setup    everything, idempotent: fnav-relay on the server, nginx, the tunnel user and key,
#                            the device side (/etc/fnav/remote.env, tunnel key, known_hosts), web copy, start.
#                            The device must already have the current code (pi/deploy.sh).
#   tools/remote.sh web      copy web/admin and web/display to the server (after UI changes; pi/deploy.sh does it
#                            itself when this file's settings exist), with a fresh web/admin/version.json
#   tools/remote.sh relay    reinstall the fnav-relay code on the server
#   tools/remote.sh nginx    reinstall the nginx snippet
#   tools/remote.sh status   tunnel and relay state
#   tools/remote.sh ssh [cmd]      ssh to the device through the server (its tunnel forwards the device's sshd
#                                  to 127.0.0.1:18022 on the server): works wherever the van is online
#   tools/remote.sh deploy [args]  pi/deploy.sh through the same way (args as for pi/deploy.sh), then "web"
#   tools/remote.sh ssh-server     allow that forward for the tunnel user on the server (setup does it too)
#
# Server and device details live in tools/.remote.env (not in git), see tools/remote.env.example.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/tools/.remote.env"
[ -f "$ENV_FILE" ] || { echo "Missing $ENV_FILE (copy tools/remote.env.example)" >&2; exit 2; }
# shellcheck disable=SC1090
. "$ENV_FILE"
: "${REMOTE_SSH:?REMOTE_SSH (user@server) is not set}"
: "${REMOTE_SITE:?REMOTE_SITE (public host name) is not set}"
: "${REMOTE_SITE_FILE:?REMOTE_SITE_FILE (nginx site file on the server) is not set}"
FNAV_HOST="${FNAV_HOST:-fnav}"
TUN_USER="${REMOTE_TUNNEL_USER:-fnavtun}"
TUN_PORT="${REMOTE_TUNNEL_PORT:-22}"
DEV_SSH_PORT=18022  # the device's sshd on the server's loopback (fnav-tunnel.service -R)

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15)
srv() { ssh "${SSH_OPTS[@]}" -o IdentitiesOnly=yes ${REMOTE_SSH_KEY:+-i "$REMOTE_SSH_KEY"} "$REMOTE_SSH" "$@"; }
dev() { ssh "${SSH_OPTS[@]}" "$FNAV_HOST" "$@"; }
say() { printf '\n== %s\n' "$*"; }
# remote command: stdin -> FILE on the device (root:fnav 0640, replaced atomically)
put_cmd() { echo "sudo sh -c 'umask 077; cat > $1.tmp && chown root:fnav $1.tmp && chmod 0640 $1.tmp && mv $1.tmp $1'"; }

do_relay() {
    say "fnav-relay -> server"
    tar -C "$ROOT" -czf - --exclude='__pycache__' --exclude='*.pyc' server \
        | srv 'set -e; d=$(mktemp -d); tar -C "$d" -xzf - --no-same-owner; bash "$d/server/install.sh"; rm -rf "$d"'
}

do_web() {
    say "web UI copy -> server:/var/www/fnadmin"
    # the admin's version marker: an admin open through the server sees the change and offers a reload
    bash "$ROOT/tools/web-version.sh" | sed 's/^/  /' || return
    tar -C "$ROOT/web" -czf - --exclude='dev' --exclude='__pycache__' admin display \
        | srv 'set -e; t=/var/www/fnadmin; rm -rf "$t.new" "$t.old"; install -d -m 0755 "$t.new"
               tar -C "$t.new" -xzf - --no-same-owner; chmod -R u=rwX,go=rX "$t.new"
               [ -d "$t" ] && mv "$t" "$t.old"; mv "$t.new" "$t"; rm -rf "$t.old"; echo "  $(find "$t" -type f | wc -l) files"'
}

do_nginx() {
    say "nginx snippet -> server ($REMOTE_SITE_FILE)"
    local d
    d="$(srv 'mktemp -d')"
    tar -C "$ROOT/server/nginx" -czf - fnadmin.conf fnadmin-zones.conf | srv "tar -C '$d' -xzf - --no-same-owner"
    srv "SITE='$REMOTE_SITE_FILE' d='$d' bash -s" <<'EOF'
set -e
Z=/etc/nginx/conf.d/fnadmin-zones.conf; N=/etc/nginx/snippets/fnadmin.conf
for f in "$Z" "$N"; do [ -f "$f" ] && cp -p "$f" "$f.prev"; done
install -m 0644 "$d/fnadmin-zones.conf" "$Z"
install -m 0644 "$d/fnadmin.conf" "$N"
rm -rf "$d"
backup=""
if ! grep -q 'snippets/fnadmin.conf' "$SITE"; then
    backup="$SITE.bak-$(date +%F-%H%M%S)"
    cp -p "$SITE" "$backup"
    # into the HTTPS server block: right before its first access_log line
    python3 - "$SITE" <<'PY'
import re, sys
p = sys.argv[1]
lines = open(p, encoding="utf-8").read().split(chr(10))
for i, ln in enumerate(lines):
    if re.match(r"\s*access_log\s", ln):
        lines[i:i] = ["    # fallout.nav remote admin (/fnadmin), see /etc/nginx/snippets/fnadmin.conf",
                      "    include snippets/fnadmin.conf;", ""]
        break
else:
    sys.exit("no access_log line to anchor the include")
open(p, "w", encoding="utf-8").write(chr(10).join(lines))
PY
    echo "  include added (backup: $backup)"
fi
if out="$(nginx -t 2>&1)"; then
    systemctl reload nginx && echo "  nginx -t ok, reloaded"
    rm -f "$Z.prev" "$N.prev"
else
    echo "$out" >&2
    # back to the previous working state: old snippet files, or none plus the original site file
    for f in "$Z" "$N"; do if [ -f "$f.prev" ]; then mv -f "$f.prev" "$f"; else rm -f "$f"; fi; done
    [ -n "$backup" ] && cp -p "$backup" "$SITE"
    echo "nginx -t failed: previous fnadmin config restored, nginx not reloaded" >&2; exit 1
fi
EOF
}

do_tunnel() {
    say "tunnel: device key, server user, device config"
    dev 'systemctl cat fnav-tunnel.service >/dev/null 2>&1' \
        || { echo "fnav-tunnel.service is not on the device yet: run pi/deploy.sh first" >&2; exit 1; }
    dev 'sudo test -f /etc/fnav/tunnel_key || sudo -u fnav ssh-keygen -q -t ed25519 -N "" -C "fnav-tunnel@$(hostname)" -f /etc/fnav/tunnel_key'
    local pub token hostkey
    pub="$(dev 'sudo cat /etc/fnav/tunnel_key.pub')"
    srv "TUN_USER='$TUN_USER' PUB='$pub' bash -s" <<'EOF'
set -e
if ! id "$TUN_USER" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "/var/lib/$TUN_USER" --shell /usr/sbin/nologin "$TUN_USER"
    usermod -p '*' "$TUN_USER"   # no password, but not "locked" (sshd refuses keys of locked accounts)
fi
h="$(getent passwd "$TUN_USER" | cut -d: -f6)"
install -d -m 0700 -o "$TUN_USER" -g "$TUN_USER" "$h/.ssh"
# only the forwards of fnav-tunnel.service: no shell, no pty, no other ports
printf 'restrict,port-forwarding,permitlisten="127.0.0.1:18080",permitlisten="127.0.0.1:18022",permitopen="127.0.0.1:8095" %s\n' \
    "$PUB" > "$h/.ssh/authorized_keys"
chown "$TUN_USER:$TUN_USER" "$h/.ssh/authorized_keys"; chmod 0600 "$h/.ssh/authorized_keys"
echo "  $TUN_USER: key installed"
EOF
    token="$(srv 'sed -n "s/^FNAV_RELAY_DEVICE_TOKEN=//p" /etc/fnav-relay/relay.env')"
    [ -n "$token" ] || { echo "no FNAV_RELAY_DEVICE_TOKEN on the server: run '$0 relay' first" >&2; exit 1; }
    hostkey="$(srv 'cut -d" " -f1,2 /etc/ssh/ssh_host_ed25519_key.pub')"
    [ "$TUN_PORT" = 22 ] && khost="$REMOTE_SITE" || khost="[$REMOTE_SITE]:$TUN_PORT"
    dev "$(put_cmd /etc/fnav/tunnel_known_hosts)" <<<"$khost $hostkey"
    dev "$(put_cmd /etc/fnav/remote.env)" <<EOF
# fallout.nav remote access (written by tools/remote.sh setup). Apply: sudo systemctl restart fnav-tunnel fnav-core
FNAV_TUNNEL_HOST=$REMOTE_SITE
FNAV_TUNNEL_USER=$TUN_USER
FNAV_TUNNEL_PORT=$TUN_PORT
FNAV_RELAY_URL=http://127.0.0.1:18095
FNAV_RELAY_DEVICE_TOKEN=$token
EOF
    dev 'sudo systemctl daemon-reload && sudo systemctl enable fnav-tunnel.service >/dev/null 2>&1
         sudo systemctl restart fnav-tunnel.service fnav-core.service && echo "  fnav-tunnel and fnav-core restarted"'
}

do_ssh_server() {
    say "server: the tunnel user may forward the device's sshd to 127.0.0.1:$DEV_SSH_PORT"
    srv "TUN_USER='$TUN_USER' bash -s" <<'EOF'
set -e
f="$(getent passwd "$TUN_USER" | cut -d: -f6)/.ssh/authorized_keys"
[ -f "$f" ] || { echo "no $f: run 'tools/remote.sh setup' first" >&2; exit 1; }
if grep -q 'permitlisten="127.0.0.1:18022"' "$f"; then echo "  already allowed"; exit 0; fi
sed -i 's/permitlisten="127.0.0.1:18080",/permitlisten="127.0.0.1:18080",permitlisten="127.0.0.1:18022",/' "$f"
grep -q 'permitlisten="127.0.0.1:18022"' "$f" && echo "  allowed" || { echo "could not update $f" >&2; exit 1; }
EOF
}

# ssh config for the device through the server: Host fnav-remote = 127.0.0.1:18022 on the server (ProxyJump).
# The device's host key is checked under the name ssh already knows it by (FNAV_HOST resolved by ssh -G).
SSH_CONF=""
ssh_conf() {
    [ -n "$SSH_CONF" ] && return
    SSH_CONF="$(mktemp)"
    trap 'rm -f "$SSH_CONF"' EXIT
    local alias g
    g="$(ssh -G "$FNAV_HOST" 2>/dev/null || true)"  # the device alias's own settings: its name and key
    alias="${FNAV_HOST_KEY_ALIAS:-$(awk '$1 == "hostname" {print $2; exit}' <<<"$g")}"
    {
        echo "Host fnav-jump"
        echo "    HostName ${REMOTE_SSH#*@}"
        [ "$REMOTE_SSH" != "${REMOTE_SSH#*@}" ] && echo "    User ${REMOTE_SSH%@*}"
        [ -n "${REMOTE_SSH_KEY:-}" ] && printf '    IdentityFile %s\n    IdentitiesOnly yes\n' "$REMOTE_SSH_KEY"
        echo "Host fnav-remote"
        echo "    HostName 127.0.0.1"
        echo "    Port $DEV_SSH_PORT"
        echo "    User ${FNAV_USER:-falloutnav}"
        echo "    HostKeyAlias ${alias:-$FNAV_HOST}"
        awk '$1 == "identityfile" {print "    IdentityFile " $2} $1 == "identitiesonly" {print "    IdentitiesOnly " $2}' <<<"$g"
        echo "    ProxyJump fnav-jump"
        echo "    ServerAliveInterval 15"
        echo "    ConnectTimeout 20"
    } > "$SSH_CONF"
}

do_ssh() {
    ssh_conf
    if [ $# -eq 0 ]; then ssh -t -F "$SSH_CONF" fnav-remote; else ssh -F "$SSH_CONF" -o BatchMode=yes fnav-remote "$@"; fi
}

do_deploy() {
    ssh_conf
    local web=1 a
    for a in "$@"; do case "$a" in --dry-run|--stage|--install|-h|--help) web=0 ;; esac; done
    FNAV_WEB_COPY=no FNAV_HOST=fnav-remote FNAV_SSH_OPTS="-F $SSH_CONF -o BatchMode=yes -o ServerAliveInterval=15" \
        bash "$ROOT/pi/deploy.sh" "$@"
    # the server's copy of web/ does not come from the device: without it the remote admin keeps the old UI
    if [ "$web" = 1 ]; then
        do_web || { echo "WARNING: the device is updated, the server's copy of the web UI is not: $0 web" >&2; exit 1; }
    fi
}

do_status() {
    say "device"
    dev 'printf "  fnav-tunnel: %s\n" "$(systemctl is-active fnav-tunnel)"; journalctl -u fnav-tunnel -n 3 --no-pager -o cat | sed "s/^/  /"
         curl -s -m 5 http://127.0.0.1/api/remote/status 2>/dev/null | head -c 300; echo' || true
    say "server"
    srv 'printf "  fnav-relay: %s\n" "$(systemctl is-active fnav-relay)"; ss -ltn | grep -E "127.0.0.1:(18080|18022|8095) " | sed "s/^/  /"' || true
    say "ssh through the server"
    ssh_conf
    if out="$(ssh -F "$SSH_CONF" -o BatchMode=yes fnav-remote 'echo "  $(hostname): ssh ok"' 2>&1)"; then echo "$out"
    elif ! srv "ss -ltn | grep -q '127.0.0.1:$DEV_SSH_PORT '" 2>/dev/null; then
        echo "  no 127.0.0.1:$DEV_SSH_PORT on the server: the device's tunnel has no ssh forward yet (deploy the"
        echo "  current code once from the local network) or the device is offline"
    else echo "  not reachable: ${out##*$'\n'}"; fi
    say "public"
    curl -s -m 10 -o /dev/null -w "  https://$REMOTE_SITE/fnadmin/admin/ -> %{http_code}\n" "https://$REMOTE_SITE/fnadmin/admin/" || true
    curl -s -m 10 "https://$REMOTE_SITE/fnadmin/api/session" | sed 's/^/  session: /'; echo
}

case "${1:-}" in
    setup) do_relay; do_nginx; do_web; do_tunnel; sleep 5; do_status ;;
    web) do_web ;;
    relay) do_relay ;;
    nginx) do_nginx ;;
    tunnel) do_tunnel ;;
    status) do_status ;;
    ssh) shift; do_ssh "$@" ;;
    deploy) shift; do_deploy "$@" ;;
    ssh-server) do_ssh_server ;;
    *) sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
