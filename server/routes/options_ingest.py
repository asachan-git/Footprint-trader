"""POST /options/ingest — store latest option chain snapshot for a symbol.

Body: {
  "symbol": "NIFTY",
  "underlying_ltp": 22150.5,
  "expiry": "2026-05-29",
  "chain": [...],   # list of strike dicts from dhan.option_chain.fetch_chain
  "bar_id": "...",  # optional — the bar that triggered this snapshot
}

The chain is kept in memory keyed by symbol. The /options/decide route reads
from this store. Old snapshots are overwritten; no persistence needed
(chain is re-fetched from Dhan on each ingestion cycle).
"""

from __future__ import annotations

import logging
import time

from flask import Blueprint, jsonify, request

bp = Blueprint("options_ingest", __name__)
LOG = logging.getLogger(__name__)

# In-memory store: symbol → latest snapshot
_CHAIN_STORE: dict[str, dict] = {}


def get_latest(symbol: str) -> dict | None:
    return _CHAIN_STORE.get(symbol)


@bp.route("/options/ingest", methods=["POST"])
def options_ingest():
    body = request.get_json(force=True, silent=True) or {}
    symbol = str(body.get("symbol") or "").upper()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400

    chain = body.get("chain") or []
    underlying_ltp = float(body.get("underlying_ltp") or 0)
    expiry = str(body.get("expiry") or "")

    if not chain:
        return jsonify({"error": "empty chain"}), 400
    if underlying_ltp <= 0:
        return jsonify({"error": "underlying_ltp must be > 0"}), 400

    _CHAIN_STORE[symbol] = {
        "symbol": symbol,
        "underlying_ltp": underlying_ltp,
        "expiry": expiry,
        "chain": chain,
        "bar_id": body.get("bar_id", ""),
        "stored_at": int(time.time()),
    }

    LOG.info(f"[options_ingest] {symbol} {expiry} {len(chain)} strikes ltp={underlying_ltp}")
    return jsonify({"ok": True, "symbol": symbol, "strikes": len(chain)})
