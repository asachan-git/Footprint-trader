"""Dhan market feed WebSocket client.

Subscribes to live quote or full DOM data using dhanhq.marketfeed.MarketFeed.
Calls on_tick(ts_ms, ltp, volume) for Quote packets.
Calls on_snapshot(full_packet) for Full (DOM) packets.

MarketFeed v2 API (dhanhq >= 2.0):
  MarketFeed(dhan_context, instruments, version='v2', on_message=callback)
  instruments: list of (exchange_segment, security_id, packet_type)
  exchange_segment: MarketFeed.IDX=0, MarketFeed.NSE=1, MarketFeed.NSE_FNO=2
  packet_type:      MarketFeed.Ticker=15, MarketFeed.Quote=17, MarketFeed.Full=21

Full packet contains: LTP, LTQ, volume, open/high/low/close, OI, depth[5 levels]
Each depth level: bid_price, bid_quantity, ask_price, ask_quantity
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable

from dhan.instruments import Instrument

LOG = logging.getLogger(__name__)

# Segment map: Instrument.under_exchange_segment → MarketFeed segment int
_SEGMENT_MAP = {
    "IDX_I": 0,    # MarketFeed.IDX
    "NSE_EQ": 1,   # MarketFeed.NSE
    "NSE_FNO": 2,  # MarketFeed.NSE_FNO
}


def _make_dhan_context():
    """Return DhanContext for MarketFeed (not the dhanhq wrapper)."""
    import os
    from dotenv import load_dotenv
    from pathlib import Path
    from dhanhq import DhanContext
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    return DhanContext(os.environ["DHAN_CLIENT_ID"], os.environ["DHAN_ACCESS_TOKEN"])


class DhanWsClient:
    """Live feed for one underlying. Blocking — run in a dedicated thread.

    mode="quote": subscribes Quote (17) packets → calls on_tick(ts_ms, ltp, volume)
    mode="full":  subscribes Full (21) packets  → calls on_snapshot(full_packet_dict)
    """

    def __init__(
        self,
        instrument: Instrument,
        mode: str = "quote",  # "quote" | "full"
        on_tick: Callable[[int, float, float], None] | None = None,
        on_snapshot: Callable[[dict], None] | None = None,
    ) -> None:
        if mode == "quote" and on_tick is None:
            raise ValueError("on_tick required for mode='quote'")
        if mode == "full" and on_snapshot is None:
            raise ValueError("on_snapshot required for mode='full'")
        self._instrument = instrument
        self._mode = mode
        self._on_tick = on_tick
        self._on_snapshot = on_snapshot

    def _handle_message(self, data: dict) -> None:
        msg_type = data.get("type", "")
        try:
            if self._mode == "quote" and "Quote" in msg_type:
                ltp = float(data.get("LTP") or 0)
                volume = float(data.get("volume") or 0)
                ts_ms = int(time.time() * 1000)
                if ltp > 0 and self._on_tick:
                    self._on_tick(ts_ms, ltp, volume)

            elif self._mode == "full" and "Full" in msg_type:
                if self._on_snapshot:
                    self._on_snapshot(data)
        except Exception as e:
            LOG.warning(f"[dhan_ws] message handler error: {e} type={msg_type!r}")

    def start(self) -> None:
        """Start blocking feed loop. Must be called from a dedicated thread."""
        try:
            from dhanhq.marketfeed import MarketFeed
        except ImportError as e:
            raise RuntimeError("dhanhq not installed. Run: pip install dhanhq") from e

        # Force IPv4: Dhan's api-feed.dhan.co has unreachable IPv6 addresses that
        # exhaust websockets' open_timeout before the library tries IPv4.
        import socket as _socket
        _orig_getaddrinfo = _socket.getaddrinfo
        def _ipv4_getaddrinfo(host: str, port: int, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0):  # type: ignore[override]
            results = _orig_getaddrinfo(host, port, family, type, proto, flags)
            ipv4 = [r for r in results if r[0] == _socket.AF_INET]
            return ipv4 if ipv4 else results
        _socket.getaddrinfo = _ipv4_getaddrinfo

        dhan_ctx = _make_dhan_context()
        seg = _SEGMENT_MAP.get(self._instrument.under_exchange_segment, 0)
        packet_type = MarketFeed.Full if self._mode == "full" else MarketFeed.Quote

        instruments = [(seg, self._instrument.under_security_id, packet_type)]

        LOG.info(
            f"[dhan_ws] {self._mode} feed: {self._instrument.symbol} "
            f"seg={seg} id={self._instrument.under_security_id} "
            f"packet={packet_type}"
        )

        import warnings
        feed = MarketFeed(
            dhan_ctx,
            instruments,
            version="v2",
            on_message=lambda _feed, data: self._handle_message(data),
        )

        _backoff = 5.0
        while True:
            try:
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", category=DeprecationWarning, module="dhanhq")
                    feed.run()  # run_forever() only connects; run() has the recv loop
                break  # clean exit
            except Exception as exc:
                LOG.warning(f"[dhan_ws] feed error: {exc}; retry in {_backoff:.0f}s")
                time.sleep(_backoff)
                _backoff = min(_backoff * 2, 120.0)
                # Rebuild feed + fresh context — stale DhanContext may hold poisoned state
                dhan_ctx = _make_dhan_context()
                feed = MarketFeed(
                    dhan_ctx,
                    instruments,
                    version="v2",
                    on_message=lambda _feed, data: self._handle_message(data),
                )
