#!/usr/bin/env bash
# Builds the publishable project page: copies the live display (web/display/, without dev/) into site/display/.
# Usage (from anywhere, Git Bash / Linux / macOS):  bash site/build.sh
# Publish the contents of site/ except _shots/ (and _work/, if present).
set -euo pipefail

SITE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SITE/../web/display"
DST="$SITE/display"

[ -f "$SRC/index.html" ] || { echo "error: $SRC/index.html not found" >&2; exit 1; }

rm -rf "$DST"
mkdir -p "$DST"
# copy everything except the dev/ folder (tar keeps the tree; works without rsync)
(cd "$SRC" && tar --exclude='./dev' --exclude='__pycache__' -cf - .) | (cd "$DST" && tar -xf -)

echo "site/display/ <- web/display/ ($(find "$DST" -type f | wc -l | tr -d ' ') files, dev/ excluded)"
