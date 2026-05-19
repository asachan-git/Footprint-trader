"""Walk-forward outcome labeling for replay-mode decisions.

Given a decision at bar K with entry/SL/TP, walk through bars K+1..K+N
(N = max_lookahead) until SL or TP touched, or N expires. Record realized R,
MFE, MAE in R units.

This is a small backtester. Pure function over bars + decisions.
"""

from __future__ import annotations

from dataclasses import dataclass

from pipeline.types import Bar


@dataclass(frozen=True)
class Outcome:
    decision_bar_id: str
    hit: str          # "tp" | "sl" | "expire"
    bars_to_exit: int
    realized_r: float
    mfe_r: float
    mae_r: float


def label(
    decision_bar_id: str,
    side: str,
    entry: float,
    sl: float,
    tp: float,
    forward: list[Bar],
    max_lookahead: int = 30,
) -> Outcome:
    risk = abs(entry - sl)
    if risk <= 0:
        raise ValueError("risk == 0 in label()")

    mfe = 0.0
    mae = 0.0
    for i, b in enumerate(forward[:max_lookahead], start=1):
        if side == "long":
            high_r = (b.ohlc.h - entry) / risk
            low_r = (b.ohlc.l - entry) / risk
            mfe = max(mfe, high_r)
            mae = min(mae, low_r)
            if b.ohlc.l <= sl:
                return Outcome(decision_bar_id, "sl", i, -1.0, mfe, mae)
            if b.ohlc.h >= tp:
                return Outcome(decision_bar_id, "tp", i, (tp - entry) / risk, mfe, mae)
        else:  # short
            high_r = (entry - b.ohlc.l) / risk
            low_r = (entry - b.ohlc.h) / risk
            mfe = max(mfe, high_r)
            mae = min(mae, low_r)
            if b.ohlc.h >= sl:
                return Outcome(decision_bar_id, "sl", i, -1.0, mfe, mae)
            if b.ohlc.l <= tp:
                return Outcome(decision_bar_id, "tp", i, (entry - tp) / risk, mfe, mae)

    last = forward[max_lookahead - 1] if forward[:max_lookahead] else None
    if last is None:
        return Outcome(decision_bar_id, "expire", 0, 0.0, 0.0, 0.0)
    if side == "long":
        realized = (last.ohlc.c - entry) / risk
    else:
        realized = (entry - last.ohlc.c) / risk
    return Outcome(decision_bar_id, "expire", len(forward[:max_lookahead]), realized, mfe, mae)
