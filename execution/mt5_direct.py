"""
Direct-MT5 execution adapter (mac side).

Connects to the wine-python rpyc server (execution/mt5_bridge/server.py, launched by
scripts/mt5_server.sh) over localhost and exposes a clean, JSON-able API for the rest
of the stack. This is the replacement for the EA WebRequest bridge: it can do what the
EA cannot — modify an open position's SL/TP, partial-close, modify pendings — so both
the grid family and the directional ict_fvg strategy can run off ONE adapter.

The server keeps all MT5 constants / request-dict construction; this module is a thin
typed facade with lazy connect + one transparent reconnect on a dropped link.

DEMO ONLY for now — the grid family is -EV and ict_fvg is unvalidated.
"""
from __future__ import annotations

import threading
from typing import Any

try:
    import rpyc
except ImportError:  # pragma: no cover - rpyc is a hard dep when this adapter is used
    rpyc = None

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18812


class MT5Direct:
    """Localhost rpyc client to the wine-side MT5Service. Thread-safe (single lock)."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: float = 10.0):
        if rpyc is None:
            raise RuntimeError("rpyc not installed in the mac venv — `pip install rpyc`")
        self._host = host
        self._port = port
        self._timeout = timeout
        self._conn = None
        self._lock = threading.Lock()

    # --- connection --------------------------------------------------------
    def _connect(self):
        self._conn = rpyc.connect(
            self._host, self._port,
            config={"sync_request_timeout": self._timeout},
        )
        return self._conn

    def _root(self):
        if self._conn is None:
            self._connect()
        return self._conn.root

    def _call(self, method: str, *args, **kwargs) -> Any:
        """Invoke a remote exposed_ method with one transparent reconnect."""
        with self._lock:
            try:
                return getattr(self._root(), method)(*args, **kwargs)
            except (EOFError, ConnectionError, AttributeError, OSError):
                # link dropped (server restart / wine hiccup) — reconnect once
                self._conn = None
                return getattr(self._root(), method)(*args, **kwargs)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    def healthy(self) -> bool:
        try:
            self._call("ping")
            return True
        except Exception:
            return False

    # --- read --------------------------------------------------------------
    def account_info(self) -> dict:
        return dict(self._call("account_info"))

    def terminal_info(self) -> dict:
        return dict(self._call("terminal_info"))

    def tick(self, symbol: str) -> dict:
        return dict(self._call("tick", symbol))

    def symbol_info(self, symbol: str) -> dict:
        return dict(self._call("symbol_info", symbol))

    def symbols_get(self, group: str = "") -> list[dict]:
        return [dict(s) for s in self._call("symbols_get", group)]

    def positions(self, symbol: str = "", magic: int = 0) -> list[dict]:
        return [dict(p) for p in self._call("positions", symbol, magic)]

    def pendings(self, symbol: str = "", magic: int = 0) -> list[dict]:
        return [dict(o) for o in self._call("pendings", symbol, magic)]

    # --- write -------------------------------------------------------------
    def place_pending(self, symbol: str, side: str, otype: str, price: float, lot: float,
                      sl: float = 0.0, tp: float = 0.0, magic: int = 0, comment: str = "",
                      deviation: int = 20) -> dict:
        return dict(self._call("place_pending", symbol, side, otype, price, lot,
                               sl, tp, magic, comment, deviation))

    def market_order(self, symbol: str, side: str, lot: float, sl: float = 0.0,
                     tp: float = 0.0, magic: int = 0, comment: str = "",
                     deviation: int = 20) -> dict:
        return dict(self._call("market_order", symbol, side, lot, sl, tp, magic,
                               comment, deviation))

    def modify_sltp(self, ticket: int, sl: float = 0.0, tp: float = 0.0) -> dict:
        return dict(self._call("modify_sltp", ticket, sl, tp))

    def modify_pending(self, ticket: int, price: float = 0.0, sl: float = 0.0,
                       tp: float = 0.0) -> dict:
        return dict(self._call("modify_pending", ticket, price, sl, tp))

    def partial_close(self, ticket: int, volume: float, deviation: int = 20) -> dict:
        return dict(self._call("partial_close", ticket, volume, deviation))

    def close_position(self, ticket: int, deviation: int = 20) -> dict:
        return dict(self._call("close_position", ticket, deviation))

    def cancel_pending(self, ticket: int) -> dict:
        return dict(self._call("cancel_pending", ticket))

    def close_all(self, symbol: str = "", magic: int = 0) -> dict:
        return dict(self._call("close_all", symbol, magic))

    def cancel_pendings(self, symbol: str = "", magic: int = 0) -> dict:
        return dict(self._call("cancel_pendings", symbol, magic))


# Module-level singleton (lazy) for the running server process.
_client: MT5Direct | None = None
_client_lock = threading.Lock()


def get_client(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> MT5Direct:
    global _client
    with _client_lock:
        if _client is None:
            _client = MT5Direct(host, port)
        return _client


if __name__ == "__main__":
    # Smoke test against a running wine server.
    c = MT5Direct()
    print("ping/healthy:", c.healthy())
    print("account:", c.account_info())
    print("terminal:", c.terminal_info())
    print("tick XAUUSD+:", c.tick("XAUUSD+"))
    print("symbol XAUUSD+:", c.symbol_info("XAUUSD+"))
    print("positions:", c.positions("XAUUSD+", 770001))
    print("pendings:", c.pendings("XAUUSD+", 770001))
