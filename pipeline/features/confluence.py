"""Confluence Scorer — weighted signal strength per entry zone.

Scores each potential grid entry zone using 5 orderflow factors.
Higher score = more confident entry, higher lot allocation.

Weights (sum to 1.0):
  footprint absorption at zone:  0.30  (sellers/buyers defending the level)
  stacked imbalance at zone:     0.25  (aggressive participation at level)
  HVN rank (VP proximity):       0.20  (institutional volume zone)
  CVD alignment:                 0.15  (session flow direction matches)
  HTF bias alignment:            0.10  (daily SMA direction matches)

Lot allocation:
  score 0.0–0.4 → min 30% of base qty (weak confluence, small position)
  score 0.4–0.7 → scaled 30%–70%
  score 0.7–1.0 → scaled 70%–100% (strong confluence, full size)

Grid tier spacing from VP structure (Pre-5 from plan):
  - Don't place legs inside LVN zones (fast-move — bad fills, SL risk)
  - Tighten spacing at HVN edges (high-confidence levels)
  - Use VAH→POC and POC→VAL distances as natural tier widths
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ZoneScore:
    price: float
    score: float       # 0.0–1.0 weighted
    qty_pct: float     # fraction of base qty for this leg (0.30–1.00)
    reasons: list[str] = field(default_factory=list)
    skip: bool = False # True if zone falls in LVN (avoid bad fills)
    skip_reason: str = ""


def score_zone(
    price: float,
    bar,
    fp,
    daily_vp: dict | None,
    side: str,
    session_cvd: float = 0.0,
    htf_bias: str = "neutral",
) -> ZoneScore:
    """Score a single entry zone price.

    Args:
        price:       Target entry price to evaluate.
        bar:         Current Bar object.
        fp:          FootprintMatrix for current bar.
        daily_vp:    Today's VP dict (poc, vah, val, hvn_zones, lvn_zones).
        side:        "long" | "short" — intended trade direction.
        session_cvd: Session cumulative delta (positive = net buying).
        htf_bias:    "bullish" | "bearish" | "neutral" from 20-SMA.
    """
    score = 0.0
    reasons: list[str] = []

    # ── LVN guard — skip zones inside LVN (fast-move, poor fills) ───────────
    if daily_vp:
        for lvn in daily_vp.get("lvn_zones", []):
            if lvn["low"] <= price <= lvn["high"]:
                return ZoneScore(
                    price=price, score=0.0, qty_pct=0.0,
                    skip=True,
                    skip_reason=f"inside LVN {lvn['low']:.2f}–{lvn['high']:.2f} — fast-move zone, avoid",
                )

    # ── 1. Footprint absorption at zone (weight 0.30) ────────────────────────
    abs_score = _absorption_at_zone(price, bar, fp, side, daily_vp)
    score += abs_score * 0.30
    if abs_score > 0:
        reasons.append(f"absorption@zone({abs_score:.2f})")

    # ── 2. Stacked imbalance at zone (weight 0.25) ───────────────────────────
    imb_score = _imbalance_at_zone(price, fp, side, daily_vp)
    score += imb_score * 0.25
    if imb_score > 0:
        reasons.append(f"imbalance@zone({imb_score:.2f})")

    # ── 3. HVN rank — proximity to high-volume node (weight 0.20) ───────────
    hvn_score = _hvn_proximity(price, daily_vp)
    score += hvn_score * 0.20
    if hvn_score > 0:
        reasons.append(f"hvn_proximity({hvn_score:.2f})")

    # ── 4. CVD alignment (weight 0.15) ──────────────────────────────────────
    cvd_score = _cvd_alignment(side, session_cvd)
    score += cvd_score * 0.15
    if cvd_score > 0:
        reasons.append(f"cvd_aligned({cvd_score:.2f})")

    # ── 5. HTF bias alignment (weight 0.10) ─────────────────────────────────
    htf_score = _htf_alignment(side, htf_bias)
    score += htf_score * 0.10
    if htf_score > 0:
        reasons.append(f"htf_aligned({htf_score:.2f})")

    score = round(min(1.0, score), 3)
    qty_pct = round(0.30 + 0.70 * score, 3)   # min 30%, max 100%

    return ZoneScore(price=price, score=score, qty_pct=qty_pct, reasons=reasons)


def score_zones(
    entry_zones: list[float],
    bar,
    fp,
    daily_vp: dict | None,
    side: str,
    session_cvd: float = 0.0,
    htf_bias: str = "neutral",
) -> list[ZoneScore]:
    """Score all entry zones for a VP setup, ordered by price (descending for long)."""
    scored = [
        score_zone(z, bar, fp, daily_vp, side, session_cvd, htf_bias)
        for z in entry_zones
    ]
    # Sort: for long, highest price first (closest to current price);
    # for short, lowest price first (closest to current price)
    reverse = (side == "long")
    return sorted(scored, key=lambda z: z.price, reverse=reverse)


def grid_tier_spacing(daily_vp: dict | None) -> dict:
    """Derive natural grid tier widths from VA structure.

    Tier 1: VAL to POC midpoint (densest, use for leg 1–2)
    Tier 2: POC to VAH midpoint (use for leg 3)

    HVN within tier → tighten spacing 30%
    LVN within tier → skip (don't place leg there)
    """
    if not daily_vp:
        return {"tier1": None, "tier2": None}
    poc = daily_vp.get("poc")
    vah = daily_vp.get("vah")
    val = daily_vp.get("val")
    if not poc or not vah or not val:
        return {"tier1": None, "tier2": None}
    tier1 = (poc - val) / 2 if poc > val else None
    tier2 = (vah - poc) / 2 if vah > poc else None
    return {"tier1": round(tier1, 4) if tier1 else None,
            "tier2": round(tier2, 4) if tier2 else None}


# ── signal sub-scores ─────────────────────────────────────────────────────────

def _absorption_at_zone(
    price: float, bar, fp, side: str, daily_vp: dict | None
) -> float:
    """Score 0–1: how much confirming absorption is at/near this zone."""
    try:
        from pipeline.features.absorption import detect_absorption
        absorptions = detect_absorption(bar, fp, absorb_ratio=0.15)
        price_tol = price * 0.001  # 0.1% tolerance
        for a in absorptions:
            if abs(a.price - price) <= price_tol:
                if (side == "long" and a.side == "buy") or (side == "short" and a.side == "sell"):
                    return min(1.0, a.bar_pct * 4)  # 25% bar pct → 1.0
    except Exception:
        pass
    return 0.0


def _imbalance_at_zone(price: float, fp, side: str, daily_vp: dict | None) -> float:
    """Score 0–1: stacked imbalances at/near the zone in the trade direction."""
    try:
        from pipeline.features.stacked_imbalance import stacked_imbalances
        stacked = stacked_imbalances(fp, min_stack=2)
        price_tol = price * 0.002
        for s in stacked:
            if abs(s.price_low - price) <= price_tol or abs(s.price_high - price) <= price_tol:
                if (side == "long" and s.side == "bid") or (side == "short" and s.side == "ask"):
                    return min(1.0, s.count / 5.0)  # 5 levels = full score
    except Exception:
        pass
    return 0.0


def _hvn_proximity(price: float, daily_vp: dict | None) -> float:
    """Score 0–1: how close zone is to a high-volume node."""
    if not daily_vp:
        return 0.0
    hvns = daily_vp.get("hvn_zones", [])
    poc = daily_vp.get("poc")
    price_range = daily_vp.get("price_range", 1.0) or 1.0

    for hvn in hvns:
        if hvn["low"] <= price <= hvn["high"]:
            return 1.0  # inside HVN = full score
        dist = min(abs(price - hvn["low"]), abs(price - hvn["high"]))
        if dist / price_range < 0.02:  # within 2% of price range from HVN
            return 0.7

    if poc and abs(price - poc) / price_range < 0.01:
        return 0.8  # near POC

    return 0.0


def _cvd_alignment(side: str, session_cvd: float) -> float:
    """Score 0–1: session CVD direction matches trade direction."""
    if side == "long" and session_cvd > 10:
        return min(1.0, session_cvd / 100)
    if side == "short" and session_cvd < -10:
        return min(1.0, abs(session_cvd) / 100)
    return 0.0


def _htf_alignment(side: str, htf_bias: str) -> float:
    """Score 0–1: daily HTF bias aligns with trade direction."""
    if side == "long" and htf_bias == "bullish":
        return 1.0
    if side == "short" and htf_bias == "bearish":
        return 1.0
    if htf_bias == "neutral":
        return 0.5
    return 0.0
