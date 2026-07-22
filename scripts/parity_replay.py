"""Live-parity replay worker — replays captured poll bodies through THIS checkout's
code and dumps every command the server would return.

Run once per git ref (old vs new) into separate output files, then diff the two.

*** KNOWN LIMITATION — READ BEFORE TRUSTING A DIFF ***
This does NOT produce a clean pass against a live audit, and cannot, because the
server is stateful while the capture is a fixed recording:

  - The recorded `magics[]` describes the LIVE server's positions. The replay server
    arms its own cycles, and after the first divergence the recorded state no longer
    corresponds to anything the replay did.
  - The replay holds no positions (it acks commands but nothing fills), so its cycles
    never progress to an exit and it keeps arming.

Measured on a 21.8 min capture (977 polls): live enqueued 154 legs across 25 arm
batches; the replay produced 873 legs — 5.7x over-arming — even though the set of
magics matched almost exactly (12 of 13; the missing one is a candle_sweep magic that
needs venue bars). Acking commands cut the replay from 38,758 to 1,038, but the
residual gap is structural, not a missing ack.

Use this to compare TWO CHECKOUTS against each other (same capture, same divergence,
so the divergence cancels) — that is a valid A/B. Do not read it as "replay reproduces
live". For a real fill-driven comparison use backtest/harness.py + fidelity_check.py.

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

# Comparison field set. The audit log dumps the whole Command dataclass (every field,
# always, including `account`), while poll's to_wire() emits only the fields relevant to
# each command type and never `account`. Diffing raw would fail on every line for purely
# structural reasons, so BOTH sides are normalised to these keys, with absent/blank
# treated as a neutral default.
_FIELDS = ("type", "symbol", "magic", "order_type", "price", "lot", "sl", "tp",
           "side", "frac", "comment")
_DEFAULTS = {"order_type": "", "price": 0.0, "lot": 0.0, "sl": 0.0, "tp": 0.0,
             "side": "", "frac": 0.0, "comment": "", "symbol": "", "magic": 0, "type": ""}


def _clean(cmd: dict) -> dict:
    """Normalise a command (wire-form or audit row) to the shared comparison shape."""
    out = {}
    for k in _FIELDS:
        v = cmd.get(k, _DEFAULTS[k])
        if v is None:
            v = _DEFAULTS[k]
        if isinstance(_DEFAULTS[k], float):
            v = round(float(v or 0.0), 5)
        elif isinstance(_DEFAULTS[k], int) and not isinstance(v, bool):
            v = int(v or 0)
        out[k] = v
    return out


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
    seen_ids: set[str] = set()
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
            acks = []
            for c in cmds:
                if not isinstance(c, dict):
                    continue
                cid = c.get("id")
                # Record each command ONCE, by id. poll() re-sends an IN_FLIGHT command
                # after _RECLAIM_AFTER_S, and the live audit logs one `enqueue` row per
                # command, so counting every re-send would inflate the replay side and
                # guarantee a false diff.
                if cid and cid in seen_ids:
                    continue
                if cid:
                    seen_ids.add(cid)
                ofh.write(json.dumps(_clean(c), sort_keys=True,
                                     separators=(",", ":")) + "\n")
                n_cmds += 1
                if cid:
                    acks.append({"id": cid, "ok": True, "ticket": 0, "retcode": 10009})
            # Ack every command so the server marks it DONE. Without this it stays
            # PENDING, is re-sent on the next poll, and the cycle never progresses —
            # which produced 38,758 replay commands against live's 223.
            if acks:
                client.post("/exec/ack",
                            json={"account": body.get("account"), "results": acks})

    clock.reset()
    print(f"replayed {n_polls} polls → {n_cmds} distinct commands → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
