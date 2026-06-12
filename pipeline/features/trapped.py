"""Trapped-cohort read — fuses liquidity grabs + HTF structure + absorption into a
single "who got caught offside recently" signal.

A trapped cohort is a group of late participants now underwater because price swept
the level their orders rested at and reversed:

  sweep_high at a resting-buy level  → LATE LONGS trapped above  → bias SHORT
  sweep_low  at a resting-sell level → LATE SHORTS trapped below → bias LONG

We only count a cohort as trapped when the grab actually reversed on them
(classification reversal/liquidity_grab, delta-confirmed, not stale) — a
sweep_acceptance / failed_sweep means the breakout was real and nobody is trapped.

Structure TF policy ("both, weighted"): sweeps are detected on the primary (15m)
feed, but each cohort's `structural_weight` scales by how significant the swept
level is on HIGHER structure — a prior-day / session level outranks an intraday
swing. ChoCh agreement and same-bar absorption further confirm the trap.

NOT WIRED (2026-06-05). Built as the foundation for a direction vote / entry
strategy / overlay, but a causal forward-test (scripts replay, fixed sweep-reclaim
detection) showed NO edge: fading a reclaimed-trap cohort is coinflip-to-negative
at K=4/8/12 on both BTC and XAUT (BTC worsens with horizon — sweeps there behave as
CONTINUATION, not reversal). Only liquidity_grab-on-XAUT flickered (n=10, untrusted).
Kept inert for future study; consumers were intentionally not built. Reads the live
state_store; nothing here mutates state.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Significance of the swept level on HTF structure → cohort weight [0..1].
_LEVEL_WEIGHT = {
    "prior_day_high": 1.0, "prior_day_low": 1.0,
    "session_high": 0.9, "session_low": 0.9,
    "vah": 0.75, "val": 0.75,
    "equal_high": 0.8, "equal_low": 0.8,     # clustered stops = dense liquidity
    "cvd_swing_high": 0.5, "cvd_swing_low": 0.5,
}
_DEFAULT_LEVEL_WEIGHT = 0.4

# A trap is only real on these sweep classifications (breakout/acceptance = no trap).
_TRAP_CLASS = {"reversal", "liquidity_grab"}


@dataclass(frozen=True)
class TrappedCohort:
    side: Literal["late_longs", "late_shorts"]   # who is offside
    bias: Literal["long", "short"]               # tradeable lean (fade the trapped side)
    price: float                                 # the swept level the cohort is trapped at
    level_label: str                             # which HTF/structure level was swept
    age_bars: int                                # bars since the grab (freshness)
    still_underwater: bool                       # price reclaimed back through → cohort offside now
    structural_weight: float                     # [0..1] HTF significance of the level
    choch_confirms: bool                         # a structure flip agrees with the trap
    absorption_confirms: bool                    # same-bar footprint absorption agrees
    score: float                                 # [0..1] overall conviction
    reason: str


def _structural_weight(level_label: str) -> float:
    return _LEVEL_WEIGHT.get(level_label, _DEFAULT_LEVEL_WEIGHT)


def trapped_cohorts(symbol: str, primary_tf: str = "15m",
                    max_age_bars: int = 12) -> list[TrappedCohort]:
    """Return active trapped cohorts for `symbol`, highest-score first.

    Built from active sweep events (the liquidity grabs), weighted by HTF level
    significance, and confirmed by ChoCh + same-bar absorption. Stale / accepted
    sweeps and grabs older than `max_age_bars` are dropped."""
    try:
        from pipeline.features.sweep import active_sweeps
    except Exception:
        return []

    sweeps = active_sweeps(symbol)
    if not sweeps:
        return []

    # ChoCh context (optional confirm) — computed once.
    choch_dir = None
    try:
        from pipeline.state_store import store
        from pipeline.features.choch import detect_choch
        bars = store().recent(symbol, primary_tf, 200)
        ev = detect_choch(bars, n=2) if len(bars) >= 20 else None
        choch_dir = ev.direction if ev else None     # "bull" | "bear" | None
    except Exception:
        bars = []

    # Same-bar absorption (optional confirm) — sell-absorption = buyers trapped at
    # high (agrees with a sweep_high), buy-absorption = sellers trapped at low.
    absorb_sides: set[str] = set()
    try:
        from pipeline.footprint import build as _build_fp
        from pipeline.features.absorption import detect_canonical_absorption
        if bars:
            absorb_sides = {a.side for a in detect_canonical_absorption(bars[-1], _build_fp(bars[-1]))}
    except Exception:
        pass

    out: list[TrappedCohort] = []
    for sw in sweeps:
        if sw.stale or not sw.delta_confirms:
            continue
        if sw.age_bars > max_age_bars:
            continue
        # Unclassified (age 0, just fired) is tentative; otherwise require a trap class.
        if sw.classification and sw.classification not in _TRAP_CLASS:
            continue

        if sw.sweep_type == "sweep_high":
            side, bias = "late_longs", "short"
            choch_confirms = (choch_dir == "bear")
            absorption_confirms = ("sell" in absorb_sides)
        elif sw.sweep_type == "sweep_low":
            side, bias = "late_shorts", "long"
            choch_confirms = (choch_dir == "bull")
            absorption_confirms = ("buy" in absorb_sides)
        else:
            continue

        still_underwater = (sw.pattern == "sweep_reclaim")
        struct_w = _structural_weight(sw.level_label)

        # Score: sweep conviction × HTF significance, decayed by age, with
        # underwater / choch / absorption bonuses. Clamped to [0,1].
        age_decay = max(0.4, 1.0 - sw.age_bars / (max_age_bars + 4))
        score = sw.confidence * struct_w * age_decay
        if still_underwater:
            score *= 1.25
        else:
            score *= 0.7          # grab fired but not yet reclaimed = weaker
        if choch_confirms:
            score *= 1.15
        if absorption_confirms:
            score *= 1.15
        score = max(0.0, min(1.0, score))

        out.append(TrappedCohort(
            side=side, bias=bias, price=round(sw.swept_level, 4),
            level_label=sw.level_label, age_bars=sw.age_bars,
            still_underwater=still_underwater, structural_weight=round(struct_w, 2),
            choch_confirms=choch_confirms, absorption_confirms=absorption_confirms,
            score=round(score, 3),
            reason=(f"{sw.sweep_type}@{sw.level_label} {sw.swept_level:.2f} "
                    f"cls={sw.classification or 'pending'} age={sw.age_bars} "
                    f"reclaim={still_underwater} choch={choch_confirms} absorb={absorption_confirms}"),
        ))

    out.sort(key=lambda c: c.score, reverse=True)
    return out


def dominant_trap(symbol: str, primary_tf: str = "15m",
                  min_score: float = 0.0) -> TrappedCohort | None:
    """Highest-conviction trapped cohort, or None. Convenience for a single read
    (direction vote / entry trigger)."""
    cohorts = [c for c in trapped_cohorts(symbol, primary_tf) if c.score >= min_score]
    return cohorts[0] if cohorts else None
