"""Server-side checks on what the EA reports about itself.

Two misconfigurations that produce no error, no rejected order and no log line
— the system looks healthy while a control it depends on is inert:

  stale EA      the per-leg disaster stop is placed by the server but silently
                discarded by the terminal. Builds before 1.10 called
                OrderModify(ticket, price, 0.0, tp, ...), and OrderModify takes
                an ABSOLUTE stop, so every fulcrum shift and every TP refresh
                wiped the SL off all legs. A placement-time stop survived only
                until the first re-anchor.
  magic window  InpMagicRange gates REPORTING, not execution. A stale chart
                input narrows the window; the EA keeps trading those magics but
                stops reporting them, so the server sees no positions, believes
                the cycle is flat, and runs zero exit logic against it. Cost
                about 9k unbooked on 2026-08-06. The tell was `grep -c
                "reconcile:"` returning 0 while positions were live.

Both are advisory: they warn loudly and never block execution. A guard that
halts trading on its own false positive is worse than the failure it watches.
"""
from __future__ import annotations

MIN_EA_VERSION = (1, 11)


def _parse(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in str(v).strip().split(".")[:3])
    except (TypeError, ValueError):
        return ()


def check_ea_version(reported: str | None, minimum: tuple[int, ...] = MIN_EA_VERSION) -> str | None:
    """Warning text when the EA is older than `minimum` (or does not report)."""
    if not reported:
        return ("EA did not report a version — assume it predates 1.11, so the per-leg "
                "disaster SL is being wiped on every pending modify. Recompile and reattach.")
    got = _parse(reported)
    if not got:
        return f"EA reported an unparseable version {reported!r}; expected >= {'.'.join(map(str, minimum))}"
    if got < minimum:
        return (f"EA v{reported} is older than v{'.'.join(map(str, minimum))} — ExecModifyPending "
                f"wipes the SL on every fulcrum shift, so disaster_sl_usd is inert. Recompile.")
    return None


def check_magic_window(active_magics, magic_lo, magic_hi) -> str | None:
    """Warning text when the server believes a magic is live that the EA's
    reporting window excludes — the exact shape of the 2026-08-06 loss."""
    try:
        lo, hi = int(magic_lo or 0), int(magic_hi or 0)
    except (TypeError, ValueError):
        return None
    if hi <= lo:
        return None                                   # not reported → nothing to check
    outside = sorted({int(m) for m in (active_magics or []) if not (lo <= int(m) < hi)})
    if not outside:
        return None
    return (f"server believes magics {outside} are active but the EA only reports "
            f"[{lo}, {hi}) — those cycles are invisible to it and get NO exit logic. "
            f"Fix InpMagic/InpMagicRange on the chart.")
