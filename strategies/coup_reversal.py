"""CoupReversal — coup mechanics, but the user's observed REVERSAL pattern.

Same engine as `coup` (Coup), only the trigger's absorption detector runs in
`reversal` mode (see pipeline/features/absorption.py): a high-volume candle that
CLOSES rejecting an extreme (lower-wick for longs), with a real volume fight at
that extreme (aggressive buying and/or sellers absorbed), confirmed by the next
candle's follow-through aggression. Runs in paper ALONGSIDE the default
`momentum` coup to accumulate live samples for the pattern (it is rare).

Distinct `name` → its own data/strategies/coup_reversal/ store. Config in
config/strategies.yaml sets absorption_mode=reversal + vol_lookback.
"""
from __future__ import annotations

from .coup import Coup


class CoupReversal(Coup):
    name = "coup_reversal"

    def __init__(self, config: dict | None = None) -> None:
        cfg = dict(config or {})
        cfg.setdefault("absorption_mode", "reversal")
        cfg.setdefault("vol_lookback", 10)
        super().__init__(cfg)
