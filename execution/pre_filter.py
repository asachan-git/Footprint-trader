"""Pre-filter — fast local check before firing Claude or M2 direction engine.

Skips the expensive call when bars show no structural setup.
Used by both /decide (Claude) and /grid_tick (M2 rules).

Checks (any ONE passing = allow):
  1. Stacked imbalance present (bid or ask column ≥ 3 consecutive levels ≥ 2.5×)
  2. Active confirmed sweep on this symbol
  3. VA touch: price within va_touch_atr_mult × ATR of VAH, VAL, or POC
  4. Delta signal: |delta| ≥ min_abs_delta AND delta_atr_ratio ≥ floor

Logic: ALL four can fail → filter blocks. If ANY passes → allow.
Exception in any check → that check counts as passed (fail-open).

Settings keys (under decide_filter in settings.yaml):
  require_structural_setup: true   # master on/off
  va_touch_atr_mult: 0.3           # how close to VAH/VAL/POC counts as "touch"
  min_abs_delta: 2.0               # abs delta floor
  min_delta_atr_ratio: 0.0         # |delta|/ATR floor (0 = disabled)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class PreFilterResult:
    passed: bool
    reason: str          # what passed (or why blocked)
    checks: dict         # per-check results for logging


def check(bar, settings: dict) -> PreFilterResult:
    """Return PreFilterResult. passed=True means proceed with decision engine.

    bar: pipeline.types.Bar
    settings: full app settings dict
    """
    filt: dict = settings.get("decide_filter") or {}

    # Master switch — if not enabled, always pass
    if not filt.get("require_structural_setup", False):
        return PreFilterResult(passed=True, reason="pre_filter_disabled", checks={})

    checks: dict = {}
    any_passed = False

    # ── 1. Stacked imbalance ──────────────────────────────────────────────────
    try:
        from pipeline.footprint import build as build_fp
        from pipeline.features.stacked_imbalance import stacked_imbalances
        fp = build_fp(bar)
        stacked = stacked_imbalances(fp, min_stack=3, ratio=3.0)
        # ATR-normalised range floor: zone must span ≥ min_stack_atr_pct × ATR.
        # Prevents XAU tiny-tick stacks ($0.20 per level, $0.60 total) from
        # passing when ATR is $3–5. Default 0.15 = 15% of ATR min zone width.
        meaningful = False
        if stacked:
            try:
                from pipeline.features.atr import atr_from_store
                # Always use 15m ATR for zone-size floor — normalises across symbols.
                # 1m ATR (XAU ~$0.78) makes any $0.20+ stack look big; 15m ATR ($3-5)
                # correctly filters sub-ATR micro-stacks that have no structural weight.
                atr_val = atr_from_store(bar.symbol, "15m", period=14) or 1.0
                min_pct = float(filt.get("min_stack_atr_pct", 0.15))
                min_range = atr_val * min_pct
                meaningful = any(
                    (z.price_high - z.price_low) >= min_range for z in stacked
                )
            except Exception:
                meaningful = True  # fail-open if ATR unavailable
        checks["stacked_imbalance"] = meaningful
        if meaningful:
            any_passed = True
    except Exception as e:
        log.debug(f"[pre_filter] stacked check failed: {e}")
        checks["stacked_imbalance"] = True  # fail-open
        any_passed = True

    # ── 2. Active confirmed sweep (reversal or liquidity_grab only) ──────────
    # stop_run = just stops cleared, no edge. failed_sweep = breakout, not reversal.
    # Unclassified (age_bars=0, classification="") = sweep just fired, pending.
    _HIGH_EDGE = {"reversal", "liquidity_grab", ""}
    try:
        from pipeline.features.sweep import active_sweeps
        sweeps = [
            s for s in active_sweeps(bar.symbol)
            if s.delta_confirms and s.classification in _HIGH_EDGE
        ]
        checks["sweep"] = bool(sweeps)
        if sweeps:
            any_passed = True
    except Exception as e:
        log.debug(f"[pre_filter] sweep check failed: {e}")
        checks["sweep"] = True
        any_passed = True

    # ── 3. VA touch (price near VAH / VAL / POC) ─────────────────────────────
    try:
        from pipeline.features.vp_cache import get as vp_get
        from pipeline.features.atr import atr_from_store
        primary_tf = str((settings.get("instrument") or {}).get("primary_tf", "15m"))
        atr_val = atr_from_store(bar.symbol, primary_tf, period=14) or 1.0
        mult = float(filt.get("va_touch_atr_mult", 0.3))
        margin = atr_val * mult
        vp = vp_get(bar.symbol, "daily")
        touched = False
        if vp:
            price = bar.ohlc.c
            for key in ("vah", "val", "poc"):
                level = vp.get(key)
                if level and abs(price - level) <= margin:
                    touched = True
                    break
        checks["va_touch"] = touched
        if touched:
            any_passed = True
    except Exception as e:
        log.debug(f"[pre_filter] va_touch check failed: {e}")
        checks["va_touch"] = True
        any_passed = True

    # ── 4. Delta signal — high-conviction only (P75 threshold, not median) ──────
    # Delta at median = noise. Only pass when delta is in top quartile of
    # distribution OR combined with structural signal (stacked/sweep/va_touch).
    # Uses calibrated P75 threshold, not mean-calibrated min_abs_delta.
    try:
        from pipeline.footprint import build as build_fp
        from pipeline.features import bar_delta
        from pipeline.features.atr import atr_from_store
        primary_tf = str((settings.get("instrument") or {}).get("primary_tf", "15m"))
        fp = build_fp(bar)
        delta = abs(bar_delta(fp))
        atr_val = atr_from_store(bar.symbol, primary_tf, period=14) or 1.0
        # P75 threshold: 2× calibrated mean floor (mean is ~P50, P75 ≈ 2× mean empirically)
        config_min = float(filt.get("min_abs_delta", 2.0))
        try:
            from pipeline.features.threshold_calibrator import get as _get_cal
            cal = _get_cal(bar.symbol, primary_tf, bar.tf)
            base = cal.min_abs_delta if cal.sample_size >= 8 else config_min
        except Exception:
            base = config_min
        p75_threshold = base * 2.0
        min_ratio = float(filt.get("min_delta_atr_ratio", 0.0))
        delta_ok = delta >= p75_threshold
        ratio_ok = (min_ratio <= 0) or (delta / atr_val >= min_ratio)
        checks["delta"] = delta_ok and ratio_ok
        if delta_ok and ratio_ok:
            any_passed = True
    except Exception as e:
        log.debug(f"[pre_filter] delta check failed: {e}")
        checks["delta"] = True
        any_passed = True

    if any_passed:
        passed_keys = [k for k, v in checks.items() if v]
        return PreFilterResult(passed=True, reason=f"setup:{'+'.join(passed_keys)}", checks=checks)

    return PreFilterResult(
        passed=False,
        reason="no_setup:no_stacked+no_sweep+no_va_touch+no_delta",
        checks=checks,
    )
