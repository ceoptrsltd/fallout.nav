#!/usr/bin/env bash
# Deploy the repository from the PC (Git Bash on Windows, or any Linux/macOS shell) to the fnav device.
# No rsync needed: the repo is packed with tar and streamed through ssh.
#
# Usage:
#   pi/deploy.sh                 sync code to /opt/fnav/app, update system files and Python deps if
#                                changed, restart fnav-core fnav-camera fnav-display
#   pi/deploy.sh --no-restart    same, but restart nothing
#   pi/deploy.sh --only "fnav-core fnav-display"   restart only these services
#   pi/deploy.sh --stage         first install, step 1: upload the repo to ~/fallout.nav on the device
#   pi/deploy.sh --install       stage + run "sudo ./pi/install.sh" there (interactive: asks for the web password)
#   pi/deploy.sh --dry-run       build the archive, print what would be sent, send nothing
#
# Environment:
#   FNAV_HOST       device address (default 172.23.204.16; an ssh alias such as "fnav" works too)
#   FNAV_USER       ssh user on the device (default falloutnav); needs passwordless sudo
#   FNAV_SSH_KEY    path to the private key (optional; ssh-agent / ~/.ssh/config work as usual)
#   FNAV_SSH_OPTS   extra ssh options (default: -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15)
#   FNAV_HOSTNAME, FNAV_TIMEZONE, MEDIAMTX_VERSION   passed on to install.sh with --install
#
# Key-based ssh authentication is assumed. For --install use a real terminal (Windows Terminal, or
# "winpty" in mintty) so that ssh can allocate a TTY for the password prompt.
set -euo pipefail

HOST="${FNAV_HOST:-172.23.204.16}"
DEV_USER="${FNAV_USER:-falloutnav}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

MODE=deploy
RESTART="fnav-core fnav-camera fnav-display"
while [ $# -gt 0 ]; do
    case "$1" in
        --no-restart) RESTART="" ;;
        --only) RESTART="${2:?--only needs a quoted list of services}"; shift ;;
        --stage) MODE=stage ;;
        --install) MODE=install ;;
        --dry-run) MODE=dry ;;
        -h|--help) sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown argument: $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done

SSH_ARGS=()
if [ -n "${FNAV_SSH_OPTS+x}" ]; then
    read -r -a SSH_ARGS <<< "$FNAV_SSH_OPTS"
else
    SSH_ARGS=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15)
fi
[ -n "${FNAV_SSH_KEY:-}" ] && SSH_ARGS+=(-i "$FNAV_SSH_KEY")
TARGET="${DEV_USER}@${HOST}"

# Pack the working tree (uncommitted changes included). Order matters: --anchored/--no-anchored apply to
# the --exclude options that follow them.
pack() {
    tar -C "$ROOT" -czf - --owner=0 --group=0 --numeric-owner \
        --no-anchored --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' --exclude='.ruff_cache' \
        --anchored --exclude='./.git' --exclude='./.devdata' --exclude='./tests' --exclude='./.venv' \
        --exclude='./venv' --exclude='./node_modules' --exclude='./.claude' --exclude='./secrets.env' \
        .
}

[ -f "$ROOT/pyproject.toml" ] && [ -d "$ROOT/pi" ] || { echo "Cannot find the repository root (looked in $ROOT)" >&2; exit 1; }

if [ "$MODE" = dry ]; then
    tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
    pack > "$tmp"
    echo "Archive: $(wc -c < "$tmp") bytes, $(tar -tzf "$tmp" | grep -vc '/$') files"
    echo "Top level:"; tar -tzf "$tmp" | awk -F/ 'NF>=2 && $2!="" {print "  " $2}' | sort -u
    echo "Target: $TARGET (mode: $MODE, restart: ${RESTART:-nothing})"
    exit 0
fi

echo "Deploying $ROOT -> $TARGET (mode: $MODE)"

if [ "$MODE" = stage ] || [ "$MODE" = install ]; then
    STAGE_REMOTE='set -e; rm -rf ~/fallout.nav.new; mkdir ~/fallout.nav.new; tar -xzf - -C ~/fallout.nav.new; rm -rf ~/fallout.nav; mv ~/fallout.nav.new ~/fallout.nav; chmod +x ~/fallout.nav/pi/install.sh ~/fallout.nav/pi/deploy.sh ~/fallout.nav/pi/bin/*; echo "Uploaded to ~/fallout.nav"'
    pack | ssh "${SSH_ARGS[@]}" "$TARGET" "bash -c $(printf '%q' "$STAGE_REMOTE")"
    if [ "$MODE" = stage ]; then
        echo "Next: ssh -t $TARGET 'cd ~/fallout.nav && sudo ./pi/install.sh'   (or rerun with --install)"
        exit 0
    fi
    envs=()
    for v in FNAV_HOSTNAME FNAV_TIMEZONE MEDIAMTX_VERSION; do
        [ -n "${!v:-}" ] && envs+=("$v=$(printf '%q' "${!v}")")
    done
    # -t: install.sh asks for the web password on the device; it is never put on a command line.
    ssh -t "${SSH_ARGS[@]}" "$TARGET" "cd ~/fallout.nav && sudo env ${envs[*]:-} ./pi/install.sh"
    exit 0
fi

# Regular deploy: upload + unpack to a temp dir, then fnav-deploy-remote (as root) does the rest.
REMOTE='set -euo pipefail; tmp=$(mktemp -d /tmp/fnav-deploy.XXXXXX); trap '"'"'rm -rf "$tmp"'"'"' EXIT; tar -xzf - -C "$tmp"; sudo -n env FNAV_RESTART="$FNAV_RESTART_ARG" bash "$tmp/pi/bin/fnav-deploy-remote" "$tmp"'
REMOTE="FNAV_RESTART_ARG=$(printf '%q' "$RESTART"); $REMOTE"

pack | ssh "${SSH_ARGS[@]}" "$TARGET" "bash -c $(printf '%q' "$REMOTE")"
echo "Deploy finished."
