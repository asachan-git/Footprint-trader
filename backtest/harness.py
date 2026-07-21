"""Phase-1 grid backtest driver — arm fidelity, NO P&L.

Simulated EA: walks historical bars, and at each step sets the simulated clock,
truncates the bar store to that instant, POSTs a synthetic /exec/poll through the
REAL Flask route (test_client), and records every command the server emits. A NULL
fill engine acks nothing and holds no positions — deliberately. The only question
this answers is: does the real arm/planner stack, driven point-in-time, reproduce
the arms and geometry the live server actually produced (see fidelity_check.py)?

Everything that decides an arm — zone_triggers, grid_planner, the _*_arm_tf drivers,
monitor_cycle — is unmodified production code. The harness supplies inputs and the
clock; it contains no trading logic.

Isolation: requires FB_DATA_DIR (scratch, with footprint symlinked). vp reads are
made point-in-time by backtest.seams.vp_asof.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from execution import clock
from backtest.seams import vp_asof, store_asof


@dataclass
class Emitted:
    poll_ts: float
    cmd: dict


@dataclass
class HarnessResult:
    polls: int = 0
    commands: list[Emitted] = field(default_factory=list)

    def arms(self) -> list[dict]:
        """PLACE_PENDING commands = grid arms (one batch per armed cycle)."""
        return [e.cmd for e in self.commands if e.cmd.get("type") == "PLACE_PENDING"]


def _bar_closes(symbol: str, tf: str, start_ts: int, end_ts: int) -> list:
    """Ascending bars in [start,end] from the (scratch) store."""
    from pipeline.state_store import store as _store
    bars = _store().recent(symbol, tf, 1_000_000)
    return [b for b in bars if start_ts <= b.close_ts <= end_ts]


def run(
    *,
    account: str,
    analysis_symbol: str,
    broker_symbol: str,
    day_key: str,
    replay_cache: str,
    settings: dict,
    venue_offset: float,
    poll_tf: str = "5m",
) -> HarnessResult:
    """Replay one session-day, polling on each `poll_tf` bar close.

    poll_tf drives the poll cadence; the arm scanners internally read every TF's
    store bars as-of the clock, so a 5m cadence exercises 1m/3m/5m/10m/15m arms
    that would have fired by each 5m close.
    """
    if not os.environ.get("FB_DATA_DIR"):
        raise RuntimeError("harness requires FB_DATA_DIR (scratch)")

    from pipeline.features import vp_cache as vpc
    from server.app import create_app, load_settings

    # day bounds via the real anchor helper
    anchor = vpc._normalize_anchor((settings.get("vp_cache") or {}).get("session_start_utc", {}).get(analysis_symbol, 0))
    start_ts, end_ts = vpc._day_bounds(day_key, anchor)

    app = create_app(settings=settings or load_settings(), start_background=False)
    client = app.test_client()

    res = HarnessResult()
    point = 0.01              # XAUUSD+ point
    stops_pts = 0.0           # freeze band unknown offline → 0 (Phase-2 fill engine models it)

    cadence_bars = _bar_closes(analysis_symbol, poll_tf, start_ts, end_ts)

    # Pending-fill model: once a magic arms, its legs REST as unfilled pendings so the
    # cycle persists — otherwise empty magics[] makes the absent-magic reap retire it and
    # it re-arms every bar. This holds Phase-1's question at "did it arm once, correctly".
    # magic -> pending leg count. Actual fills (pending→position) are Phase 2.
    pending_by_magic: dict[int, int] = {}

    with vp_asof(replay_cache), store_asof():
        for bar in cadence_bars:
            ts = bar.close_ts
            clock.set_source(lambda ts=ts: float(ts))
            # venue quote = analysis close shifted to venue frame
            mid = bar.ohlc.c + venue_offset
            spread = point  # nominal 1-point; Phase-2 models real spread
            magics_arr = [
                {"magic": mg, "buys": 0, "sells": 0, "pendings": n}
                for mg, n in pending_by_magic.items() if n > 0
            ]
            tot_pend = sum(pending_by_magic.values())
            body = {
                "account": account,
                "symbol": broker_symbol,
                "bid": round(mid - spread / 2, 5),
                "ask": round(mid + spread / 2, 5),
                "point": point,
                "stops_pts": stops_pts,
                "buys": 0, "sells": 0, "pendings": tot_pend,
                "magics": magics_arr,
            }
            resp = client.post("/exec/poll", json=body)
            res.polls += 1
            data = resp.get_json(silent=True) or {}
            cmds = data.get("commands", [])
            for c in cmds:
                res.commands.append(Emitted(poll_ts=float(ts), cmd=c))
                # reflect placed/cancelled pendings so the next poll's magics[] holds them
                mg = int(c.get("magic", 0) or 0)
                ctype = c.get("type")
                if ctype == "PLACE_PENDING" and mg:
                    pending_by_magic[mg] = pending_by_magic.get(mg, 0) + 1
                elif ctype in ("CANCEL_PENDINGS", "CLOSE_ALL") and mg:
                    pending_by_magic[mg] = 0
            # Null-fill ack: mark every emitted command DONE so poll() doesn't re-emit
            # it on the next cadence (IN_FLIGHT reclaim would otherwise re-return the
            # whole queue). We record the FIRST emission as the arm — that's the arm
            # fidelity signal; the fill outcome is Phase 2.
            acks = [{"id": c.get("id"), "ok": True, "ticket": 0, "retcode": 10009}
                    for c in cmds if c.get("id")]
            if acks:
                client.post("/exec/ack", json={"account": account, "results": acks})

    clock.reset()
    return res


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True)
    ap.add_argument("--analysis-symbol", default="XAUTUSDT")
    ap.add_argument("--broker-symbol", default="XAUUSD+")
    ap.add_argument("--day", required=True, help="session-day key YYYY-MM-DD")
    ap.add_argument("--replay-cache", required=True)
    ap.add_argument("--offset", type=float, default=0.0)
    ap.add_argument("--poll-tf", default="5m")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    from server.app import load_settings
    settings = load_settings()
    r = run(account=args.account, analysis_symbol=args.analysis_symbol,
            broker_symbol=args.broker_symbol, day_key=args.day,
            replay_cache=args.replay_cache, settings=settings,
            venue_offset=args.offset, poll_tf=args.poll_tf)
    arms = r.arms()
    print(f"polls={r.polls} commands={len(r.commands)} arms(PLACE_PENDING)={len(arms)}")
    if args.out:
        # poll_ts is required to group legs into arms downstream (legs of one arm are
        # emitted in the same poll; the per-leg `comment` tag is NOT an arm key).
        Path(args.out).write_text(
            "\n".join(json.dumps({"poll_ts": e.poll_ts, **e.cmd}) for e in r.commands))
        print(f"→ {args.out}")
