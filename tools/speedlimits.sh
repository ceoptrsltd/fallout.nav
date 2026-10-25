#!/bin/bash
# Speed limits for the car screen: build the OpenStreetMap road database on the server and copy it to the device.
#
#   tools/speedlimits.sh                      # Poland (Geofabrik europe/poland), implicit limits PL:*
#   tools/speedlimits.sh europe/germany DE    # another Geofabrik extract + country code (zones only for PL so far)
#
# Server (tools/.remote.env: REMOTE_SSH, REMOTE_SSH_KEY): installs osmium-tool once, downloads the extract
# (only when Geofabrik has a newer one), keeps car roads, runs tools/speedlimits/build_db.py -> speedlimits.sqlite.
# Device (FNAV_HOST, default the ssh alias "fnav"; FNAV_SSH_OPTS): the file goes to /var/lib/fnav/speedlimits.sqlite,
# fnav-core picks it up within a minute (no restart). Data © OpenStreetMap contributors, ODbL.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source tools/.remote.env; set +a
EXTRACT="${1:-europe/poland}"
CC="${2:-PL}"
DEV="${FNAV_HOST:-fnav}"
read -r -a DEV_SSH <<< "${FNAV_SSH_OPTS:--o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15}"
SRV=(ssh -o BatchMode=yes -o ServerAliveInterval=30 -i "$REMOTE_SSH_KEY" "$REMOTE_SSH")
WORK=/var/tmp/fnav-speedlimits
CLASSES=motorway,motorway_link,trunk,trunk_link,primary,primary_link,secondary,secondary_link,tertiary,tertiary_link,unclassified,residential,living_street

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

echo "== device: $DEV"
"${SRV[@]}" "cat $WORK/speedlimits.sqlite" | ssh "${DEV_SSH[@]}" "$DEV" \
    'set -e; t=$(mktemp /tmp/speedlimits.XXXXXX); cat > "$t"
     sudo install -o fnav -g fnav -m 0644 "$t" /var/lib/fnav/speedlimits.sqlite.new; rm -f "$t"
     sudo mv /var/lib/fnav/speedlimits.sqlite.new /var/lib/fnav/speedlimits.sqlite
     ls -la /var/lib/fnav/speedlimits.sqlite'
echo "Done: fnav-core loads it within a minute."
