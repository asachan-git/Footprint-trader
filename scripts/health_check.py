#!/usr/bin/env python3
"""Fleet smoke test — does every enabled strategy + feature module actually run?

Builds the enabled fleet from config, then for each strategy × symbol runs
.decide() on the latest persisted bar inside the strategy's scope and reports
OK / flat / ERROR. Also pings each core feature module once. Read-only.

    PYTHONPATH=. .venv/bin/python -u scripts/health_check.py
"""
from __future__ import annotations

import logging
import traceback

logging.disable(logging.CRITICAL)

from strategies.registry import build_enabled
from strategies.base import StrategyContext
from pipeline.state_store import store


def latest_bar(symbol: str, tf: str):
    bars = store().recent(symbol, tf, 1)
    return bars[-1] if bars else None


def main() -> None:
    print("=" * 80)
    print("FLEET HEALTH CHECK")
    print("=" * 80)

    # ── feature modules ──────────────────────────────────────────────────────
    print("\n── feature modules ─────────────────────────────────────────")
    checks = []
    sym = "BTCUSDT"
    try:
        from pipeline.features.atr import atr_from_store
        checks.append(("atr_from_store", f"{atr_from_store(sym, '15m'):.1f}"))
    except Exception as e:
        checks.append(("atr_from_store", f"ERROR {e}"))
    try:
        from pipeline.features.session import current_session
        checks.append(("session", current_session(None, sym).session))
    except Exception as e:
        checks.append(("session", f"ERROR {e}"))
    try:
        from pipeline.features.cvd_div_state import last
        d = last(sym)
        checks.append(("cvd_div_state.last", d["type"] if d else "none"))
    except Exception as e:
        checks.append(("cvd_div_state.last", f"ERROR {e}"))
    try:
        from pipeline.features.cvd_candlestick import scan_divergences
        bars = store().recent(sym, "15m", 60)
        checks.append(("scan_divergences", f"{len(scan_divergences(bars))} divs"))
    except Exception as e:
        checks.append(("scan_divergences", f"ERROR {e}"))
    try:
        from pipeline.features.vp_cache import get as vp_get
        vp = vp_get(sym, "daily")
        checks.append(("vp_cache", f"poc={vp.get('poc')}" if vp else "none"))
    except Exception as e:
        checks.append(("vp_cache", f"ERROR {e}"))
    try:
        from execution.direction_engine import _trend_regime
        r, s = _trend_regime(sym, "15m")
        checks.append(("_trend_regime", f"{r} ({s})"))
    except Exception as e:
        checks.append(("_trend_regime", f"ERROR {e}"))
    for name, val in checks:
        flag = "✗" if str(val).startswith("ERROR") else "✓"
        print(f"  {flag} {name:<22} {val}")

    # ── strategies ───────────────────────────────────────────────────────────
    print("\n── enabled strategies: decide() smoke test ─────────────────")
    strategies = build_enabled()
    print(f"  loaded {len(strategies)} enabled strategies\n")
    settings = {"decide_on_bar_close": {"tf": "15m"}}
    n_ok = n_err = 0
    for strat in strategies:
        ctx = StrategyContext.create(strat.name)
        for symbol in strat.symbols(settings):
            tf = str(strat.config.get("decide_tf")
                     or strat.config.get("vote_tf") or "15m")
            bar = latest_bar(symbol, tf)
            if bar is None:
                print(f"  ⚠ {strat.name:<22} {symbol:<9} no {tf} bar in store")
                continue
            try:
                with ctx.scope():
                    dec = strat.decide(symbol, tf, bar, strat.settings_override(settings))
                side = dec.side if dec else "flat"
                print(f"  ✓ {strat.name:<22} {symbol:<9} {tf:<3} → {side}")
                n_ok += 1
            except Exception as e:
                n_err += 1
                tb = traceback.format_exc().splitlines()[-2:]
                print(f"  ✗ {strat.name:<22} {symbol:<9} {tf:<3} ERROR: {e}")
                for t in tb:
                    print(f"        {t}")

    print(f"\n  decide() calls: {n_ok} ok, {n_err} errors")
    print("=" * 80)
    print("RESULT:", "ALL GREEN" if n_err == 0 else f"{n_err} STRATEGY ERRORS — see above")


if __name__ == "__main__":
    main()
