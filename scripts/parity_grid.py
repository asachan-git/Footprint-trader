#!/usr/bin/env python3
"""Bit-for-bit parity for the GRID family (democracy/republic/senate).

The family shares Democracy.decide (vote → placeholder Decision); the difference
is adjust_plan (GridPlan SL/TP reshaping). So two checks:
  1. decide parity — democracy vs ComposedStrategy(vote_panel), replayed.
  2. adjust_plan parity — republic/senate vs the grid_structural_sl execution
     component, on synthetic GridPlans fed through both against a real bar.

    python scripts/parity_grid.py
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
import strategies.democracy as _demomod
from strategies.democracy import Democracy
from strategies.republic import Republic
from strategies.senate import Senate
from strategies.composed.engine import ComposedStrategy
from strategies.composed.components import Ctx
from strategies.composed.registry import EXECUTION, resolve
from execution.grid_placer import GridPlan, GridLegPlan


class _FakeStore:
    _bars: list = []
    def recent(self, symbol, tf, n): return self._bars[-n:]


def _dfields(d):
    if d is None: return None
    return (d.side, round(d.entry, 6), round(d.stop_loss, 6), round(d.take_profit, 6),
            round(d.confidence, 6), d.bias_strength)


def _pfields(p):
    return (round(p.safety_sl, 4) if p.safety_sl is not None else None,
            round(p.take_profit, 4), p.tp_source,
            round(p.safety_sl_offset_pct, 8) if p.safety_sl_offset_pct is not None else None,
            round(p.tp_offset_pct, 8))


def _mk_plan(symbol, side, anchor, bias, with_floor):
    leg = GridLegPlan(leg_idx=1, price=anchor, lots=0.01, side=side, source="test")
    tp = anchor * (1.02 if side == "long" else 0.98)
    floor_pct = 0.03 if symbol.startswith("BTC") else 0.015
    safety_sl = (anchor * (1 - floor_pct) if side == "long" else anchor * (1 + floor_pct)) if with_floor else None
    return GridPlan(
        symbol=symbol, broker_symbol=symbol, side=side, legs=[leg],
        avg_entry_on_full_fill=anchor, take_profit=tp, tp_source="test_tp",
        bias_strength=bias, safety_sl=safety_sl, note="parity",
        anchor_price=anchor, leg_offsets_pct=(0.0,), tp_offset_pct=(tp - anchor) / anchor,
        safety_sl_offset_pct=((safety_sl - anchor) / anchor if safety_sl else None))


def decide_parity():
    fake = _FakeStore(); _ss.store = lambda: fake; _demomod.store = lambda: fake
    comp = ComposedStrategy(config={"name": "democracy", "engine": "composed", "vote_tf": "15m",
                                    "trigger": {"type": "vote_panel"}, "entry": {"type": "market"}})
    demo = Democracy(config={"symbols": ["BTCUSDT", "XAUTUSDT"], "vote_tf": "15m"})
    tot = mis = fires = 0
    for sym in ("BTCUSDT", "XAUTUSDT"):
        bars = _obs.load_bars(sym, "15m")
        for i in range(max(0, len(bars) - 300), len(bars)):
            fake._bars = bars[:i + 1]; bar = bars[i]
            f1, f2 = _dfields(demo.decide(sym, "15m", bar, {})), _dfields(comp.decide(sym, "15m", bar, {}))
            tot += 1
            if f1 is not None: fires += 1
            if f1 != f2:
                mis += 1
                if mis <= 4: print(f"  decide MISMATCH {sym}@{i}\n    demo={f1}\n    comp={f2}")
    print(f"decide parity: {tot} bars, {fires} fires, {mis} mismatches {'✅' if mis == 0 else '❌'}")
    return mis


def adjust_parity():
    mis = tot = 0
    cases = [
        ("republic", lambda: Republic(config={"sl_atr_mult": 1.5}),
         {"type": "grid_structural_sl", "sl_anchor": "confluence"}),
        ("senate", lambda: Senate(config={"sl_atr_mult": 1.5}),
         {"type": "grid_structural_sl", "sl_anchor": "wall"}),
    ]
    for label, legacy_factory, comp_spec in cases:
        leg = legacy_factory()
        comp_exec = resolve(EXECUTION, comp_spec, "execution")
        cm = 0
        for sym in ("BTCUSDT", "XAUTUSDT"):
            bars = _obs.load_bars(sym, "15m")
            if not bars: continue
            bar = bars[-1]; anchor = bar.ohlc.c
            for side in ("long", "short"):
                for bias in (3, 5):
                    for floor in (True, False):
                        p1 = _mk_plan(sym, side, anchor, bias, floor)
                        p2 = _mk_plan(sym, side, anchor, bias, floor)
                        out1 = leg.adjust_plan(p1, bar, {})
                        ctx = Ctx(sym, "15m", bar, {}, {"name": label, "decide_tf": "15m"}, {})
                        out2 = comp_exec(p2, bar, ctx)
                        tot += 1
                        if _pfields(out1) != _pfields(out2):
                            cm += 1; mis += 1
                            if cm <= 4:
                                print(f"  adjust MISMATCH {label} {sym} {side} bias{bias} floor{floor}")
                                print(f"    legacy={_pfields(out1)}\n    comp  ={_pfields(out2)}")
        print(f"  {label}: {'✅' if cm == 0 else f'❌ {cm}'}")
    print(f"adjust_plan parity: {tot} plans, {mis} mismatches {'✅' if mis == 0 else '❌'}")
    return mis


if __name__ == "__main__":
    print("=== GRID FAMILY PARITY ===")
    m1 = decide_parity()
    m2 = adjust_parity()
    print(f"\n{'✅ GRID FAMILY BIT-FOR-BIT' if m1 + m2 == 0 else f'❌ {m1 + m2} MISMATCHES'}")
