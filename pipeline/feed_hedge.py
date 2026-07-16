"""Feed-outage protective hedge — retry → hedge → auto-remove on recovery.

When the Binance analysis feed dies mid-session, the grid stops arming (feed-stale gate) and
the auto-backfill heals the hole on recovery — but any already-open grid positions are left
NAKED during the blind window. This opens an opposite-side market position (under a dedicated
out-of-band magic) to FREEZE net P&L across the outage, and removes it once data returns, so
the balance stays intact — a bridge over the blind window.

Driven by the feed_monitor loop (no new polling thread):
  - on_stale     — called each pass a symbol is stale; counts consecutive checks and, at the
                   threshold (default 3 ≈ 90s), opens the hedge ONCE.
  - on_recovered — called on the stale→ok transition; removes the hedge, resets counters.

Net exposure + account come from ExecBridge.get_last_magics (stashed each EA poll) — the EA
keeps polling Vantage even while Binance is down, so this stays fresh. Fully fail-open: a
hedge-logic error must never break the monitor.
"""

from __future__ import annotations

import logging
import time

LOG = logging.getLogger(__name__)

# analysis_symbol → {stale_count, retry_count, hedged, side, lots, price, opened_ts}
_state: dict[str, dict] = {}


def _blank(sym: str) -> dict:
    st = {"stale_count": 0, "retry_count": 0, "hedged": False,
          "side": "", "lots": 0.0, "price": 0.0, "opened_ts": 0}
    _state[sym] = st
    return st


def chart_state(analysis_symbol: str) -> dict:
    """Draw-payload for the EA (shipped in the poll response). {active:false} when no hedge."""
    st = _state.get(analysis_symbol)
    # active only when a real hedge (positive lots) is open. A "hedged" flag with 0 lots is the
    # flat-exposure skip sentinel (nothing to draw). Still surface the retry count so the chart
    # can show the stale countdown before a hedge is placed.
    if not st or not st.get("hedged") or float(st.get("lots", 0.0)) <= 0:
        retry = int(st.get("retry_count", 0)) if st else 0
        return {"active": False, "retry": retry}
    return {"active": True, "side": st["side"], "lot": round(st["lots"], 2),
            "price": round(st["price"], 4), "retry": int(st["retry_count"])}


def _net_exposure(magics: list) -> float:
    """Σ(buy_lots − sell_lots) across all grid magics (excludes the hedge magic itself)."""
    from execution.exec_bridge import HEDGE_MAGIC
    net = 0.0
    for m in magics or []:
        if int(m.get("magic", 0)) == HEDGE_MAGIC:
            continue
        net += float(m.get("buy_lots", 0.0) or 0.0) - float(m.get("sell_lots", 0.0) or 0.0)
    return net


def _already_hedged_at_broker(magics: list) -> dict | None:
    """If the poll reports a live HEDGE_MAGIC position, return its side/lots (for restart
    rehydrate). None otherwise."""
    from execution.exec_bridge import HEDGE_MAGIC
    for m in magics or []:
        if int(m.get("magic", 0)) != HEDGE_MAGIC:
            continue
        buys, sells = int(m.get("buys", 0)), int(m.get("sells", 0))
        if buys + sells <= 0:
            continue
        if buys >= sells:
            return {"side": "buy", "lots": float(m.get("buy_lots", 0.0) or 0.0)}
        return {"side": "sell", "lots": float(m.get("sell_lots", 0.0) or 0.0)}
    return None


def _cfg(settings: dict) -> dict:
    return (settings.get("monitor") or {})


def on_stale(analysis_symbol: str, tf: str, settings: dict) -> None:
    """One stale check for this symbol. Increments the counter; hedges at the threshold."""
    mon = _cfg(settings)
    if not mon.get("feed_hedge_enabled", True):
        return
    try:
        st = _state.get(analysis_symbol) or _blank(analysis_symbol)
        st["stale_count"] += 1
        st["retry_count"] = st["stale_count"]
        after = int(mon.get("feed_hedge_after_checks", 3) or 3)
        if st["hedged"] or st["stale_count"] < after:
            return
        _open_hedge(analysis_symbol, mon)
    except Exception as e:
        LOG.warning(f"[feed_hedge] on_stale error {analysis_symbol}: {e}")


