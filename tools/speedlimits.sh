#!/bin/bash
# Speed limits for the car screen: build the OpenStreetMap road database on the server and copy it to the device.
#
#   tools/speedlimits.sh                      # Poland (Geofabrik europe/poland), implicit limits PL:*
#   tools/speedlimits.sh europe/germany DE    # another Geofabrik extract + country code (zones only for PL so far)
#   tools/speedlimits.sh --copy               # only copy the database the server already has
#
# Server (tools/.remote.env: REMOTE_SSH, REMOTE_SSH_KEY): installs osmium-tool once, downloads the extract
# (only when Geofabrik has a newer one), keeps car roads, runs tools/speedlimits/build_db.py -> speedlimits.sqlite.
# Device (FNAV_HOST, default the ssh alias "fnav"; FNAV_SSH_OPTS): the file goes to /var/lib/fnav/speedlimits.sqlite,
# fnav-core picks it up within a minute (no restart). Data © OpenStreetMap contributors, ODbL.
set -euo pipefail
cd "$(dirname "$0")/.."
CALLER_HOST="${FNAV_HOST:-}" CALLER_OPTS="${FNAV_SSH_OPTS:-}"  # the caller's device address wins over .remote.env
set -a; source tools/.remote.env; set +a
COPY_ONLY=0
if [ "${1:-}" = "--copy" ]; then COPY_ONLY=1; shift; fi  # the server already has a fresh database: only copy it
EXTRACT="${1:-europe/poland}"
CC="${2:-PL}"
DEV="${CALLER_HOST:-${FNAV_HOST:-fnav}}"
read -r -a DEV_SSH <<< "${CALLER_OPTS:-${FNAV_SSH_OPTS:--o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15}}"
SRV=(ssh -o BatchMode=yes -o ServerAliveInterval=30 -i "$REMOTE_SSH_KEY" "$REMOTE_SSH")
WORK=/var/tmp/fnav-speedlimits
CLASSES=motorway,motorway_link,trunk,trunk_link,primary,primary_link,secondary,secondary_link,tertiary,tertiary_link,unclassified,residential,living_street

if [ "$COPY_ONLY" = 0 ]; then
echo "== server: osmium, $EXTRACT"
"${SRV[@]}" "command -v osmium >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y -q osmium-tool >/dev/null; mkdir -p $WORK"
"${SRV[@]}" "cat > $WORK/build_db.py" < tools/speedlimits/build_db.py
"${SRV[@]}" "cat > $WORK/export.json" < tools/speedlimits/export.json
"${SRV[@]}" "set -e; cd $WORK
    curl -fsSL -z extract.osm.pbf -o extract.osm.pbf https://download.geofabrik.de/$EXTRACT-latest.osm.pbf
    ls -la extract.osm.pbf
    nice osmium tags-filter --overwrite -o roads.osm.pbf extract.osm.pbf w/highway=$CLASSES
    nice osmium export --overwrite -c export.json -f geojsonseq -x print_record_separator=false \
        --geometry-types=linestring -i sparse_mem_array -o roads.geojsonseq roads.osm.pbf
    nice python3 build_db.py roads.geojsonseq speedlimits.sqlite --region $CC
    rm -f roads.osm.pbf roads.geojsonseq
    ls -la speedlimits.sqlite"

fi

# Copy through this PC. The link to the server may be slow or drop (mobile internet), so the compressed file is
# downloaded resumably into .devdata/speedlimits/ (a rerun continues where it stopped), and the device installs it
# only when the checksum matches: a cut-off file never replaces a good one.
echo "== download"
CACHE=.devdata/speedlimits
mkdir -p "$CACHE"
read -r SIZE GZSHA SHA <<< "$("${SRV[@]}" "cd $WORK && { [ speedlimits.sqlite.gz -nt speedlimits.sqlite ] || gzip -6 -c speedlimits.sqlite > speedlimits.sqlite.gz; } && echo \$(stat -c %s speedlimits.sqlite.gz) \$(sha256sum speedlimits.sqlite.gz | cut -d' ' -f1) \$(sha256sum speedlimits.sqlite | cut -d' ' -f1)")"
[ "$(cat "$CACHE/db.gz.sha" 2>/dev/null)" = "$GZSHA" ] || { rm -f "$CACHE/db.gz"; echo "$GZSHA" > "$CACHE/db.gz.sha"; }
for i in $(seq 1 100); do
    have=$(stat -c %s "$CACHE/db.gz" 2>/dev/null || echo 0)
    [ "$have" -ge "$SIZE" ] && break
    echo "  $((have / 1048576)) of $((SIZE / 1048576)) MB"
    "${SRV[@]}" "tail -c +$((have + 1)) $WORK/speedlimits.sqlite.gz" >> "$CACHE/db.gz" || sleep 10
done
echo "$GZSHA  $CACHE/db.gz" | sha256sum -c --quiet || { rm -f "$CACHE/db.gz"; echo "download corrupt, rerun" >&2; exit 1; }

echo "== device: $DEV"
ssh "${DEV_SSH[@]}" "$DEV" "sha=$SHA; set -e; t=\$(mktemp /tmp/speedlimits.XXXXXX); trap 'rm -f \"\$t\"' EXIT
    gunzip -c > \"\$t\"
    echo \"\$sha  \$t\" | sha256sum -c --quiet
    sudo install -o fnav -g fnav -m 0644 \"\$t\" /var/lib/fnav/speedlimits.sqlite.new
    sudo mv /var/lib/fnav/speedlimits.sqlite.new /var/lib/fnav/speedlimits.sqlite
    ls -la /var/lib/fnav/speedlimits.sqlite" < "$CACHE/db.gz"
echo "Done: fnav-core loads it within a minute."
