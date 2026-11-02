#!/usr/bin/env bash
# Version marker of the admin's static files: writes web/admin/version.json (not in git). pi/deploy.sh and
# tools/remote.sh web run it right before they pack web/, so the device and the server's copy each carry the marker
# of the files they serve. The open admin reads it at startup and again later (web/admin/js/shell.js) and offers a
# reload when "version" changes: modules it has already loaded would otherwise stay old next to new pages.
#
#   "version"  hash of the files under web/admin (dev/ and version.json itself left out): changes only when they do
#   "commit"   git commit ("-dirty": uncommitted changes under web/admin); "built" — UTC time of writing
#
# No marker (dev, the mock server) — the admin does not check.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ADMIN="$ROOT/web/admin"
[ -d "$ADMIN" ] || { echo "no $ADMIN" >&2; exit 1; }

hash="$(cd "$ADMIN" && find . -type f ! -path './dev/*' ! -name 'version.json*' ! -name '*.pyc' -print0 \
    | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-16)"
commit="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
if [ "$commit" != unknown ] && [ -n "$(git -C "$ROOT" status --porcelain -- web/admin 2>/dev/null)" ]; then
    commit="$commit-dirty"
fi
built="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

printf '{"version": "%s", "commit": "%s", "built": "%s"}\n' "$hash" "$commit" "$built" > "$ADMIN/version.json.tmp"
mv -f "$ADMIN/version.json.tmp" "$ADMIN/version.json"
echo "web/admin/version.json: $hash ($commit)"
