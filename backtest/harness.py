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
from backtest.fill_engine import Broker


@dataclass
class Emitted:
    poll_ts: float
    cmd: dict


@dataclass
class HarnessResult:
    polls: int = 0
    commands: list[Emitted] = field(default_factory=list)
    broker: object | None = None      # backtest.fill_engine.Broker after a run

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
    poll_tf: str = "1m",
    balance: float = 100_000.0,
) -> HarnessResult:
    """Replay one session-day, polling on each `poll_tf` bar close.

    poll_tf drives the poll cadence; the arm scanners internally read every TF's
    store bars as-of the clock, so one cadence exercises 1m..15m arms.

    Default 1m, not 5m: the live EA polls on a ~1s timer, so intrabar touch-arms fire
    far finer than any bar cadence. At 5m the harness misses taps that occur inside the
    bar and arms the right magic at the wrong time (median gap to the matching live arm
    was ~39 min). Measured on 2026-07-20: G1 27.0% at 5m vs 33.0% at 1m. 1m is the
    finest cadence the stored footprint supports.
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
    # 1m bars give the broker a finer intrabar path than the poll cadence: between two
    # 5m polls we step every 1m bar, so fewer bars are range-ambiguous for SL-vs-TP.
    path_bars = _bar_closes(analysis_symbol, "1m", start_ts, end_ts)
    path_i = 0

    broker = Broker(symbol=broker_symbol, stops_dist=stops_pts * point)
    res.broker = broker

    with vp_asof(replay_cache), store_asof():
        for bar in cadence_bars:
            ts = bar.close_ts

            # Advance the broker over every 1m bar up to this poll instant. Prices are
            # shifted into the venue frame so fills happen where the legs actually sit.
            while path_i < len(path_bars) and path_bars[path_i].close_ts <= ts:
                pb = path_bars[path_i]
                broker.step_bar(pb.ohlc.o + venue_offset, pb.ohlc.h + venue_offset,
                                pb.ohlc.l + venue_offset, pb.ohlc.c + venue_offset,
                                pb.close_ts)
                path_i += 1

            clock.set_source(lambda ts=ts: float(ts))
            # venue quote = analysis close shifted to venue frame
            mid = bar.ohlc.c + venue_offset
            spread = point  # nominal 1-point; real spread is Phase 2d
            tot = broker.totals(mid)
            body = {
                "account": account,
                "symbol": broker_symbol,
                "bid": round(mid - spread / 2, 5),
                "ask": round(mid + spread / 2, 5),
                "point": point,
                "stops_pts": stops_pts,
                "balance": balance,
                "equity": round(balance + broker.realized + broker.floating(mid), 2),
                "buys": tot["buys"], "sells": tot["sells"], "pendings": tot["pendings"],
                "pnl": tot["pnl"],
                "magics": broker.magics_json(mid),
            }
            resp = client.post("/exec/poll", json=body)
            res.polls += 1
            data = resp.get_json(silent=True) or {}
            cmds = data.get("commands", [])

            acks = []
            for c in cmds:
                res.commands.append(Emitted(poll_ts=float(ts), cmd=c))
                acks.append(broker.apply_command(c, mid, int(ts)))
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
    ap.add_argument("--poll-tf", default="1m")
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
    b = r.broker
    if b is not None:
        import collections as _c
        ev = _c.Counter(e.kind for e in b.events)
        print(f"broker: events={dict(ev)}")
        print(f"        open_positions={len(b.positions)} resting_pendings={len(b.pendings)}")
        # GROSS — no spread/commission/swap/margin yet (Phase 2d). Not a result.
        print(f"        realized(GROSS, no costs)={round(b.realized, 2)}")
    if args.out:
        # poll_ts is required to group legs into arms downstream (legs of one arm are
        # emitted in the same poll; the per-leg `comment` tag is NOT an arm key).
        Path(args.out).write_text(
            "\n".join(json.dumps({"poll_ts": e.poll_ts, **e.cmd}) for e in r.commands))
        print(f"→ {args.out}")
