#!/usr/bin/env python3
"""Bit-for-bit parity: legacy Coup vs ComposedStrategy(coup config).

Replays footprint history through both strategies' decide() and asserts the
execution-relevant Decision fields (side, entry, stop_loss, take_profit,
confidence, bias_strength) match at every bar. Both read store().recent(), so we
monkeypatch the store to serve the historical window up to each replay index —
identical inputs to both → any mismatch is a real logic divergence.

    python scripts/parity_coup.py
"""
from __future__ import annotations

import sys
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
from strategies.coup import Coup
from strategies.composed.engine import ComposedStrategy

# live coup config (config/strategies.yaml)
COUP_CFG = {
    "symbols": ["BTCUSDT", "XAUTUSDT"], "trigger_mode": "climax_flip",
    "decide_tf": "15m", "vol_mult": 2.0, "delta_swing": 50.0, "vp_filter": True,
    "min_sl_atr_mult": 0.5, "flip_exit": False, "cvd_divergence_exit": True,
    "cvd_exit_conf": 0.65,
}
# the composed equivalent
COMPOSED_CFG = {
    "name": "coup", "engine": "composed", "decide_tf": "15m",
    "trigger": {"type": "climax_flip", "vol_mult": 2.0, "delta_swing": 50.0, "vp_filter": True},
    "entry": {"type": "market"},
    "sl": {"type": "atr_floor", "min_sl_atr_mult": 0.5},
    "tp": {"type": "rr", "rr": 2.0},
    "execution": {"type": "single_leg"},
    "exits": [{"type": "hard_sl"}, {"type": "cvd_divergence", "conf": 0.65}],
}

WARMUP = 130   # need ≥ VP_WIN(96)+30 bars before the trigger can fire


class _FakeStore:
    _bars: list = []
    def recent(self, symbol, tf, n):
        return self._bars[-n:]


def _fields(d):
    if d is None:
        return None
    return (d.side, round(d.entry, 6), round(d.stop_loss, 6), round(d.take_profit, 6),
            round(d.confidence, 6), d.bias_strength)


def main():
    fake = _FakeStore()
    _ss.store = lambda: fake          # patch lazy-import path (components.py)
    _coupmod.store = lambda: fake     # patch module-bound name (coup.py)

    total = mismatches = fires = 0
    for sym in ("BTCUSDT", "XAUTUSDT"):
        bars = _obs.load_bars(sym, "15m")
        if len(bars) < WARMUP + 5:
            print(f"{sym}: only {len(bars)} bars, skip"); continue
        coup = Coup(config=dict(COUP_CFG))
        comp = ComposedStrategy(config=dict(COMPOSED_CFG))
        sym_fire = sym_mis = 0
        for i in range(WARMUP, len(bars)):
            fake._bars = bars[:i + 1]
            bar = bars[i]
            d1 = coup.decide(sym, "15m", bar, {})
            d2 = comp.decide(sym, "15m", bar, {})
            total += 1
            f1, f2 = _fields(d1), _fields(d2)
            if f1 is not None:
                fires += 1; sym_fire += 1
            if f1 != f2:
                mismatches += 1; sym_mis += 1
                if sym_mis <= 5:
                    import time
                    print(f"  MISMATCH {sym} {time.strftime('%m-%d %H:%M', time.localtime(bar.close_ts))}")
                    print(f"    legacy:   {f1}")
                    print(f"    composed: {f2}")
        print(f"{sym}: bars={len(bars)-WARMUP} legacy-fires={sym_fire} mismatches={sym_mis}")
    print(f"\nTOTAL: {total} bars | legacy fired {fires} | mismatches {mismatches}")
    print("✅ BIT-FOR-BIT PARITY" if mismatches == 0 else "❌ DIVERGENCE — fix before porting")


if __name__ == "__main__":
    main()
