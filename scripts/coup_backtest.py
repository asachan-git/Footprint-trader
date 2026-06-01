#!/usr/bin/env python3
"""Coup A/B sweep + trade/candidate emitters on historical 15m footprint.

Sweep (default): for each entry_mode × sl_mode combo, walk bars, run Coup.decide,
simulate fill + exit, print n / fill% / win% / avgR / ΣR.
  - FILL: close = market at signal-bar close; imbalance/lvn/range = limit into the
    trigger candle, filled only if a later bar (≤ fill_within) touches the level.
  - EXIT (15m resolution, SL-first conservative): flip / sl / tp(2R) / hold(max_hold).
  - one open trade at a time (mirrors the manager's one-cycle-per-symbol).
R = sign·(exit − entry) / |entry − sl|.

  python scripts/coup_backtest.py [SYMBOL] [TF]                 # sweep table
  python scripts/coup_backtest.py SYMBOL TF --emit ENTRY SL     # write backtest_trades.jsonl
  python scripts/coup_backtest.py SYMBOL TF --candidates        # write candidates.jsonl

Limitation: 15m bars can't order SL vs TP within one bar — SL assumed first (worst
case). Limit fills assume touch=fill (no queue/slippage). Relative ranking only.
"""
from __future__ import annotations

import json
import sys
import statistics
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import logging
logging.disable(logging.CRITICAL)

from pipeline.state_store import store
from pipeline.footprint import build as build_fp
from pipeline.features.absorption import detect_canonical_absorption
from pipeline.features.atr import atr
import strategies.coup as coup_mod
from strategies.coup import Coup

ENTRY_MODES = ["close", "imbalance", "lvn", "range"]
SL_MODES = ["candle", "swing", "imbalance"]
FILL_WITHIN = 4     # bars a limit entry has to fill
MAX_HOLD = 12       # bars before time-stop
CONT_N = 6          # forward window for the continuation label
CONT_ATR = 1.0      # winner-dir move ≥ this × ATR within CONT_N = continuation

COUP_DIR = ROOT / "data" / "strategies" / "coup"


class _Slice:
    def __init__(self, bars, upto):
        self._bars, self._upto = bars, upto

    def recent(self, sym, tf, n):
        return self._bars[:self._upto][-n:]


def _patch_store(bars, upto):
    """Point coup's `store()` at a historical slice (no future leak)."""
    coup_mod.store = (lambda b, u: (lambda: _Slice(b, u)))(bars, upto)


def _flip_against(side: str, bar) -> bool:
    sides = {a.side for a in detect_canonical_absorption(bar, build_fp(bar))}
    return (side == "long" and "sell" in sides) or (side == "short" and "buy" in sides)


def _simulate(side, entry, sl, tp, future) -> tuple[float, str, int, float] | None:
    """Walk forward bars (already past fill). Return (R, reason, exit_idx, exit_price)."""
    risk = abs(entry - sl) or 1e-9
    for k, b in enumerate(future):
        if k >= MAX_HOLD:
            r = ((b.ohlc.c - entry) if side == "long" else (entry - b.ohlc.c)) / risk
            return r, "hold", k, b.ohlc.c
        if side == "long" and b.ohlc.l <= sl:
            return -abs(entry - sl) / risk, "sl", k, sl
        if side == "short" and b.ohlc.h >= sl:
            return -abs(sl - entry) / risk, "sl", k, sl
        if side == "long" and b.ohlc.h >= tp:
            return abs(tp - entry) / risk, "tp", k, tp
        if side == "short" and b.ohlc.l <= tp:
            return abs(entry - tp) / risk, "tp", k, tp
        if _flip_against(side, b):
            r = ((b.ohlc.c - entry) if side == "long" else (entry - b.ohlc.c)) / risk
            return r, "flip", k, b.ohlc.c
    return None


def _try_fill(side, entry, future) -> int | None:
    for k, b in enumerate(future[:FILL_WITHIN]):
        if b.ohlc.l <= entry <= b.ohlc.h:
            return k
    return None


