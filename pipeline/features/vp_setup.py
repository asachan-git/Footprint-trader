"""VP Setup Classifier — 4 institutional mean-reversion setups.

Classifies the current bar into one of four VP-based trade setups using
the relationship between current price, daily VP (POC/VAH/VAL/HVN/LVN),
prior day VP, and weekly VP.

The 4 setups:
  val_reclaim:   Price was below VAL, now closing back inside VA.
                 Long bias. Buyers reclaiming value area.

  vah_rejection: Price was above VAH, now closing back inside VA.
                 Short bias. Sellers rejecting breakout.

  hvn_bounce:    Price at HVN zone with footprint absorption confirming.
                 Reversal bias in direction of prior trend.

  poc_migration: Daily POC consistently migrating same direction ≥ 2 days.
                 Trend-follow setup. Wider target, max 2 legs.

  acceptance:    Price closed outside VA and subsequent bar also outside.
                 Breakout continuation. Tight grid (1-2 legs), trend follow.

  none:          No clear setup — flat bias.

Entry zones are ordered [primary, secondary, tertiary] using Fibonacci
levels from wave.py when available, otherwise HVN/LVN-derived levels.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class VPSetup:
    setup_type: str          # "val_reclaim" | "vah_rejection" | "hvn_bounce" | "poc_migration" | "acceptance" | "none"
    bias: str                # "long" | "short" | "neutral"
    entry_zones: list[float] # [primary, secondary, tertiary] — ordered entry prices
    target_zones: list[float]# [T1, T2] nearest to farthest
    max_legs: int            # 3 for mean-reversion, 2 for trend, 1 for cautious
    setup_strength: float    # 0.0–1.0
    reason: str
    grid_mode: str           # "mean_reversion" | "directional" | "cautious"


_NO_SETUP = VPSetup(
    setup_type="none", bias="neutral",
    entry_zones=[], target_zones=[],
    max_legs=1, setup_strength=0.0,
    reason="no qualifying VP setup",
    grid_mode="cautious",
)


def classify(
    current_price: float,
    daily_vp: dict | None,
    prior_day_vp: dict | None,
    weekly_vp: dict | None,
    prev_bar_close: float | None,
    poc_sequence: list[float | None] | None = None,
    wave=None,
) -> VPSetup:
    """Classify current price location into a VP trade setup.

    Args:
        current_price:   Latest bar close price.
        daily_vp:        Today's session VP dict (poc, vah, val, hvn_zones, lvn_zones).
        prior_day_vp:    Prior day completed VP dict.
        weekly_vp:       This week's VP dict.
        prev_bar_close:  Close of the bar BEFORE current (for reclaim/rejection detection).
        poc_sequence:    List of last N daily POC values (newest last) for migration check.
        wave:            WavePhase object from wave.py (optional, for entry zone refinement).
    """
    if not daily_vp:
        return _NO_SETUP

    poc = daily_vp.get("poc")
    vah = daily_vp.get("vah")
    val = daily_vp.get("val")
    hvn_zones = daily_vp.get("hvn_zones", [])
    lvn_zones = daily_vp.get("lvn_zones", [])

    if not poc or not vah or not val:
        return _NO_SETUP

    va_range = vah - val
    if va_range <= 0:
        return _NO_SETUP

    pd_poc = prior_day_vp.get("poc") if prior_day_vp else None
    pd_vah = prior_day_vp.get("vah") if prior_day_vp else None
    pd_val = prior_day_vp.get("val") if prior_day_vp else None
    wk_poc = weekly_vp.get("poc") if weekly_vp else None

    # ── Setup 1: VAL Reclaim (long) ─────────────────────────────────────────
    if (prev_bar_close is not None and prev_bar_close < val and current_price >= val):
        # Previous bar was below VAL, current bar closed above VAL — reclaim
        entry_zones = _build_long_zones(val, poc, hvn_zones, wave)
        targets = [poc]
        if vah: targets.append(vah)
        if pd_poc and pd_poc > current_price: targets.append(pd_poc)
        return VPSetup(
            setup_type="val_reclaim",
            bias="long",
            entry_zones=entry_zones[:3],
            target_zones=sorted(set(targets))[:2],
            max_legs=3,
            setup_strength=_reclaim_strength(current_price, val, poc, daily_vp),
            reason=f"price reclaimed VAL {val:.2f} from below — long into VA toward POC {poc:.2f}",
            grid_mode="mean_reversion",
        )

    # ── Setup 2: VAH Rejection (short) ──────────────────────────────────────
    if (prev_bar_close is not None and prev_bar_close > vah and current_price <= vah):
        entry_zones = _build_short_zones(vah, poc, hvn_zones, wave)
        targets = [poc]
        if val: targets.append(val)
        if pd_poc and pd_poc < current_price: targets.append(pd_poc)
        return VPSetup(
            setup_type="vah_rejection",
            bias="short",
            entry_zones=entry_zones[:3],
            target_zones=sorted(set(targets), reverse=True)[:2],
            max_legs=3,
            setup_strength=_reclaim_strength(vah, current_price, poc, daily_vp),
            reason=f"price rejected VAH {vah:.2f} from above — short into VA toward POC {poc:.2f}",
            grid_mode="mean_reversion",
        )

    # ── Setup 3: HVN Bounce ──────────────────────────────────────────────────
    for zone in hvn_zones:
        if zone["low"] <= current_price <= zone["high"]:
            # Price is inside an HVN — potential bounce if footprint confirms
            zone_mid = (zone["low"] + zone["high"]) / 2
            if current_price > zone_mid:
                # Above midpoint — bullish HVN bounce (buyers defending)
                entry_zones = [zone["low"], zone["low"] - va_range * 0.1]
                targets = [poc, vah] if current_price < poc else [vah]
                return VPSetup(
                    setup_type="hvn_bounce",
                    bias="long",
                    entry_zones=_filter_zones(entry_zones),
                    target_zones=targets[:2],
                    max_legs=2,
                    setup_strength=0.60,
                    reason=f"price in HVN {zone['low']:.2f}–{zone['high']:.2f}, above midpoint — long bias",
                    grid_mode="mean_reversion",
                )
            else:
                # Below midpoint — bearish HVN resistance
                entry_zones = [zone["high"], zone["high"] + va_range * 0.1]
                targets = [poc, val] if current_price > poc else [val]
                return VPSetup(
                    setup_type="hvn_bounce",
                    bias="short",
                    entry_zones=_filter_zones(entry_zones),
                    target_zones=targets[:2],
                    max_legs=2,
                    setup_strength=0.60,
                    reason=f"price in HVN {zone['low']:.2f}–{zone['high']:.2f}, below midpoint — short bias",
                    grid_mode="mean_reversion",
                )

    # ── Setup 4: POC Migration (trend follow) ────────────────────────────────
    if poc_sequence and len([p for p in poc_sequence if p is not None]) >= 3:
        valid = [p for p in poc_sequence if p is not None]
        rising  = all(valid[i] < valid[i+1] for i in range(len(valid)-1))
        falling = all(valid[i] > valid[i+1] for i in range(len(valid)-1))
        if rising or falling:
            bias = "long" if rising else "short"
            poc_drift = abs(valid[-1] - valid[0]) / len(valid)
            if bias == "long":
                entry = current_price
                target = current_price + poc_drift * 2
                entry_zones = [entry, entry - va_range * 0.15]
            else:
                entry = current_price
                target = current_price - poc_drift * 2
                entry_zones = [entry, entry + va_range * 0.15]
            return VPSetup(
                setup_type="poc_migration",
                bias=bias,
                entry_zones=_filter_zones(entry_zones[:2]),
                target_zones=[round(target, 2)],
                max_legs=2,
                setup_strength=min(0.85, 0.50 + len(valid) * 0.10),
                reason=f"daily POC migrating {'higher' if rising else 'lower'} {len(valid)} days: {[round(p,0) for p in valid[-3:]]}",
                grid_mode="directional",
            )

    # ── Setup 5: VAH/VAL Acceptance (breakout continuation) ─────────────────
    if prev_bar_close is not None:
        if prev_bar_close > vah and current_price > vah:
            # Both bars above VAH — acceptance above value area
            extension = current_price + va_range * 1.5
            entry_zones = [current_price, vah + (current_price - vah) * 0.5]
            return VPSetup(
                setup_type="acceptance",
                bias="long",
                entry_zones=_filter_zones(entry_zones[:2]),
                target_zones=[round(extension, 2)],
                max_legs=2,
                setup_strength=0.65,
                reason=f"price accepted above VAH {vah:.2f} — breakout continuation long",
                grid_mode="directional",
            )
        if prev_bar_close < val and current_price < val:
            # Both bars below VAL — acceptance below value area
            extension = current_price - va_range * 1.5
            entry_zones = [current_price, val - (val - current_price) * 0.5]
            return VPSetup(
                setup_type="acceptance",
                bias="short",
                entry_zones=_filter_zones(entry_zones[:2]),
                target_zones=[round(extension, 2)],
                max_legs=2,
                setup_strength=0.65,
                reason=f"price accepted below VAL {val:.2f} — breakout continuation short",
                grid_mode="directional",
            )

    return _NO_SETUP


# ── helpers ───────────────────────────────────────────────────────────────────

def _build_long_zones(val: float, poc: float, hvn_zones: list, wave) -> list[float]:
    """Entry zones for a long setup: primary at VAL, secondary at nearest HVN above VAL, tertiary deeper."""
    zones = [val]
    # Add HVN zones between VAL and POC as secondary entries
    hvn_between = [z["low"] for z in hvn_zones if val < z["low"] < poc]
    zones.extend(sorted(hvn_between)[:2])
    # Use Fibonacci levels from wave if available
    if wave and wave.fib_retrace and wave.phase in ("correction", "reversal"):
        fib_50 = wave.fib_retrace.get(0.5)
        fib_618 = wave.fib_retrace.get(0.618)
        if fib_50 and val <= fib_50 <= poc: zones.insert(1, fib_50)
        if fib_618 and val <= fib_618 <= poc: zones.append(fib_618)
    return _filter_zones(zones)


def _build_short_zones(vah: float, poc: float, hvn_zones: list, wave) -> list[float]:
    """Entry zones for a short setup: primary at VAH, secondary nearest HVN below VAH."""
    zones = [vah]
    hvn_between = [z["high"] for z in hvn_zones if poc < z["high"] < vah]
    zones.extend(sorted(hvn_between, reverse=True)[:2])
    if wave and wave.fib_retrace and wave.phase in ("correction", "reversal"):
        fib_50 = wave.fib_retrace.get(0.5)
        fib_618 = wave.fib_retrace.get(0.618)
        if fib_50 and poc <= fib_50 <= vah: zones.insert(1, fib_50)
        if fib_618 and poc <= fib_618 <= vah: zones.append(fib_618)
    return _filter_zones(zones)


def _filter_zones(zones: list) -> list[float]:
    seen: set[float] = set()
    result = []
    for z in zones:
        if z is not None and z not in seen:
            seen.add(z)
            result.append(round(z, 4))
    return result


def _reclaim_strength(price: float, boundary: float, poc: float, vp: dict) -> float:
    """Setup strength for reclaim/rejection based on distance to POC and bar_count."""
    bar_count = vp.get("bar_count", 0)
    data_quality = min(1.0, bar_count / 300)  # more bars = more reliable VP
    distance_score = min(1.0, abs(poc - boundary) / max(abs(poc), 1) * 200)
    return round(min(0.90, 0.45 + 0.30 * data_quality + 0.25 * distance_score), 2)


def from_store(symbol: str, primary_tf: str) -> VPSetup:
    """Convenience: classify from state_store latest bar + VP cache."""
    import pipeline.features.vp_cache as _vpc
    from pipeline.state_store import store as _store
    from pipeline.features.wave import classify as _wave
    from types import SimpleNamespace as _NS

    s = _store()
    bars = s.recent(symbol, primary_tf, 25)
    if len(bars) < 2:
        return _NO_SETUP

    current_price  = bars[-1].ohlc.c
    prev_bar_close = bars[-2].ohlc.c

    daily_vp    = _vpc.get(symbol, "daily")
    weekly_vp   = _vpc.get(symbol, "weekly")
    hist        = _vpc.get_history(symbol, "daily", n=2)
    prior_vp    = hist[-2] if len(hist) >= 2 else None
    poc_seq     = _vpc.poc_sequence(symbol, "daily", n=5)
    wave        = _wave(bars, lookback=min(len(bars), 20))

    return classify(
        current_price=current_price,
        daily_vp=daily_vp,
        prior_day_vp=prior_vp,
        weekly_vp=weekly_vp,
        prev_bar_close=prev_bar_close,
        poc_sequence=poc_seq,
        wave=wave,
    )
