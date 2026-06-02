"""Footprint replay tester — feed historical footprint JSONL through any feature.

Usage:
    # List available built-in features
    python scripts/replay.py --list

    # Run a single feature on XAUTUSDT 1m
    python scripts/replay.py --feature stacked_imbalance --symbol XAUTUSDT --tf 1m

    # Run with custom parameters
    python scripts/replay.py --feature stacked_imbalance --symbol XAUTUSDT --tf 1m \
        --params '{"min_stack": 3, "ratio": 3.0}'

    # Run the pre_filter (combined structural check)
    python scripts/replay.py --feature pre_filter --symbol XAUTUSDT --tf 1m

    # Run multiple features, output CSV
    python scripts/replay.py --feature stacked_imbalance --symbol BTCUSDT --tf 15m \
        --out data/replay_stacked_btc_15m.csv

    # Date range filter
    python scripts/replay.py --feature pre_filter --symbol XAUTUSDT --tf 1m \
        --from 2026-05-01 --to 2026-05-30

    # Show signal stats summary only (no per-bar output)
    python scripts/replay.py --feature stacked_imbalance --symbol XAUTUSDT --tf 1m --summary

Output columns (per bar, CSV or TSV to stdout):
    ts, datetime_utc, symbol, tf, open, high, low, close, delta,
    signal, <feature-specific columns>

Stats printed to stderr after run:
    bars_total, bars_signal, signal_rate_pct, avg_signal_delta, avg_bar_delta

Anatomy of a feature plugin:
    Each built-in is a function `run(bar: Bar, history: list[Bar], params: dict) -> dict`.
    Return dict must include:
        signal: bool      — did the feature fire?
    Any other keys become output columns.

    To test a custom feature without editing this file:
        --feature path.to.module:function_name
    Function signature: same as above.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.types import Bar, OHLC, Level  # noqa: E402
from pipeline.footprint import build as build_fp  # noqa: E402


# ── Bar deserializer ──────────────────────────────────────────────────────────

def _bar_from_dict(d: dict) -> Bar:
    return Bar(
        bar_id=d["bar_id"],
        symbol=d["symbol"],
        tf=d["tf"],
        close_ts=d["close_ts"],
        source="replay",
        ohlc=OHLC(**d["ohlc"]),
        bid_ladder=tuple(Level(**x) for x in d.get("bid_ladder") or []),
        ask_ladder=tuple(Level(**x) for x in d.get("ask_ladder") or []),
        poc=d.get("poc"),
        delta=d.get("delta"),
    )


def _load_jsonl(path: Path, symbol: str, tf: str,
                ts_from: int = 0, ts_to: int = 2**62) -> list[Bar]:
    bars: list[Bar] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("symbol") != symbol or d.get("tf") != tf:
                continue
            ts = d.get("close_ts", 0)
            if ts < ts_from or ts > ts_to:
                continue
            try:
                bars.append(_bar_from_dict(d))
            except Exception as e:
                sys.stderr.write(f"[replay] skip bar {d.get('bar_id')}: {e}\n")
    bars.sort(key=lambda b: b.close_ts)
    return bars


# ── Built-in feature plugins ──────────────────────────────────────────────────

def _feature_stacked_imbalance(bar: Bar, history: list[Bar], params: dict) -> dict:
    from pipeline.features.stacked_imbalance import stacked_imbalances
    fp = build_fp(bar)
    stacked = stacked_imbalances(
        fp,
        min_stack=int(params.get("min_stack", 3)),
        ratio=float(params.get("ratio", 3.0)),
    )
    atr_floor = None
    meaningful = bool(stacked)
    if stacked and (params.get("atr_pct") or params.get("proximity_atr")):
        from pipeline.features.atr import atr as compute_atr
        atr_val = compute_atr(history[-15:] if len(history) >= 15 else history) or 1.0
        current_price = bar.ohlc.c
        proximity_mult = float(params.get("proximity_atr", 0))
        min_span_pct = float(params.get("atr_pct", 0))
        min_range = atr_val * min_span_pct if min_span_pct else 0
        atr_floor = round(min_range, 4) if min_range else None
        meaningful = any(
            (min_range == 0 or (z.price_high - z.price_low) >= min_range)
            and (proximity_mult == 0 or abs((z.price_low + z.price_high) / 2 - current_price) <= atr_val * proximity_mult)
            for z in stacked
        )
    return {
        "signal": meaningful,
        "n_zones": len(stacked),
        "atr_floor": atr_floor,
        "zones": "|".join(
            f"{z.side}@{z.price_low:.2f}-{z.price_high:.2f}(n={z.count})"
            for z in stacked
        ) if stacked else "",
    }


def _feature_sweep(bar: Bar, history: list[Bar], params: dict) -> dict:
    try:
        from pipeline.features.sweep import active_sweeps
        _HIGH_EDGE = {"reversal", "liquidity_grab", ""}
        sweeps = active_sweeps(bar.symbol)
        confirmed = [s for s in sweeps if s.delta_confirms]
        high_edge = [s for s in confirmed if s.classification in _HIGH_EDGE]
        classifications = "|".join(
            f"{s.classification or 'pending'}({s.sweep_type})" for s in confirmed
        )
        return {
            "signal": bool(high_edge),
            "n_sweeps": len(sweeps),
            "n_confirmed": len(confirmed),
            "n_high_edge": len(high_edge),
            "classifications": classifications,
        }
    except Exception as e:
        return {"signal": False, "error": str(e)}


def _feature_delta_divergence(bar: Bar, history: list[Bar], params: dict) -> dict:
    """Delta divergence: price makes new high/low vs prior window but delta fails to confirm.

    Compares current bar to the prior N-bar window (excludes current bar):
      bearish: current close > window max close, but current delta < window max delta
      bullish: current close < window min close, but current delta > window min delta
    More robust than bar-to-bar comparison — requires actual price extremes to be broken.
    """
    _empty = {"signal": False, "direction": "none", "price_vs_window": 0.0, "delta_vs_window": 0.0}
    window_size = int(params.get("window", 5))
    if len(history) < window_size:
        return _empty
    prior = history[-window_size:]
    prior_closes = [b.ohlc.c for b in prior]
    prior_deltas = [b.delta or 0.0 for b in prior]
    cur_close = bar.ohlc.c
    cur_delta = bar.delta or 0.0
    max_close = max(prior_closes)
    min_close = min(prior_closes)
    max_delta = max(prior_deltas)
    min_delta = min(prior_deltas)
    # Bearish: price breaks above prior window high, but delta lower than prior window max
    bearish_div = (cur_close > max_close) and (cur_delta < max_delta)
    # Bullish: price breaks below prior window low, but delta higher than prior window min
    bullish_div = (cur_close < min_close) and (cur_delta > min_delta)
    return {
        "signal": bullish_div or bearish_div,
        "direction": "bullish" if bullish_div else ("bearish" if bearish_div else "none"),
        "price_vs_window": round(cur_close - max_close if bearish_div else min_close - cur_close, 4),
        "delta_vs_window": round(max_delta - cur_delta if bearish_div else cur_delta - min_delta, 4),
    }


def _feature_pre_filter(bar: Bar, history: list[Bar], params: dict) -> dict:
    """Run the full execution.pre_filter check using a fake settings dict."""
    from execution.pre_filter import check as pf_check
    settings = {
        "decide_filter": {
            "require_structural_setup": True,
            "va_touch_atr_mult": float(params.get("va_touch_atr_mult", 0.3)),
            "min_abs_delta": float(params.get("min_abs_delta", 2.0)),
            "min_delta_atr_ratio": float(params.get("min_delta_atr_ratio", 0.0)),
            "min_stack_atr_pct": float(params.get("min_stack_atr_pct", 0.15)),
        },
        "instrument": {"primary_tf": bar.tf},
    }
    result = pf_check(bar, settings)
    return {
        "signal": result.passed,
        "reason": result.reason,
        **{f"check_{k}": v for k, v in result.checks.items()},
    }


def _feature_va_touch(bar: Bar, history: list[Bar], params: dict) -> dict:
    try:
        from pipeline.features.vp_cache import get as vp_get
        from pipeline.features.atr import atr as compute_atr
        vp = vp_get(bar.symbol, "daily")
        if not vp:
            return {"signal": False, "reason": "no_vp"}
        atr_val = compute_atr(history[-15:] if len(history) >= 15 else history) or 1.0
        mult = float(params.get("mult", 0.3))
        margin = atr_val * mult
        price = bar.ohlc.c
        touched = None
        for key in ("poc", "vah", "val"):
            level = vp.get(key)
            if level and abs(price - level) <= margin:
                touched = key
                break
        return {
            "signal": touched is not None,
            "touched": touched or "",
            "margin": round(margin, 4),
        }
    except Exception as e:
        return {"signal": False, "error": str(e)}


def _feature_fvg(bar: Bar, history: list[Bar], params: dict) -> dict:
    try:
        from pipeline.features.fvg import detect_fvgs
        fvgs = detect_fvgs(
            history + [bar],
            max_age_bars=int(params.get("max_age_bars", 200)),
        )
        active = [f for f in fvgs if not f.filled]
        return {
            "signal": bool(active),
            "n_fvg": len(active),
            "sides": "|".join(f.side for f in active),
        }
    except Exception as e:
        return {"signal": False, "error": str(e)}


def _feature_unfinished_auction(bar: Bar, history: list[Bar], params: dict) -> dict:
    from pipeline.features.unfinished_auction import detect as ua_detect
    from pipeline.footprint import build as build_fp
    fp = build_fp(bar)
    ua = ua_detect(
        fp,
        n_cells=int(params.get("n_cells", 2)),
        min_vol=float(params.get("min_vol", 0.5)),
        min_ratio=float(params.get("min_ratio", 4.0)),
    )
    return {
        "signal": ua.fired,
        "side": ua.side,
        "extreme_price": ua.extreme_price,
        "ratio": ua.ratio,
    }


def _feature_combined_gates(bar: Bar, history: list[Bar], params: dict) -> dict:
    """Run all three direction gates and report combined veto for long/short.

    Output columns:
      stacked_side    : "sell"/"buy"/"none"  — conflicting stacked zone side
      delta_dir       : "bearish"/"bullish"/"none"
      ua_side         : "up"/"down"/"none"
      veto_long       : 1 if any gate would block a long entry
      veto_short      : 1 if any gate would block a short entry
      n_gates_long    : number of gates vetoing long
      n_gates_short   : number of gates vetoing short
    """
    from pipeline.footprint import build as build_fp
    fp = build_fp(bar)
    atr_val = 1.0

    # ── Stacked imbalance ──────────────────────────────────────────────────
    stacked_side = "none"
    try:
        from pipeline.features.stacked_imbalance import stacked_imbalances
        from pipeline.features.atr import atr as compute_atr
        stacks = stacked_imbalances(fp, min_stack=int(params.get("si_min_stack", 10)),
                                    ratio=float(params.get("si_ratio", 2.5)))
        if stacks and len(history) >= 5:
            atr_val = compute_atr(history[-15:]) or 1.0
            price = bar.ohlc.c
            prox_mult = float(params.get("si_proximity_atr", 1.0))
            span_pct  = float(params.get("si_span_atr", 0.15))
            for z in stacks:
                mid = (z.price_low + z.price_high) / 2
                if (abs(mid - price) <= atr_val * prox_mult
                        and (z.price_high - z.price_low) >= atr_val * span_pct):
                    stacked_side = z.side   # "sell" or "buy"
                    break
    except Exception:
        pass

    # ── Delta divergence ──────────────────────────────────────────────────
    delta_dir = "none"
    try:
        from pipeline.features.delta_divergence import detect as dd_detect
        window = int(params.get("dd_window", 15))
        if len(history) >= window:
            dd = dd_detect(bar, history, window=window)
            if dd.fired:
                delta_dir = dd.direction
    except Exception:
        pass

    # ── Unfinished auction ────────────────────────────────────────────────
    ua_side = "none"
    try:
        from pipeline.features.unfinished_auction import detect as ua_detect
        ua = ua_detect(fp,
                       n_cells=int(params.get("ua_n_cells", 2)),
                       min_vol=float(params.get("ua_min_vol", 0.5)),
                       min_ratio=float(params.get("ua_min_ratio", 8.0)))
        if ua.fired:
            ua_side = ua.side
    except Exception:
        pass

    # ── Combine: count gates that veto each direction ─────────────────────
    gates_long  = (
        int(stacked_side == "sell") +   # sell stack blocks long
        int(delta_dir == "bearish") +   # bearish div blocks long
        int(ua_side == "down")          # unfinished_down blocks long
    )
    gates_short = (
        int(stacked_side == "buy") +    # buy stack blocks short
        int(delta_dir == "bullish") +   # bullish div blocks short
        int(ua_side == "up")            # unfinished_up blocks short
    )
    veto_long  = int(gates_long > 0)
    veto_short = int(gates_short > 0)

    return {
        "signal":       int(veto_long or veto_short),
        "stacked_side": stacked_side,
        "delta_dir":    delta_dir,
        "ua_side":      ua_side,
        "veto_long":    veto_long,
        "veto_short":   veto_short,
        "n_gates_long": gates_long,
        "n_gates_short": gates_short,
    }


BUILTIN_FEATURES: dict[str, Callable] = {
    "stacked_imbalance": _feature_stacked_imbalance,
    "sweep": _feature_sweep,
    "delta_divergence": _feature_delta_divergence,
    "unfinished_auction": _feature_unfinished_auction,
    "combined_gates": _feature_combined_gates,
    "pre_filter": _feature_pre_filter,
    "va_touch": _feature_va_touch,
    "fvg": _feature_fvg,
}


def _load_feature(name: str) -> Callable:
    """Resolve built-in name or 'module.path:function' external reference."""
    if name in BUILTIN_FEATURES:
        return BUILTIN_FEATURES[name]
    if ":" in name:
        mod_path, fn_name = name.rsplit(":", 1)
        mod = importlib.import_module(mod_path)
        return getattr(mod, fn_name)
    raise ValueError(f"Unknown feature '{name}'. Use --list to see available features.")


# ── Main runner ───────────────────────────────────────────────────────────────

def _ts_from_date(date_str: str) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay historical footprint JSONL through a feature function."
    )
    parser.add_argument("--feature", "-f", help="Feature name or module:function")
    parser.add_argument("--symbol", "-s", default="XAUTUSDT")
    parser.add_argument("--tf", default="1m")
    parser.add_argument("--params", default="{}", help="JSON dict of feature params")
    parser.add_argument("--data-dir", default=str(ROOT / "data" / "footprint"),
                        help="Directory containing footprint JSONL files")
    parser.add_argument("--out", help="Output CSV path (default: stdout)")
    parser.add_argument("--from", dest="ts_from", default=None, help="Start date YYYY-MM-DD")
    parser.add_argument("--to", dest="ts_to", default=None, help="End date YYYY-MM-DD (inclusive)")
    parser.add_argument("--history", type=int, default=200,
                        help="Rolling history window passed to feature (default 200 bars)")
    parser.add_argument("--summary", action="store_true", help="Print stats only, no per-bar rows")
    parser.add_argument("--list", action="store_true", help="List built-in features and exit")
    args = parser.parse_args()

    if args.list:
        print("Built-in features:")
        for name in BUILTIN_FEATURES:
            print(f"  {name}")
        print("\nCustom: --feature path.to.module:function_name")
        return

    if not args.feature:
        parser.error("--feature required (use --list to see options)")

    params = json.loads(args.params)
    feature_fn = _load_feature(args.feature)

    ts_from = _ts_from_date(args.ts_from) if args.ts_from else 0
    ts_to = _ts_from_date(args.ts_to) + 86400 if args.ts_to else 2**62

    data_dir = Path(args.data_dir)
    jsonl_path = data_dir / f"{args.symbol}_{args.tf}.jsonl"
    if not jsonl_path.exists():
        sys.exit(f"[replay] file not found: {jsonl_path}")

    sys.stderr.write(f"[replay] loading {jsonl_path}...\n")
    bars = _load_jsonl(jsonl_path, args.symbol, args.tf, ts_from, ts_to)
    sys.stderr.write(f"[replay] {len(bars)} bars loaded\n")

    if not bars:
        sys.exit("[replay] no bars after filter")

    # Warm up feature on first bar to discover extra columns
    history: list[Bar] = []
    probe_result = feature_fn(bars[0], history, params)
    extra_cols = [k for k in probe_result if k != "signal"]

    base_cols = ["ts", "datetime_utc", "symbol", "tf", "open", "high", "low", "close", "delta", "signal"]
    all_cols = base_cols + extra_cols

    # Output setup
    if args.out:
        out_file = open(args.out, "w", newline="")
    else:
        out_file = sys.stdout if not args.summary else None

    writer = None
    if out_file:
        writer = csv.DictWriter(out_file, fieldnames=all_cols, extrasaction="ignore")
        writer.writeheader()

    n_signal = 0
    n_total = 0
    signal_deltas: list[float] = []
    all_deltas: list[float] = []

    for i, bar in enumerate(bars):
        hist_window = bars[max(0, i - args.history):i]
        try:
            result = feature_fn(bar, hist_window, params)
        except Exception as e:
            sys.stderr.write(f"[replay] feature error bar {bar.bar_id}: {e}\n")
            result = {"signal": False, "error": str(e)}

        sig = bool(result.get("signal"))
        n_total += 1
        delta_val = bar.delta or 0.0
        all_deltas.append(delta_val)
        if sig:
            n_signal += 1
            signal_deltas.append(delta_val)

        if writer:
            row = {
                "ts": bar.close_ts,
                "datetime_utc": datetime.fromtimestamp(bar.close_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "symbol": bar.symbol,
                "tf": bar.tf,
                "open": bar.ohlc.o,
                "high": bar.ohlc.h,
                "low": bar.ohlc.l,
                "close": bar.ohlc.c,
                "delta": round(delta_val, 4),
                "signal": int(sig),
            }
            for col in extra_cols:
                row[col] = result.get(col, "")
            writer.writerow(row)

    if out_file and out_file is not sys.stdout:
        out_file.close()

    # Stats to stderr (or stdout if --summary)
    signal_rate = 100 * n_signal / n_total if n_total else 0
    avg_signal_delta = sum(signal_deltas) / len(signal_deltas) if signal_deltas else 0
    avg_all_delta = sum(all_deltas) / len(all_deltas) if all_deltas else 0
    block_rate = 100 - signal_rate

    stats = (
        f"\n[replay] === {args.feature} | {args.symbol} {args.tf} ===\n"
        f"  bars_total    : {n_total}\n"
        f"  bars_signal   : {n_signal}\n"
        f"  signal_rate   : {signal_rate:.1f}%\n"
        f"  block_rate    : {block_rate:.1f}%\n"
        f"  avg_delta (signal bars) : {avg_signal_delta:.3f}\n"
        f"  avg_delta (all bars)    : {avg_all_delta:.3f}\n"
    )
    if args.out:
        sys.stderr.write(stats)
        sys.stderr.write(f"  output        : {args.out}\n")
    else:
        if args.summary:
            sys.stdout.write(stats)
        else:
            sys.stderr.write(stats)


if __name__ == "__main__":
    main()
