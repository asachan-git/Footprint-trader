"""Parse userscript payload (format=userscript_v1) → canonical Bar.

Schema of `raw_frame` is filled in after Phase 0b spike identifies it.
For now this is a stub that raises until the real shape is known.
"""

from __future__ import annotations

from ..types import Bar, Level, OHLC


def _extract_ladders(raw: dict) -> tuple[tuple[Level, ...], tuple[Level, ...]]:
    # TODO: populate after Phase 0b spike identifies frame schema
    raise NotImplementedError(
        "userscript_v1 ladder extraction stub — fill in after spike identifies raw_frame shape"
    )


def parse(payload: dict) -> Bar:
    raw = payload["raw_frame"]
    bid, ask = _extract_ladders(raw)
    ohlc = raw.get("ohlc", {"o": 0, "h": 0, "l": 0, "c": 0})
    return Bar(
        bar_id=payload["bar_id"],
        symbol=payload["symbol"],
        tf=payload["tf"],
        close_ts=int(payload["close_ts"]),
        source=payload.get("source", "live"),
        ohlc=OHLC(o=ohlc["o"], h=ohlc["h"], l=ohlc["l"], c=ohlc["c"]),
        bid_ladder=bid,
        ask_ladder=ask,
    )
