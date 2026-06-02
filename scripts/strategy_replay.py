#!/usr/bin/env python3
"""Strategy replay harness — simulate democracy / republic / coup on historical 15m bars.

Walks closed 15m footprint bars for each symbol, asks each strategy to .decide() at the
cutoff (no future leak via a monkeypatched store.recent), simulates the trade forward bar
by bar (first-touch SL/TP, else time-stop at MAX_HOLD close), and records per-trade R plus
the strategy's own rationale/confidence/bias. Writes data/strategies/replay_trades.json and
prints a per-strategy×symbol summary.

Caveats (relative ranking only, not live-exact):
  - 15m resolution can't order SL vs TP inside one bar → SL assumed first (worst case).
  - republic SL is the ATR-tightened stop recomputed here (decide() inherits democracy's
    placeholder ±5% SL; the real tightening lives in adjust_plan on a GridPlan we don't
    build). We apply SL = entry ∓ sl_atr_mult×ATR15, used only when tighter than decide().
  - democracy/republic fill at the signal bar close (market). coup uses d.entry as a limit
    and is filled only if a later bar (≤ FILL_WITHIN) trades through it (mirrors range mode).
  - one open trade at a time per strategy×symbol (mirrors one-cycle-per-symbol).

Run: .venv/bin/python scripts/strategy_replay.py
"""
from __future__ import annotations

import json
import logging
import statistics
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.disable(logging.CRITICAL)

import yaml

from pipeline.state_store import store
from pipeline.features.atr import atr
from strategies.democracy import Democracy
from strategies.republic import Republic
from strategies.coup import Coup

SYMBOLS = ["BTCUSDT", "XAUTUSDT"]
TF = "15m"
WARMUP = 30          # bars of history before the first decision
MAX_HOLD = 16        # bars before the time-stop
FILL_WITHIN = 4      # bars a coup limit entry has to fill
DEFAULT_RR = 2.0     # TP fallback if a strategy gives no usable take_profit
IST = timezone(timedelta(hours=5, minutes=30))

OUT = ROOT / "data" / "strategies" / "replay_trades.json"


# ── store time-travel: every strategy sees only bars up to CUT["ts"] ──────────
S = store()
_orig = S.recent
CUT = {"ts": None}


def _recent(symbol, tf, n):
    bars = _orig(symbol, tf, 10_000_000)
    if CUT["ts"] is not None:
        bars = [b for b in bars if b.close_ts <= CUT["ts"]]
    return bars[-n:] if n else bars


S.recent = _recent


def _ist(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=IST).strftime("%Y-%m-%d %H:%M IST")


def _atr15(symbol: str, bars: list, upto: int) -> float:
    """ATR over the 15 bars ending at index upto (inclusive)."""
    window = bars[max(0, upto - 14): upto + 1]
    return atr(window, period=14)


def _simulate(side: str, entry: float, sl: float, tp: float, future: list):
    """Walk forward bars. Return (r, reason, exit_idx_offset, exit_price). SL-first."""
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
    # ran out of bars
    last = future[-1] if future else None
    if last is None:
        return None
    r = ((last.ohlc.c - entry) if side == "long" else (entry - last.ohlc.c)) / risk
    return r, "hold", len(future) - 1, last.ohlc.c


def _coup_fill(side: str, entry: float, future: list):
    """coup limit: filled at the first of the next FILL_WITHIN bars that trades through."""
    for k, b in enumerate(future[:FILL_WITHIN]):
        if b.ohlc.l <= entry <= b.ohlc.h:
            return k
    return None


def _load_configs() -> dict:
    raw = yaml.safe_load((ROOT / "config" / "strategies.yaml").read_text())
    out = {}
    for entry in raw.get("strategies", []):
        out[entry["name"]] = entry.get("config") or {}
    return out


