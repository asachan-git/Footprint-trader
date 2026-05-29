"""Decision Validator — veto layer for every grid decision.

Applies to ALL decisions (Claude, mechanical, FULL, RESTRICTED, OFF).
Called from router.dispatch() before grid legs are placed.

Checks (in order):
  1. Feed quality — skip if feed suspect
  2. Session active hours — skip if outside symbol's trading window
  3. Confluence score — reject entry zone below 0.40
  4. Regime alignment — reject decision opposing high-confidence trend
     UNLESS counter_trend_exhaustion=True (allowed with cap)
  5. Auction acceptance — reject if LVN acceptance moving against decision
  6. Delta floor — reject if recent delta below symbol's dynamic floor

All checks wrapped in try/except — single failure does not veto the trade.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Literal

log = logging.getLogger(__name__)


class VetoReason(str, Enum):
    FEED_SUSPECT       = "feed_suspect"
    OUTSIDE_HOURS      = "outside_hours"
    LOW_CONFLUENCE     = "low_confluence"
    REGIME_MISMATCH    = "regime_mismatch"
    AUCTION_ACCEPTANCE = "auction_acceptance"
    DELTA_BELOW_FLOOR  = "delta_below_floor"
    INVALID_SIDE       = "invalid_side"


@dataclass
class ValidationResult:
    ok: bool
    score: float           # 0.0–1.0 composite (higher = more confident to place)
    veto_reason: VetoReason | None
    details: dict


def validate(
    side: Literal["long", "short", "flat"],
    symbol: str,
    bar,
    settings: dict,
    entry_price: float | None = None,
    counter_trend_exhaustion: bool = False,
    feed_quality_score: float = 1.0,
) -> ValidationResult:
    """Gate a decision. Returns ok=True/False with veto_reason.

    Args:
        side:                     Proposed trade direction.
        symbol:                   Trading symbol.
        bar:                      Current bar (pipeline.types.Bar).
        settings:                 Full settings dict.
        entry_price:              Proposed first leg price (for confluence check).
        counter_trend_exhaustion: If True, regime mismatch check is relaxed.
        feed_quality_score:       From data_validator (default 1.0 = clean).
    """
    if side == "flat":
        return ValidationResult(ok=False, score=0.0,
                                veto_reason=VetoReason.INVALID_SIDE,
                                details={"reason": "flat side has no trade to validate"})

    details: dict = {}
    score_components: list[float] = []
    primary_tf = str((settings.get("instrument") or {}).get("primary_tf", "15m"))

    # ── 1. Feed quality ────────────────────────────────────────────────────────
    if feed_quality_score < 0.50:
        return ValidationResult(
            ok=False, score=feed_quality_score,
            veto_reason=VetoReason.FEED_SUSPECT,
            details={"feed_quality_score": feed_quality_score},
        )
    score_components.append(feed_quality_score)

    # ── 2. Session active hours ────────────────────────────────────────────────
    in_hours = True
    try:
        from pipeline.features.session import in_active_hours
        in_hours = in_active_hours(symbol, bar.close_ts)
    except Exception:
        pass
    if not in_hours:
        return ValidationResult(
            ok=False, score=0.0,
            veto_reason=VetoReason.OUTSIDE_HOURS,
            details={"close_ts": bar.close_ts},
        )
    score_components.append(1.0)

    # ── 3. Confluence score ────────────────────────────────────────────────────
    confluence_score = 0.5  # default when no zone data
    if entry_price is not None:
        try:
            from pipeline.features.confluence import score_zone
            from pipeline.features.vp_cache import get as vp_get
            from pipeline.footprint import build as build_fp
            from pipeline.features.session import htf_bias as _htf_bias

            daily_vp = vp_get(symbol, "daily")
            fp = build_fp(bar)
            session_cvd = sum(b.delta or 0 for b in [bar])  # single bar as proxy
            htf = _htf_bias(symbol)
            zs = score_zone(entry_price, bar, fp, daily_vp, side, session_cvd, htf)
            confluence_score = zs.score if zs else 0.5
            details["confluence_score"] = round(confluence_score, 3)
            details["confluence_components"] = getattr(zs, "components", {})
        except Exception as e:
            log.debug(f"[validator] confluence check skipped: {e}")

    if confluence_score < 0.40:
        return ValidationResult(
            ok=False, score=confluence_score,
            veto_reason=VetoReason.LOW_CONFLUENCE,
            details=details,
        )
    score_components.append(confluence_score)

    # ── 4. Regime alignment ────────────────────────────────────────────────────
    regime_ok = True
    regime_detail = {}
    try:
        from pipeline.features.day_type import get_regime, is_blocked_by_regime
        floor = float((settings.get("regime") or {}).get("block_against_trend_confidence", 0.75))
        regime = get_regime(symbol, primary_tf)
        blocked, reason = is_blocked_by_regime(regime, side, confidence_floor=floor)
        regime_detail = {"regime_type": regime.type, "confidence": regime.confidence, "reason": reason}
        if blocked and not counter_trend_exhaustion:
            regime_ok = False
        elif blocked and counter_trend_exhaustion:
            # Allowed but noted — router will cap bias_strength at 2
            regime_detail["override"] = "counter_trend_exhaustion"
        score_components.append(regime.confidence if not blocked else 0.5)
    except Exception as e:
        log.debug(f"[validator] regime check skipped: {e}")
        score_components.append(0.8)

    if not regime_ok:
        details.update(regime_detail)
        return ValidationResult(
            ok=False, score=sum(score_components) / max(len(score_components), 1),
            veto_reason=VetoReason.REGIME_MISMATCH,
            details=details,
        )
    details.update(regime_detail)

    # ── 5. Auction acceptance check ────────────────────────────────────────────
    try:
        from pipeline.features.auction import from_store as auction_from_store
        auction = auction_from_store(symbol, primary_tf)
        # Acceptance in OPPOSITE direction to trade = bad signal (market moving against)
        if auction.type == f"acceptance_long" and side == "short":
            details["auction"] = auction.type
            return ValidationResult(
                ok=False, score=sum(score_components) / max(len(score_components), 1),
                veto_reason=VetoReason.AUCTION_ACCEPTANCE,
                details=details,
            )
        if auction.type == "acceptance_short" and side == "long":
            details["auction"] = auction.type
            return ValidationResult(
                ok=False, score=sum(score_components) / max(len(score_components), 1),
                veto_reason=VetoReason.AUCTION_ACCEPTANCE,
                details=details,
            )
        score_components.append(0.9)
        details["auction"] = auction.type
    except Exception as e:
        log.debug(f"[validator] auction check skipped: {e}")
        score_components.append(0.8)

    # ── 6. Delta floor ─────────────────────────────────────────────────────────
    try:
        from pipeline.features.threshold_calibrator import get_thresholds
        thresholds = get_thresholds(symbol)
        delta_floor = float(thresholds.get("min_delta_threshold", 0))
        recent_delta = abs(bar.delta or 0)
        if recent_delta < delta_floor:
            details["delta_floor"] = {"delta": recent_delta, "floor": delta_floor}
            return ValidationResult(
                ok=False, score=sum(score_components) / max(len(score_components), 1),
                veto_reason=VetoReason.DELTA_BELOW_FLOOR,
                details=details,
            )
        score_components.append(min(1.0, recent_delta / (delta_floor + 1)))
    except Exception as e:
        log.debug(f"[validator] delta floor check skipped: {e}")
        score_components.append(0.8)

    final_score = round(sum(score_components) / max(len(score_components), 1), 3)
    return ValidationResult(ok=True, score=final_score, veto_reason=None, details=details)