def _run_combo(symbol, tf, bars, em, sm) -> tuple[int, int, list[dict]]:
    """Return (signals, filled, trades[]) for one entry_mode × sl_mode combo."""
    inst = Coup(config={"symbols": [symbol], "entry_mode": em, "sl_mode": sm})
    signals = filled = 0
    trades: list[dict] = []
    open_until = -1
    for i in range(30, len(bars) - 1):
        if i <= open_until:
            continue
        _patch_store(bars, i + 1)
        d = inst.decide(symbol, tf, bars[i], {})
        if not d:
            continue
        signals += 1
        future = bars[i + 1:]
        if not future:
            continue
        if em == "close":
            fill_k, entry, entry_idx = 0, float(d.entry), i
        else:
            fk = _try_fill(d.side, float(d.entry), future)
            if fk is None:
                continue                        # limit never filled
            fill_k, entry, entry_idx = fk, float(d.entry), i + 1 + fk
        filled += 1
        risk = abs(entry - float(d.stop_loss)) or 1e-9
        tp = entry + 2 * risk if d.side == "long" else entry - 2 * risk
        res = _simulate(d.side, entry, float(d.stop_loss), tp, future[fill_k + 1:])
        if res is None:
            continue
        r, reason, exit_k, exit_price = res
        exit_idx = i + 2 + fill_k + exit_k
        trades.append({
            "symbol": symbol, "tf": tf, "side": d.side,
            "entry_ts": bars[entry_idx].close_ts, "entry": round(entry, 2),
            "sl": round(float(d.stop_loss), 2), "tp": round(tp, 2),
            "exit_ts": bars[exit_idx].close_ts, "exit_price": round(exit_price, 2),
            "reason": reason, "r": round(r, 3),
            "entry_mode": em, "sl_mode": sm, "source": "backtest",
        })
        open_until = exit_idx
    return signals, filled, trades


def run(symbol: str, tf: str):
    bars = store().recent(symbol, tf, 10_000_000)
    print(f"{symbol} {tf}: {len(bars)} bars\n")
    if len(bars) < 60:
        print("not enough bars"); return
    header = (f"{'entry':9s} {'sl':9s} {'n':>3s} {'fill%':>6s} {'win%':>6s} "
              f"{'avgR':>7s} {'sumR':>8s}  reasons")
    print(header); print("-" * len(header))
    for em, sm in product(ENTRY_MODES, SL_MODES):
        signals, filled, trades = _run_combo(symbol, tf, bars, em, sm)
        if not trades:
            print(f"{em:9s} {sm:9s} {signals:>3d} {'--':>6s} {'--':>6s} "
                  f"{'--':>7s} {'--':>8s}  no fills")
            continue
        rs = [t["r"] for t in trades]
        reasons = {}
        for t in trades:
            reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
        wins = sum(1 for r in rs if r > 0)
        rstr = " ".join(f"{k}:{v}" for k, v in sorted(reasons.items()))
        print(f"{em:9s} {sm:9s} {len(rs):>3d} {100*filled/max(signals,1):>5.0f}% "
              f"{100*wins/len(rs):>5.0f}% {statistics.mean(rs):>7.3f} {sum(rs):>8.2f}  {rstr}")


def emit_trades(symbol: str, tf: str, em: str, sm: str):
    bars = store().recent(symbol, tf, 10_000_000)
    if len(bars) < 60:
        print("not enough bars"); return
    _, _, trades = _run_combo(symbol, tf, bars, em, sm)
    COUP_DIR.mkdir(parents=True, exist_ok=True)
    out = COUP_DIR / "backtest_trades.jsonl"
    # overwrite for this symbol/tf/combo; keep other symbols' rows
    keep = []
    if out.exists():
        for line in out.open():
            if not line.strip():
                continue
            row = json.loads(line)
            if not (row.get("symbol") == symbol and row.get("tf") == tf):
                keep.append(row)
    with out.open("w") as fh:
        for row in keep + trades:
            fh.write(json.dumps(row) + "\n")
    print(f"emit_trades: {len(trades)} {symbol} {tf} {em}+{sm} → {out}")