def _replay_one(name: str, strat, symbol: str, errors: dict) -> list[dict]:
    bars = _orig(symbol, TF, 10_000_000)
    trades: list[dict] = []
    if len(bars) < WARMUP + 4:
        return trades
    open_until = -1
    is_coup = name == "coup"
    sl_atr_mult = float(getattr(strat, "config", {}).get("sl_atr_mult", 1.5)) if name == "republic" else None

    for i in range(WARMUP, len(bars) - 2):
        if i <= open_until:
            continue
        CUT["ts"] = bars[i].close_ts
        try:
            d = strat.decide(symbol, TF, bars[i], {})
        except Exception as e:  # noqa: BLE001
            errors[name] = errors.get(name, 0) + 1
            errors[f"{name}:last"] = f"decide {symbol}@{i}: {type(e).__name__}: {e}"
            continue
        if not d or d.side not in ("long", "short") or d.entry is None:
            continue

        future = bars[i + 1:]
        if not future:
            continue

        try:
            if is_coup:
                fk = _coup_fill(d.side, float(d.entry), future)
                if fk is None:
                    continue  # limit never filled
                entry = float(d.entry)
                entry_idx = i + 1 + fk
                sim_future = future[fk + 1:]
            else:
                # market fill at the signal bar close
                entry = float(bars[i].ohlc.c)
                entry_idx = i
                sim_future = future

            # ── SL ──
            sl = float(d.stop_loss) if d.stop_loss is not None else None
            if name == "republic":
                a = _atr15(symbol, bars, i)
                if a > 0:
                    tightened = entry - sl_atr_mult * a if d.side == "long" else entry + sl_atr_mult * a
                    # use only if tighter (closer to entry) than decide's placeholder
                    if sl is None:
                        sl = tightened
                    elif d.side == "long":
                        sl = max(sl, tightened)
                    else:
                        sl = min(sl, tightened)
            if sl is None:
                continue
            risk = abs(entry - sl)
            if risk <= 0:
                continue

            # ── TP ──
            tp = float(d.take_profit) if d.take_profit is not None else None
            # democracy/republic decide() emits a placeholder ±5% TP; replace with RR target
            # so R math is meaningful. coup already emits a real 2R TP — keep it.
            if not is_coup or tp is None:
                tp = entry + DEFAULT_RR * risk if d.side == "long" else entry - DEFAULT_RR * risk
            # guard wrong-side TP
            if (d.side == "long" and tp <= entry) or (d.side == "short" and tp >= entry):
                tp = entry + DEFAULT_RR * risk if d.side == "long" else entry - DEFAULT_RR * risk

            res = _simulate(d.side, entry, sl, tp, sim_future)
            if res is None:
                continue
            r, reason, exit_off, exit_price = res
            exit_idx = entry_idx + 1 + exit_off
            exit_idx = min(exit_idx, len(bars) - 1)
        except Exception as e:  # noqa: BLE001
            errors[name] = errors.get(name, 0) + 1
            errors[f"{name}:last"] = f"sim {symbol}@{i}: {type(e).__name__}: {e}"
            continue

        trades.append({
            "strategy": name,
            "symbol": symbol,
            "side": d.side,
            "entry_ts": bars[entry_idx].close_ts,
            "entry_ist": _ist(bars[entry_idx].close_ts),
            "entry": round(entry, 4),
            "sl": round(sl, 4),
            "tp": round(tp, 4),
            "exit_ts": bars[exit_idx].close_ts,
            "exit_ist": _ist(bars[exit_idx].close_ts),
            "exit_price": round(float(exit_price), 4),
            "reason": reason,
            "r": round(float(r), 3),
            "rationale": d.rationale,
            "confidence": round(float(d.confidence), 3),
            "bias": int(d.bias_strength),
        })
        open_until = exit_idx

    return trades


def _summarize(trades: list[dict]) -> dict:
    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    return {
        "n": len(rs),
        "win_pct": (100 * len(wins) / len(rs)) if rs else 0.0,
        "sumR": sum(rs),
        "avgR": statistics.mean(rs) if rs else 0.0,
        "avgWin": statistics.mean(wins) if wins else 0.0,
        "avgLoss": statistics.mean(losses) if losses else 0.0,
        "pf": pf,
        "reasons": reasons,
    }


def main() -> None:
    cfgs = _load_configs()
    builders = {
        "democracy": lambda: Democracy(config=cfgs.get("democracy", {})),
        "republic": lambda: Republic(config=cfgs.get("republic", {})),
        "coup": lambda: Coup(config=cfgs.get("coup", {})),
    }

    # report bar availability
    print("=== bar availability ===")
    for sym in SYMBOLS:
        n = len(_orig(sym, TF, 10_000_000))
        print(f"  {sym} {TF}: {n} bars")
    print()

    all_trades: list[dict] = []
    errors: dict = {}
    summaries: dict = {}

    for name, build in builders.items():
        strat = build()
        cfg_syms = strat.config.get("symbols") or SYMBOLS
        syms = [s for s in SYMBOLS if s in cfg_syms]
        for sym in syms:
            CUT["ts"] = None
            trades = _replay_one(name, strat, sym, errors)
            all_trades.extend(trades)
            summaries[(name, sym)] = _summarize(trades)

    CUT["ts"] = None
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(all_trades, indent=2))

    # ── print summary ──
    print("=== per strategy × symbol ===")
    hdr = (f"{'strategy':10s} {'symbol':9s} {'n':>4s} {'win%':>6s} {'sumR':>8s} "
           f"{'avgR':>7s} {'avgW':>7s} {'avgL':>7s} {'PF':>6s}  reasons")
    print(hdr)
    print("-" * len(hdr))
    for name in builders:
        for sym in SYMBOLS:
            s = summaries.get((name, sym))
            if not s:
                continue
            if s["n"] == 0:
                print(f"{name:10s} {sym:9s} {0:>4d}  --  (no trades)")
                continue
            pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
            rstr = " ".join(f"{k}:{v}" for k, v in sorted(s["reasons"].items()))
            print(f"{name:10s} {sym:9s} {s['n']:>4d} {s['win_pct']:>5.0f}% "
                  f"{s['sumR']:>8.2f} {s['avgR']:>7.3f} {s['avgWin']:>7.3f} "
                  f"{s['avgLoss']:>7.3f} {pf:>6s}  {rstr}")

    print("\n=== totals per strategy ===")
    for name in builders:
        tr = [t for t in all_trades if t["strategy"] == name]
        rs = [t["r"] for t in tr]
        wins = sum(1 for r in rs if r > 0)
        sumr = sum(rs)
        wp = (100 * wins / len(rs)) if rs else 0.0
        print(f"  {name:10s} n={len(tr):>4d}  win={wp:>4.0f}%  sumR={sumr:>8.2f}  "
              f"avgR={(sumr/len(rs) if rs else 0):>6.3f}")

    if errors:
        print("\n=== errors (skipped) ===")
        for k, v in errors.items():
            if k.endswith(":last"):
                print(f"  {k}: {v}")
            else:
                print(f"  {k}: {v} skipped")

    print(f"\nwrote {len(all_trades)} trades → {OUT}")


if __name__ == "__main__":
    main()
