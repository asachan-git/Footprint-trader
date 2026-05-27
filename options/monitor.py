"""Options position monitor — exits when underlying hits SL/TP or MIS time limit.

Runs as a polling loop (30s intervals during market hours). For each open
position tracked in position_store that has a matching entry in the options
sidecar, it:

  1. Fetches current underlying LTP from Dhan REST
  2. Checks SL/TP against underlying price (same logic as ingest.py SL/TP check)
  3. On trigger: places SELL order via DhanAdapter + marks position closed in store
  4. MIS time exit: at 15:15 IST (15 min before NSE forced square-off), closes all
     INTRA positions regardless of price

Run standalone:
  python3 -m options.monitor [--flask http://localhost:5000] [--interval 30]
"""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils.logging_config import setup as _setup_logging

_setup_logging()
LOG = logging.getLogger("options.monitor")

_IST = ZoneInfo("Asia/Kolkata")
_MIS_EXIT_HOUR = 15
_MIS_EXIT_MINUTE = 15  # 15:15 IST — 15 min before NSE forced close at 15:30


def _ist_now() -> datetime.datetime:
    return datetime.datetime.now(tz=_IST)


def _is_market_hours() -> bool:
    t = _ist_now()
    mins = t.hour * 60 + t.minute
    return 555 <= mins < 930  # 9:15–15:30 IST


def _past_mis_exit() -> bool:
    t = _ist_now()
    return t.hour > _MIS_EXIT_HOUR or (t.hour == _MIS_EXIT_HOUR and t.minute >= _MIS_EXIT_MINUTE)


def _get_underlying_ltp(symbol: str) -> float | None:
    try:
        from dhan.instruments import get as get_instr
        from dhan.option_chain import get_underlying_ltp
        instr = get_instr(symbol)
        return get_underlying_ltp(instr)
    except Exception as e:
        LOG.warning(f"[monitor] LTP fetch failed {symbol}: {e}")
        return None


def _close_position(pos, meta: dict, reason: str, realized_r: float) -> None:
    """Place SELL order + update position_store + remove sidecar."""
    from execution.position_store import position_store
    from options.position_sidecar import remove as sidecar_remove

    # Broker close
    live_mode = os.environ.get("ALLOW_LIVE") == "1"
    if live_mode and meta.get("security_id"):
        try:
            from execution.live.dhan_adapter import DhanAdapter
            da = DhanAdapter()
            da.close_option_position(
                security_id=meta["security_id"],
                quantity=int(meta.get("quantity", 0)),
                product_type=meta.get("product_type", "INTRA"),
            )
            LOG.info(f"[monitor] broker SELL sent: {meta['security_id']} qty={meta.get('quantity')}")
        except Exception as e:
            LOG.error(f"[monitor] broker close FAILED for {pos.position_id}: {e}")
    else:
        LOG.info(f"[monitor] paper/journal mode — skipping broker SELL for {pos.position_id}")

    # Mark closed in store
    try:
        position_store().close_position(pos.position_id, reason, realized_r)
    except Exception as e:
        LOG.error(f"[monitor] position_store close failed: {e}")

    # Remove sidecar
    try:
        sidecar_remove(pos.position_id)
    except Exception:
        pass

    # Notify
    try:
        from utils.notify import notify
        notify(
            "🔴 OPTION CLOSED",
            f"{meta.get('symbol', '')} {meta.get('option_type', '')} "
            f"{meta.get('strike', '')} {meta.get('expiry', '')}\n"
            f"{reason}  R={realized_r:+.2f}",
        )
    except Exception:
        pass


def check_once() -> list[dict]:
    """Single pass: check all open option positions. Returns list of exit events."""
    from execution.position_store import position_store
    from options.position_sidecar import all_open as sidecar_all

    exits = []
    sidecar = sidecar_all()
    ps = position_store()

    # Group open positions by symbol to batch LTP calls
    positions = ps.open_positions()  # all symbols
    by_symbol: dict[str, list] = {}
    for pos in positions:
        if pos.position_id in sidecar:
            by_symbol.setdefault(pos.symbol, []).append(pos)

    for symbol, sym_positions in by_symbol.items():
        ltp = _get_underlying_ltp(symbol)
        if ltp is None:
            LOG.warning(f"[monitor] can't get LTP for {symbol}, skipping")
            continue

        for pos in sym_positions:
            meta = sidecar.get(pos.position_id, {})
            if not meta:
                continue

            risk = abs(pos.avg_entry - pos.stop_loss) if pos.stop_loss else 0
            product = meta.get("product_type", "INTRA")

            # MIS time exit
            if product == "INTRA" and _past_mis_exit():
                reason = f"MIS time exit at {_ist_now().strftime('%H:%M')} IST"
                realized_r = (ltp - pos.avg_entry) / risk if risk > 0 and pos.side == "long" else \
                             (pos.avg_entry - ltp) / risk if risk > 0 else 0.0
                LOG.warning(f"[monitor] MIS time exit {pos.position_id} {symbol} LTP={ltp}")
                _close_position(pos, meta, reason, round(realized_r, 3))
                exits.append({"position_id": pos.position_id, "exit": "mis_time", "ltp": ltp})
                continue

            sl_hit = (
                (pos.side == "long"  and pos.stop_loss and ltp <= pos.stop_loss) or
                (pos.side == "short" and pos.stop_loss and ltp >= pos.stop_loss)
            )
            tp_hit = (
                (pos.side == "long"  and pos.take_profit and ltp >= pos.take_profit) or
                (pos.side == "short" and pos.take_profit and ltp <= pos.take_profit)
            )

            if sl_hit:
                realized_r = -1.0
                reason = f"sl_hit: underlying LTP={ltp:.2f} {'≤' if pos.side == 'long' else '≥'} SL={pos.stop_loss:.2f}"
                LOG.info(f"[monitor] SL hit {pos.position_id} {symbol} ltp={ltp}")
                _close_position(pos, meta, reason, realized_r)
                exits.append({"position_id": pos.position_id, "exit": "sl_hit", "ltp": ltp})

            elif tp_hit:
                realized_r = abs(pos.take_profit - pos.avg_entry) / risk if risk > 0 else 1.5
                reason = f"tp_hit: underlying LTP={ltp:.2f} {'≥' if pos.side == 'long' else '≤'} TP={pos.take_profit:.2f}"
                LOG.info(f"[monitor] TP hit {pos.position_id} {symbol} ltp={ltp} R={realized_r:.2f}")
                _close_position(pos, meta, reason, round(realized_r, 3))
                exits.append({"position_id": pos.position_id, "exit": "tp_hit", "ltp": ltp, "realized_r": realized_r})

    return exits


def run_loop(interval_s: float = 30.0) -> None:
    LOG.info(f"[monitor] starting options position monitor (interval={interval_s}s)")
    while True:
        if _is_market_hours():
            try:
                exits = check_once()
                if exits:
                    LOG.info(f"[monitor] {len(exits)} exits this pass: {exits}")
            except Exception as e:
                LOG.error(f"[monitor] check_once failed: {e}")
        else:
            LOG.debug("[monitor] outside market hours")
        time.sleep(interval_s)


def main() -> None:
    ap = argparse.ArgumentParser(description="Options position monitor")
    ap.add_argument("--interval", type=float, default=30.0, help="Poll interval in seconds")
    args = ap.parse_args()
    run_loop(args.interval)


if __name__ == "__main__":
    main()