def emit_candidates(symbol: str, tf: str):
    """Log EVERY trigger candidate (accepted+rejected) + forward outcome label."""
    bars = store().recent(symbol, tf, 10_000_000)
    if len(bars) < 60:
        print("not enough bars"); return
    inst = Coup(config={"symbols": [symbol]})
    cfg = inst.config
    vol_mult = float(cfg.get("vol_mult", 1.8))
    delta_ratio = float(cfg.get("delta_ratio", 0.35))
    confirm_within = int(cfg.get("confirm_within", 3))
    rows = []
    for i in range(30, len(bars) - 1):
        b = bars[i]
        fp = build_fp(b)
        total = fp.total_bid + fp.total_ask
        prev = [t for t in ((build_fp(x).total_bid + build_fp(x).total_ask)
                            for x in bars[i - 20:i]) if t > 0]
        med = statistics.median(prev) if prev else 0.0
        if med <= 0 or total < vol_mult * med:
            continue
        if b.delta is None or abs(b.delta) / max(total, 1e-9) < delta_ratio:
            continue
        absorbs = detect_canonical_absorption(b, fp)
        if not absorbs:
            continue
        winner = "short" if absorbs[-1].side == "sell" else "long"
        post = bars[i + 1:i + 1 + confirm_within]
        _patch_store(bars, i + 1 + len(post))
        confirmed = bool(post) and inst._confirm(winner, b, post, bars[:i + 1 + len(post)])
        # forward outcome label
        a = atr(bars[:i + 1]) or 0.0
        fwd = bars[i + 1:i + 1 + CONT_N]
        c0 = b.ohlc.c
        if fwd and c0 > 0:
            mfe = (max(x.ohlc.h for x in fwd) - c0) if winner == "long" else (c0 - min(x.ohlc.l for x in fwd))
            mae = (c0 - min(x.ohlc.l for x in fwd)) if winner == "long" else (max(x.ohlc.h for x in fwd) - c0)
            cont = a > 0 and mfe >= CONT_ATR * a
            r3 = (bars[i + 3].ohlc.c - c0) / c0 if i + 3 < len(bars) else None
            r6 = (bars[i + 6].ohlc.c - c0) / c0 if i + 6 < len(bars) else None
            if winner == "short":
                r3 = -r3 if r3 is not None else None
                r6 = -r6 if r6 is not None else None
        else:
            mfe = mae = 0.0; cont = False; r3 = r6 = None
        rows.append({
            "symbol": symbol, "tf": tf, "trigger_ts": b.close_ts, "winner": winner,
            "total_vol": round(total, 2), "vol_ratio": round(total / med, 2),
            "delta_ratio": round(abs(b.delta) / max(total, 1e-9), 3),
            "absorption_side": absorbs[-1].side, "confirmed": confirmed,
            "fwd_ret_3": round(r3, 5) if r3 is not None else None,
            "fwd_ret_6": round(r6, 5) if r6 is not None else None,
            "fwd_mfe_atr": round(mfe / a, 2) if a > 0 else None,
            "fwd_mae_atr": round(mae / a, 2) if a > 0 else None,
            "continuation": bool(cont),
        })
    COUP_DIR.mkdir(parents=True, exist_ok=True)
    out = COUP_DIR / "candidates.jsonl"
    with out.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    print(f"emit_candidates: {len(rows)} triggers {symbol} {tf} → {out}")


if __name__ == "__main__":
    sym = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "BTCUSDT"
    tf = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("-") else "15m"
    if "--emit" in sys.argv:
        j = sys.argv.index("--emit")
        emit_trades(sym, tf, sys.argv[j + 1], sys.argv[j + 2])
    elif "--candidates" in sys.argv:
        emit_candidates(sym, tf)
    else:
        run(sym, tf)
