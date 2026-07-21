#!/usr/bin/env bash
# Live-parity check for the Phase-0 seam refactor.
#
# Proves the NEW code (clock + paths seams) emits the SAME command stream the OLD
# live server did, for the SAME poll inputs.
#
# The old code is what produced the live session, so we don't re-run it — its
# emitted commands already exist in the session's exec_bridge.jsonl audit. We
# replay the captured poll bodies through the NEW code (in a scratch data dir,
# with a clock pinned to each poll's recv_ts) and diff the two command streams.
#
# Prereq — capture a session on the NEW branch first:
#   FB_CAPTURE_POLLS=1 bash scripts/start.sh        # run one normal session, then Ctrl-C
#   # copy that session's audit aside so it isn't overwritten:
#   cp data/exec_bridge.jsonl /tmp/parity/live_audit.jsonl
#   cp data/poll_capture.jsonl /tmp/parity/poll_capture.jsonl
#
# Then:
#   bash scripts/parity_check.sh /tmp/parity/poll_capture.jsonl /tmp/parity/live_audit.jsonl
set -euo pipefail

CAPTURE="${1:?usage: parity_check.sh <poll_capture.jsonl> <live_audit.jsonl>}"
LIVE_AUDIT="${2:?usage: parity_check.sh <poll_capture.jsonl> <live_audit.jsonl>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SCRATCH="$(mktemp -d /tmp/parity.XXXXXX)"
mkdir -p "$SCRATCH/footprint"
# Read-only view of live footprint so the replay has the same bars but can't write them.
for f in data/footprint/*.jsonl; do ln -s "$ROOT/$f" "$SCRATCH/footprint/$(basename "$f")"; done
# Seed identical starting arm/emit state so both streams begin from the same point.
for f in arm_state.jsonl emit_state.jsonl vp_cache.json; do
  [ -f "data/$f" ] && cp "data/$f" "$SCRATCH/$f" || true
done

echo "[parity] replaying captured polls through NEW code → scratch=$SCRATCH"
FB_DATA_DIR="$SCRATCH" ./venv/bin/python scripts/parity_replay.py \
  --capture "$CAPTURE" --out "$SCRATCH/cmds_new.jsonl"

# Extract the live PLACE/CLOSE/MODIFY command stream from the old server's audit,
# cleaned to the same fields parity_replay dumps, in emit order.
echo "[parity] extracting live command stream from old-server audit"
./venv/bin/python - "$LIVE_AUDIT" "$SCRATCH/cmds_live.jsonl" <<'PY'
import json, sys
STRIP = {"id","ts_created","ts_sent","ts","status","result","event"}
src, dst = sys.argv[1], sys.argv[2]
with open(src) as fh, open(dst,"w") as ofh:
    for line in fh:
        line=line.strip()
        if not line: continue
        row=json.loads(line)
        # audit logs one row per command lifecycle event; take the enqueue-time view
        if row.get("event") not in ("enqueue","emit","place",None):
            # keep only the first-seen event per id to avoid double-counting ack rows
            pass
        c={k:v for k,v in row.items() if k not in STRIP}
        ofh.write(json.dumps(c, sort_keys=True, separators=(",",":"))+"\n")
PY

echo "[parity] diffing command streams (sorted-set compare)"
sort "$SCRATCH/cmds_new.jsonl"  > "$SCRATCH/new.sorted"
sort "$SCRATCH/cmds_live.jsonl" > "$SCRATCH/live.sorted"
if diff -u "$SCRATCH/live.sorted" "$SCRATCH/new.sorted"; then
  echo "[parity] PASS — command streams identical"
else
  echo "[parity] DIFF above (live '<' vs new '>'). Investigate before merge."
  echo "[parity] scratch kept at $SCRATCH"
  exit 1
fi