def on_recovered(analysis_symbol: str, tf: str, settings: dict) -> None:
    """Feed recovered — remove the hedge (if any) and reset counters."""
    mon = _cfg(settings)
    if not mon.get("feed_hedge_enabled", True):
        return
    try:
        st = _state.get(analysis_symbol)
        if st and st.get("hedged"):
            _close_hedge(analysis_symbol)
        _blank(analysis_symbol)
    except Exception as e:
        LOG.warning(f"[feed_hedge] on_recovered error {analysis_symbol}: {e}")


def rehydrate(analysis_symbol: str, magics: list) -> None:
    """On startup / first poll, adopt an existing broker HEDGE_MAGIC position so a hedge that
    survived a server restart is still removed on recovery (never strand a hedge)."""
    try:
        if _state.get(analysis_symbol, {}).get("hedged"):
            return
        existing = _already_hedged_at_broker(magics)
        if not existing:
            return
        st = _blank(analysis_symbol)
        st.update(hedged=True, side=existing["side"], lots=existing["lots"],
                  opened_ts=int(time.time()), retry_count=0)
        LOG.warning(f"[feed_hedge] {analysis_symbol} rehydrated existing hedge "
                    f"{existing['side']} {existing['lots']} from broker (post-restart)")
    except Exception as e:
        LOG.debug(f"[feed_hedge] rehydrate skipped {analysis_symbol}: {e}")


def _open_hedge(analysis_symbol: str, mon: dict) -> None:
    from execution.exec_bridge import ExecBridge, HEDGE_MAGIC
    stash = ExecBridge.get_last_magics(analysis_symbol)
    if not stash:
        LOG.warning(f"[feed_hedge] {analysis_symbol} feed down but no poll data — cannot hedge")
        return
    account, broker_symbol = stash["account"], stash["broker_symbol"]
    magics = stash["magics"]
    net = _net_exposure(magics)
    min_lots = float(mon.get("feed_hedge_min_lots", 0.01) or 0.01)
    if abs(net) < min_lots:
        LOG.info(f"[feed_hedge] {analysis_symbol} feed down but net exposure {net:+.2f} "
                 f"< {min_lots} — nothing to hedge")
        # mark hedged=True with 0 lots? No — leave unhedged so recovery is a no-op. But set a
        # sentinel so we don't re-evaluate every pass; use hedged flag with lots 0.
        st = _state[analysis_symbol]
        st.update(hedged=True, side="", lots=0.0, price=0.0, opened_ts=int(time.time()))
        return
    side = "sell" if net > 0 else "buy"   # opposite of net directional exposure
    lot = round(abs(net), 2)
    q = ExecBridge.get_quote(account, broker_symbol) or {}
    price = float(q.get("mid", 0.0) or 0.0)
    ExecBridge.enqueue(account, "OPEN_MARKET", broker_symbol, side=side, lot=lot,
                       magic=HEDGE_MAGIC, comment="feed_hedge")
    st = _state[analysis_symbol]
    st.update(hedged=True, side=side, lots=lot, price=price, opened_ts=int(time.time()))
    LOG.warning(f"[feed_hedge] {analysis_symbol} feed down {st['retry_count']}× — HEDGE "
                f"{side} {lot} @ ~{price} (net exposure {net:+.2f}, magic {HEDGE_MAGIC})")


def _close_hedge(analysis_symbol: str) -> None:
    from execution.exec_bridge import ExecBridge, HEDGE_MAGIC
    st = _state.get(analysis_symbol) or {}
    if st.get("lots", 0.0) <= 0:
        LOG.info(f"[feed_hedge] {analysis_symbol} feed recovered — no lots to remove")
        return
    stash = ExecBridge.get_last_magics(analysis_symbol)
    if not stash:
        LOG.warning(f"[feed_hedge] {analysis_symbol} recovered but no poll data — cannot "
                    f"remove hedge (will retry on next recovery pass)")
        return
    account, broker_symbol = stash["account"], stash["broker_symbol"]
    ExecBridge.enqueue(account, "CLOSE_ALL", broker_symbol, magic=HEDGE_MAGIC,
                       comment="feed_hedge_remove")
    LOG.warning(f"[feed_hedge] {analysis_symbol} feed recovered — hedge removed "
                f"({st.get('side')} {st.get('lots')}, magic {HEDGE_MAGIC})")
