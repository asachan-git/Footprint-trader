"""Live-parity replay worker — replays captured poll bodies through THIS checkout's
code and dumps every command the server would return.

Run once per git ref (old vs new) into separate output files, then diff the two.
Byte-identical command streams prove the Phase-0 seam refactor changed no behaviour.

Usage:
    FB_DATA_DIR=<scratch> python scripts/parity_replay.py \
        --capture data/poll_capture.jsonl --out /tmp/cmds_new.jsonl

The driver scripts/parity_check.sh stashes to the base ref, runs this, restores,
runs this again, and diffs.

Determinism notes:
  - A clock source is installed that returns each poll's recorded recv_ts, so
    cooldowns / daily keys / reclaim windows resolve exactly as they did live.
  - FB_DATA_DIR must point at a scratch dir (footprint/ symlinked in) so arm-state
    and audit writes are isolated and identical-start.
  - Command ids are UUIDs (non-deterministic) — we strip them before dumping so the
    diff compares behaviour, not random ids.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Allow `python scripts/parity_replay.py` without PYTHONPATH=. (start.sh sets it).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Volatile / non-deterministic fields stripped before comparison.
_STRIP = {"id", "ts_created", "ts_sent", "ts"}


def _clean(cmd: dict) -> dict:
    return {k: v for k, v in cmd.items() if k not in _STRIP}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", required=True, help="poll_capture.jsonl from a live session")
    ap.add_argument("--out", required=True, help="where to dump the command stream")
    args = ap.parse_args()

    if not os.environ.get("FB_DATA_DIR"):
        print("refusing: FB_DATA_DIR must be a scratch dir", file=sys.stderr)
        return 2

    from execution import clock

    # Install a mutable clock the replay loop advances per poll.
    _state = {"now": 0.0}
    clock.set_source(lambda: _state["now"])

    from server.app import create_app, load_settings

    app = create_app(settings=load_settings(), start_background=False)
    client = app.test_client()

    cap = Path(args.capture)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    n_polls = n_cmds = 0
    with cap.open() as fh, out.open("w") as ofh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            _state["now"] = float(rec.get("recv_ts") or 0.0)
            body = rec.get("body") or {}
            resp = client.post("/exec/poll", json=body)
            n_polls += 1
            data = resp.get_json(silent=True) or {}
            # The poll returns queued commands under a stable key; capture whatever
            # command list it hands the EA, cleaned of volatile ids.
            cmds = data.get("commands") or data.get("cmds") or []
            for c in cmds:
                if isinstance(c, dict):
                    ofh.write(json.dumps(_clean(c), sort_keys=True,
                                         separators=(",", ":")) + "\n")
                    n_cmds += 1

    clock.reset()
    print(f"replayed {n_polls} polls → {n_cmds} commands → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
