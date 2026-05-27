"""Select candidate option strikes for Claude to choose from.

Given:
  - directional bias from footprint (long/short/flat)
  - confidence (0–1)
  - current option chain
  - options signal

Returns 3 candidates (ATM, 1OTM, 2OTM) for the appropriate option type.
Claude picks the final strike from this list via the submit_decision tool.

Strike selection convention (buying options):
  long  bias → buy CE → OTM = strikes above underlying (higher strike = deeper OTM)
  short bias → buy PE → OTM = strikes below underlying (lower strike = deeper OTM)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from options.signal import OptionsSignal


@dataclass
class StrikeCandidate:
    option_type: str      # "CE" or "PE"
    strike: float
    expiry: str
    security_id: str
    trading_symbol: str
    ltp: float
    bid: float
    ask: float
    iv: float
    oi: int
    oi_change: int
    delta: float
    distance_otm: int  # 0=ATM, 1=1OTM, 2=2OTM
    label: str         # "ATM", "1OTM", "2OTM"


def select_candidates(
    chain: list[dict[str, Any]],
    signal: OptionsSignal,
    bias: str,          # "long" | "short" | "flat"
    confidence: float,
    expiry: str,
    n_candidates: int = 3,
) -> list[StrikeCandidate]:
    """Return up to n_candidates strike candidates for Claude to select from."""
    if bias == "flat" or not chain:
        return []

    opt_type = "CE" if bias == "long" else "PE"
    opt_key = "ce" if bias == "long" else "pe"

    chain_sorted = sorted(chain, key=lambda x: x["strike"])

    # ATM index
    atm_idx = min(
        range(len(chain_sorted)),
        key=lambda i: abs(chain_sorted[i]["strike"] - signal.atm_strike),
    )

    # For CE: higher strikes are OTM. For PE: lower strikes are OTM.
    if opt_type == "CE":
        raw_indices = [atm_idx, atm_idx + 1, atm_idx + 2]
    else:
        raw_indices = [atm_idx, atm_idx - 1, atm_idx - 2]

    labels = ["ATM", "1OTM", "2OTM"]
    candidates: list[StrikeCandidate] = []

    for dist, idx in enumerate(raw_indices[:n_candidates]):
        if idx < 0 or idx >= len(chain_sorted):
            continue
        row = chain_sorted[idx]
        opt = row.get(opt_key, {})
        if not opt or not opt.get("security_id"):
            continue
        if opt.get("ltp", 0) <= 0:
            continue

        candidates.append(
            StrikeCandidate(
                option_type=opt_type,
                strike=row["strike"],
                expiry=expiry,
                security_id=opt["security_id"],
                trading_symbol=opt.get("trading_symbol", ""),
                ltp=opt["ltp"],
                bid=opt.get("bid", 0.0),
                ask=opt.get("ask", 0.0),
                iv=opt.get("iv", 0.0),
                oi=opt.get("oi", 0),
                oi_change=opt.get("oi_change", 0),
                delta=abs(opt.get("delta", 0.0)),
                distance_otm=dist,
                label=labels[dist],
            )
        )

    return candidates
