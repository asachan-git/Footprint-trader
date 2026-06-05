#!/usr/bin/env python3
"""Bit-for-bit parity: legacy Strategy subclass vs ComposedStrategy(config).

Replays footprint history through both and asserts the execution-relevant
Decision fields match at every bar. Add a case to CASES to cover a new port.

    python scripts/parity_composed.py
"""
from __future__ import annotations

import sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import logging; logging.disable(logging.CRITICAL)

import importlib.util
def _L(n, p):
    s = importlib.util.spec_from_file_location(n, ROOT / p); m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m
_obs = _L("absorption_observations", "scripts/absorption_observations.py")

import pipeline.state_store as _ss
import strategies.coup as _coupmod
import strategies.reversal_choch as _chochmod
from strategies.coup import Coup
from strategies.coup_reversal import CoupReversal
from strategies.reversal_choch import ReversalChoch
from strategies.composed.engine import ComposedStrategy

WARMUP = 215   # choch needs choch_lookback(200)+ bars before it can detect

# climax_flip composed template (shared by coup + coup_reversal — only params differ)
def _composed(name, vol_mult, delta_swing):
    return {
        "name": name, "engine": "composed", "decide_tf": "15m",
        "trigger": {"type": "climax_flip", "vol_mult": vol_mult, "delta_swing": delta_swing, "vp_filter": True},
        "entry": {"type": "market"},
        "sl": {"type": "atr_floor", "min_sl_atr_mult": 0.5},
        "tp": {"type": "rr", "rr": 2.0},
        "execution": {"type": "single_leg"},
        "exits": [{"type": "hard_sl"}, {"type": "cvd_divergence", "conf": 0.65}],
    }

def _composed_choch(name, fib_entry, fib_ext):
    return {
        "name": name, "engine": "composed", "decide_tf": "15m",
        "trigger": {"type": "choch_fib", "fib_entry": fib_entry, "fib_ext": fib_ext,
                    "entry_expiry_bars": 6},
        "execution": {"type": "single_leg"},
        "exits": [{"type": "hard_sl"}, {"type": "cvd_divergence", "conf": 0.65}],
    }


def _legacy_choch(name, fib_entry, fib_ext):
    return ReversalChoch(config={
        "symbols": ["BTCUSDT", "XAUTUSDT"], "decide_tf": "15m",
        "fib_entry": fib_entry, "fib_ext": fib_ext, "entry_expiry_bars": 6,
        "cvd_divergence_exit": True, "cvd_exit_conf": 0.65})


CASES = [
    {
        "label": "coup",
        "legacy": lambda: Coup(config={
            "symbols": ["BTCUSDT", "XAUTUSDT"], "trigger_mode": "climax_flip", "decide_tf": "15m",
            "vol_mult": 2.0, "delta_swing": 50.0, "vp_filter": True, "min_sl_atr_mult": 0.5,
            "flip_exit": False, "cvd_divergence_exit": True, "cvd_exit_conf": 0.65}),
        "composed": lambda: ComposedStrategy(config=_composed("coup", 2.0, 50.0)),
    },
    {
        "label": "coup_reversal",
        "legacy": lambda: CoupReversal(config={
            "symbols": ["BTCUSDT", "XAUTUSDT"], "trigger_mode": "climax_flip", "decide_tf": "15m",
            "vol_mult": 1.8, "delta_swing": 0.0, "vp_filter": True, "min_sl_atr_mult": 0.5,
            "flip_exit": False, "cvd_divergence_exit": True, "cvd_exit_conf": 0.65}),
        "composed": lambda: ComposedStrategy(config=_composed("coup_reversal", 1.8, 0.0)),
    },
    {"label": "reversal_choch", "legacy": lambda: _legacy_choch("reversal_choch", 0.705, 2.0),
     "composed": lambda: ComposedStrategy(config=_composed_choch("reversal_choch", 0.705, 2.0))},
    {"label": "reversal_choch_ext", "legacy": lambda: _legacy_choch("reversal_choch_ext", 0.705, 1.618),
     "composed": lambda: ComposedStrategy(config=_composed_choch("reversal_choch_ext", 0.705, 1.618))},
    {"label": "reversal_choch_entry", "legacy": lambda: _legacy_choch("reversal_choch_entry", 0.618, 2.0),
     "composed": lambda: ComposedStrategy(config=_composed_choch("reversal_choch_entry", 0.618, 2.0))},
]


class _FakeStore:
    _bars: list = []
    def recent(self, symbol, tf, n): return self._bars[-n:]


def _fields(d):
    if d is None: return None
    return (d.side, round(d.entry, 6), round(d.stop_loss, 6), round(d.take_profit, 6),
            round(d.confidence, 6), d.bias_strength)


def main():
    fake = _FakeStore()
    _ss.store = lambda: fake
    _coupmod.store = lambda: fake
    _chochmod.store = lambda: fake
    grand_mis = 0
    for case in CASES:
        print(f"\n=== {case['label']} ===")
        tot = fires = mis = 0
        for sym in ("BTCUSDT", "XAUTUSDT"):
            bars = _obs.load_bars(sym, "15m")
            if len(bars) < WARMUP + 5:
                print(f"  {sym}: {len(bars)} bars, skip"); continue
            leg = case["legacy"](); comp = case["composed"]()
            sf = sm = 0
            for i in range(WARMUP, len(bars)):
                fake._bars = bars[:i + 1]; bar = bars[i]
                f1, f2 = _fields(leg.decide(sym, "15m", bar, {})), _fields(comp.decide(sym, "15m", bar, {}))
                tot += 1
                if f1 is not None: fires += 1; sf += 1
                if f1 != f2:
                    mis += 1; sm += 1
                    if sm <= 4:
                        print(f"    MISMATCH {sym} {time.strftime('%m-%d %H:%M', time.localtime(bar.close_ts))}")
                        print(f"      legacy={f1}\n      composed={f2}")
            print(f"  {sym}: fires={sf} mismatches={sm}")
        print(f"  {case['label']}: {tot} bars, {fires} fires, {mis} mismatches "
              f"{'✅' if mis == 0 else '❌'}")
        grand_mis += mis
    print(f"\n{'✅ ALL PORTS BIT-FOR-BIT' if grand_mis == 0 else f'❌ {grand_mis} MISMATCHES'}")


if __name__ == "__main__":
    main()
