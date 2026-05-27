"""NSE F&O instrument definitions for Dhan API.

Security IDs are Dhan's internal IDs (same as NSE security IDs for indices).
Lot sizes per NSE circulars (update when SEBI revises them).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Instrument:
    symbol: str
    under_security_id: str        # for option_chain API
    under_exchange_segment: str   # "IDX_I" for indices, "NSE_EQ" for stocks
    option_exchange_segment: str  # "NSE_FNO"
    lot_size: int
    tick_size: float
    strike_step: float            # option strike interval
    # weekday of weekly expiry: 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri
    expiry_weekday: int = 3


INSTRUMENTS: dict[str, Instrument] = {
    "NIFTY": Instrument(
        symbol="NIFTY",
        under_security_id="13",
        under_exchange_segment="IDX_I",
        option_exchange_segment="NSE_FNO",
        lot_size=25,
        tick_size=0.05,
        strike_step=50.0,
        expiry_weekday=3,  # Thursday
    ),
    "BANKNIFTY": Instrument(
        symbol="BANKNIFTY",
        under_security_id="25",
        under_exchange_segment="IDX_I",
        option_exchange_segment="NSE_FNO",
        lot_size=15,
        tick_size=0.05,
        strike_step=100.0,
        expiry_weekday=2,  # Wednesday
    ),
    "FINNIFTY": Instrument(
        symbol="FINNIFTY",
        under_security_id="27",
        under_exchange_segment="IDX_I",
        option_exchange_segment="NSE_FNO",
        lot_size=40,
        tick_size=0.05,
        strike_step=50.0,
        expiry_weekday=1,  # Tuesday
    ),
    # F&O stocks — security IDs are NSE scrip codes (examples; verify on Dhan instrument master)
    "RELIANCE": Instrument(
        symbol="RELIANCE",
        under_security_id="2885",
        under_exchange_segment="NSE_EQ",
        option_exchange_segment="NSE_FNO",
        lot_size=250,
        tick_size=0.05,
        strike_step=20.0,
        expiry_weekday=3,  # Thursday (monthly last Thursday)
    ),
    "HDFCBANK": Instrument(
        symbol="HDFCBANK",
        under_security_id="1333",
        under_exchange_segment="NSE_EQ",
        option_exchange_segment="NSE_FNO",
        lot_size=550,
        tick_size=0.05,
        strike_step=10.0,
        expiry_weekday=3,
    ),
    "TCS": Instrument(
        symbol="TCS",
        under_security_id="11536",
        under_exchange_segment="NSE_EQ",
        option_exchange_segment="NSE_FNO",
        lot_size=150,
        tick_size=0.05,
        strike_step=50.0,
        expiry_weekday=3,
    ),
}


def get(symbol: str) -> Instrument:
    if symbol not in INSTRUMENTS:
        raise ValueError(f"Unknown instrument: {symbol!r}. Known: {sorted(INSTRUMENTS)}")
    return INSTRUMENTS[symbol]


def all_symbols() -> list[str]:
    return sorted(INSTRUMENTS)
