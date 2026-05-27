"""Fetch and parse Dhan option chain for a given underlying + expiry.

Returns a list of strike dicts, each with "ce" and/or "pe" sub-dicts containing
LTP, OI, OI change, IV, Greeks, bid/ask, and security_id for order placement.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from dhan.auth import get_client
from dhan.instruments import Instrument

LOG = logging.getLogger(__name__)


def nearest_expiry(instrument: Instrument) -> str:
    """Return nearest weekly (or monthly for stocks) expiry as YYYY-MM-DD."""
    today = date.today()
    weekday = instrument.expiry_weekday
    days_ahead = (weekday - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7  # skip same-day expiry — too close to expiry, liquidity dries up
    expiry = today + timedelta(days=days_ahead)
    return expiry.strftime("%Y-%m-%d")


def next_expiry(instrument: Instrument) -> str:
    """Return the expiry after the nearest one."""
    today = date.today()
    weekday = instrument.expiry_weekday
    days_ahead = (weekday - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    expiry = today + timedelta(days=days_ahead + 7)
    return expiry.strftime("%Y-%m-%d")


def _parse_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalise a single option row from Dhan API response."""
    return {
        "security_id": str(row.get("security_id") or row.get("securityId") or ""),
        "trading_symbol": str(row.get("trading_symbol") or row.get("tradingSymbol") or ""),
        "ltp": float(row.get("last_price") or row.get("LTP") or row.get("ltp") or 0),
        "oi": int(row.get("open_interest") or row.get("OI") or row.get("oi") or 0),
        "oi_change": int(
            row.get("oi_day_change") or row.get("oiDayChange") or row.get("oi_change") or 0
        ),
        "volume": int(row.get("volume") or 0),
        "iv": float(row.get("implied_volatility") or row.get("iv") or row.get("IV") or 0),
        "delta": float(row.get("delta") or 0),
        "theta": float(row.get("theta") or 0),
        "gamma": float(row.get("gamma") or 0),
        "vega": float(row.get("vega") or 0),
        "bid": float(row.get("bid_price") or row.get("bid") or 0),
        "ask": float(row.get("ask_price") or row.get("ask") or 0),
    }


def fetch_chain(
    instrument: Instrument, expiry: str | None = None
) -> list[dict[str, Any]]:
    """Fetch option chain. Returns list of strike dicts sorted by strike.

    Each entry: {"strike": float, "expiry": str, "ce": {...}, "pe": {...}}
    ce/pe keys may be absent if no data for that option type at that strike.
    """
    if expiry is None:
        expiry = nearest_expiry(instrument)

    dhan = get_client()
    try:
        resp = dhan.option_chain(
            under_security_id=instrument.under_security_id,
            under_exchange_segment=instrument.under_exchange_segment,
            expiry=expiry,
        )
    except Exception as e:
        LOG.error(f"[option_chain] fetch failed {instrument.symbol} {expiry}: {e}")
        return []

    if not resp or resp.get("status") == "failure":
        LOG.warning(f"[option_chain] bad response: {resp}")
        return []

    raw = resp.get("data") or []
    if not raw:
        LOG.warning(f"[option_chain] empty data for {instrument.symbol} {expiry}")
        return []

    strikes: dict[float, dict[str, Any]] = {}

    for row in raw:
        strike = float(row.get("strike_price") or row.get("strikePrice") or 0)
        if strike <= 0:
            continue
        opt_type = str(row.get("option_type") or row.get("optionType") or "").upper()

        entry = strikes.setdefault(strike, {"strike": strike, "expiry": expiry})
        parsed = _parse_row(row)

        if opt_type in ("CALL", "CE"):
            entry["ce"] = parsed
        elif opt_type in ("PUT", "PE"):
            entry["pe"] = parsed

    chain = sorted(strikes.values(), key=lambda x: x["strike"])
    LOG.debug(f"[option_chain] {instrument.symbol} {expiry}: {len(chain)} strikes")
    return chain


def get_underlying_ltp(instrument: Instrument) -> float | None:
    """Fetch current underlying LTP via Dhan market quote API."""
    dhan = get_client()
    try:
        resp = dhan.get_market_quote(
            securities={instrument.under_exchange_segment: [instrument.under_security_id]}
        )
        data = (resp.get("data") or {})
        key = f"{instrument.under_exchange_segment}:{instrument.under_security_id}"
        ltp = float(
            (data.get(key) or data.get(instrument.under_security_id) or {}).get("last_price") or 0
        )
        return ltp if ltp > 0 else None
    except Exception as e:
        LOG.warning(f"[option_chain] get_underlying_ltp failed {instrument.symbol}: {e}")
        return None
