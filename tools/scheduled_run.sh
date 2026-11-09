#!/bin/bash
# Record one fresh-receiver validation run at a set UTC time: probe-fresh, record, jt9 decode, score, manifest.
#   nohup caffeinate -dims tools/scheduled_run.sh run4 "2026-11-09 14:58:00" > run4.log 2>&1 &
set -euo pipefail
cd "$(dirname "$0")/.."
RUN=$1; AT=$(date -u -j -f "%Y-%m-%d %H:%M:%S" "$2" +%s)
echo "$(date -u +%FT%TZ) waiting for $2 UTC"
while [ "$(date -u +%s)" -lt "$AT" ]; do sleep 30; done
mkdir -p "$RUN"
export OUTDIR=$RUN PYTHONUNBUFFERED=1
RECEIVERS_JSON=receivers.json N_RX=${N_RX:-16} PER_BAND=${PER_BAND:-6} python3 tools/val.py probe-fresh > "${RUN}_probe.log" 2>&1
python3 tools/val.py record > "$RUN/record.log" 2>&1
python3 tools/val.py decode > "$RUN/decode.log" 2>&1
python3 tools/verify.py --recordings "$RUN/recordings" --spots "$RUN/pskr_spots.jsonl" --list > "$RUN/verify_jt9.log" 2>&1
python3 ../ft8-nn/collect/manifest.py make "$RUN" pskr_spots.jsonl selection.json session.json > "$RUN/MANIFEST.sha256"
echo "$(date -u +%FT%TZ) done"; grep '^TOTAL' "$RUN/verify_jt9.log"
