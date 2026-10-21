#!/usr/bin/env bash
# Publish the project page (site/) to https://ptrs.ltd/apps/fallout.nav/
#   tools/publish_site.sh [--dry-run]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Server details live in tools/.publish.env (not in git): PUBLISH_HOST, PUBLISH_DIR, PUBLISH_OWNER
# shellcheck disable=SC1091
[ -f "$ROOT/tools/.publish.env" ] && . "$ROOT/tools/.publish.env"
HOST="${PUBLISH_HOST:?set PUBLISH_HOST in tools/.publish.env}"
DIR="${PUBLISH_DIR:?set PUBLISH_DIR in tools/.publish.env}"
OWNER="${PUBLISH_OWNER:?set PUBLISH_OWNER in tools/.publish.env}"

bash "$ROOT/site/build.sh"

pack() {
    tar -C "$ROOT/site" -czf - --owner=0 --group=0 --numeric-owner \
        --exclude='./_shots' --exclude='*.sh' --exclude='./dev' --exclude='__pycache__' .
}

if [ "${1:-}" = "--dry-run" ]; then
    tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
    pack > "$tmp"
    echo "Archive: $(wc -c < "$tmp") bytes, $(tar -tzf "$tmp" | grep -vc '/$') files -> $HOST:$DIR"
    exit 0
fi

# stdin carries the archive, so the remote script goes in the ssh command line
REMOTE="set -e
new=\"$DIR.new.\$\$\"
mkdir -p \"\$new\"
tar -xzf - -C \"\$new\"
chown -R $OWNER:$OWNER \"\$new\"
find \"\$new\" -type d -exec chmod 755 {} +
find \"\$new\" -type f -exec chmod 644 {} +
if [ -d \"$DIR\" ]; then mv \"$DIR\" \"$DIR.old.\$\$\"; fi
mv \"\$new\" \"$DIR\"
rm -rf \"$DIR.old.\$\$\"
echo published: \$(find \"$DIR\" -type f | wc -l) files in $DIR"
pack | ssh -o BatchMode=yes "$HOST" "bash -c $(printf '%q' "$REMOTE")"
