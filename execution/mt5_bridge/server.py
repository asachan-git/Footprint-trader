#!/usr/bin/env python3
"""
MT5 direct-execution RPC server — runs UNDER wine python, inside the MetaQuotes
wine prefix, alongside the running terminal64.exe.

Why this exists: the `MetaTrader5` python module is Windows-only and IPCs into a
*Windows* terminal process. On this Mac the terminal runs under wine + Rosetta 2,
so the module can only import in a Windows python that lives in the same wine
prefix. The mac-side stack (homebrew python venv) cannot import it. So we run the
module here under wine and expose a thin rpyc service the mac venv connects to as
a localhost client (see execution/mt5_direct.py).

Unlike the EA WebRequest bridge (stop-orders only, no SL-modify / partial / trail),
this gives full order control: position SLTP-modify, partial close, pending modify,
market orders. That is what lets the directional strategy (ict_fvg) and grid run
off ONE adapter.

All MT5 constant / request-dict construction stays SERVER-SIDE so the mac client
passes only plain scalars and gets back plain JSON-able dicts (no rpyc netrefs to
fumble). Bind localhost only — this is an unauthenticated remote-exec surface.

Run (via scripts/mt5_server.sh):
    wine64 C:\\python312\\python.exe Z:\\...\\execution\\mt5_bridge\\server.py [port]
"""
import sys
import time

import MetaTrader5 as mt5
import rpyc
from rpyc.utils.server import ThreadedServer

DEFAULT_PORT = 18812
TERMINAL_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"

# --- map our plain-string order kinds → MT5 order_type constants -------------
_PENDING_TYPE = {
    ("buy", "stop"):  mt5.ORDER_TYPE_BUY_STOP,
    ("sell", "stop"): mt5.ORDER_TYPE_SELL_STOP,
    ("buy", "limit"):  mt5.ORDER_TYPE_BUY_LIMIT,
    ("sell", "limit"): mt5.ORDER_TYPE_SELL_LIMIT,
}
_MARKET_TYPE = {"buy": mt5.ORDER_TYPE_BUY, "sell": mt5.ORDER_TYPE_SELL}


def _ensure() -> None:
    """(Re)attach to the terminal if the IPC link dropped."""
    if mt5.terminal_info() is None:
        if not mt5.initialize(TERMINAL_PATH):
            raise RuntimeError(f"mt5.initialize failed: {mt5.last_error()}")


def _result(r) -> dict:
    """Normalize an OrderSendResult / None into a plain dict."""
    if r is None:
        return {"ok": False, "retcode": None, "error": mt5.last_error(), "comment": "send returned None"}
    ok = r.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED,
                        mt5.TRADE_RETCODE_DONE_PARTIAL)
    return {
        "ok": bool(ok),
        "retcode": int(r.retcode),
        "deal": int(r.deal),
        "order": int(r.order),
        "volume": float(r.volume),
        "price": float(r.price),
        "comment": str(r.comment),
        "request_id": int(getattr(r, "request_id", 0) or 0),
    }


def _round_price(symbol: str, price: float) -> float:
    info = mt5.symbol_info(symbol)
    digits = info.digits if info else 5
    return round(float(price), digits)


class MT5Service(rpyc.Service):
    # rpyc default Service exposes only `exposed_`-prefixed members.

    def exposed_ping(self) -> float:
        _ensure()
        return time.time()

    # --- read --------------------------------------------------------------
    def exposed_account_info(self) -> dict:
        _ensure()
        a = mt5.account_info()
        if a is None:
            return {}
        return {"login": a.login, "server": a.server, "balance": a.balance,
                "equity": a.equity, "margin": a.margin, "margin_free": a.margin_free,
                "currency": a.currency, "leverage": a.leverage}

    def exposed_terminal_info(self) -> dict:
        _ensure()
        t = mt5.terminal_info()
        if t is None:
            return {}
        return {"connected": t.connected, "trade_allowed": t.trade_allowed,
                "build": t.build, "company": t.company}

    def exposed_tick(self, symbol: str) -> dict:
        _ensure()
        t = mt5.symbol_info_tick(symbol)
        if t is None:
            return {}
        return {"bid": t.bid, "ask": t.ask, "last": t.last, "time": t.time,
                "time_msc": t.time_msc}

    def exposed_symbol_info(self, symbol: str) -> dict:
        _ensure()
        s = mt5.symbol_info(symbol)
        if s is None:
            return {}
        return {"digits": s.digits, "point": s.point,
                "stops_level": s.trade_stops_level, "freeze_level": s.trade_freeze_level,
                "volume_min": s.volume_min, "volume_max": s.volume_max,
                "volume_step": s.volume_step, "bid": s.bid, "ask": s.ask,
                "visible": s.visible, "trade_mode": s.trade_mode}

    def exposed_symbols_get(self, group: str = "") -> list:
        """Return list of symbol names. group: e.g. '*XAU*' or '*BTC*'."""
        _ensure()
        syms = mt5.symbols_get(group) if group else mt5.symbols_get()
        if not syms:
            return []
        return [{"name": s.name, "visible": s.visible, "bid": s.bid, "ask": s.ask,
                 "digits": s.digits, "volume_min": s.volume_min} for s in syms]

    def exposed_positions(self, symbol: str = "", magic: int = 0) -> list:
        _ensure()
        ps = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        out = []
        for p in (ps or []):
            if magic and p.magic != magic:
                continue
            out.append({"ticket": p.ticket, "symbol": p.symbol,
                        "type": "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell",
                        "volume": p.volume, "price_open": p.price_open,
                        "sl": p.sl, "tp": p.tp, "price_current": p.price_current,
                        "profit": p.profit, "swap": p.swap, "magic": p.magic,
                        "comment": p.comment, "time": p.time})
        return out

    def exposed_pendings(self, symbol: str = "", magic: int = 0) -> list:
        _ensure()
        os_ = mt5.orders_get(symbol=symbol) if symbol else mt5.orders_get()
        type_name = {mt5.ORDER_TYPE_BUY_STOP: "buy_stop", mt5.ORDER_TYPE_SELL_STOP: "sell_stop",
                     mt5.ORDER_TYPE_BUY_LIMIT: "buy_limit", mt5.ORDER_TYPE_SELL_LIMIT: "sell_limit"}
        out = []
        for o in (os_ or []):
            if magic and o.magic != magic:
                continue
            out.append({"ticket": o.ticket, "symbol": o.symbol,
                        "type": type_name.get(o.type, str(o.type)),
                        "volume": o.volume_current, "price_open": o.price_open,
                        "sl": o.sl, "tp": o.tp, "magic": o.magic, "comment": o.comment})
        return out

    def exposed_history_deals(self, ts_from: float, ts_to: float,
                              symbol: str = "", magic: int = 0) -> list:
        """Realized closed-out deals in [ts_from, ts_to] (epoch seconds). Only
        DEAL_ENTRY_OUT / OUT_BY / INOUT rows carry realized P&L (profit+swap+commission);
        entry deals have profit 0. Filter by symbol and/or magic. This is the BROKER-SIDE
        ground truth (vs the server's exec_emit basket estimate). Returns plain dicts."""
        _ensure()
        deals = mt5.history_deals_get(int(ts_from), int(ts_to))
        if not deals:
            return []
        _OUT = (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY, mt5.DEAL_ENTRY_INOUT)
        out = []
        for d in deals:
            if symbol and d.symbol != symbol:
                continue
            if magic and d.magic != magic:
                continue
            if d.entry not in _OUT:
                continue   # entry deal — no realized P&L
            out.append({
                "ticket": int(d.ticket), "order": int(d.order),
                "position_id": int(getattr(d, "position_id", 0) or 0),
                "symbol": d.symbol, "magic": int(d.magic),
                "type": "buy" if d.type == mt5.DEAL_TYPE_BUY else "sell",
                "entry": int(d.entry), "volume": float(d.volume), "price": float(d.price),
                "profit": float(d.profit), "swap": float(d.swap),
                "commission": float(d.commission),
                "net": float(d.profit) + float(d.swap) + float(d.commission),
                "time": int(d.time), "comment": str(d.comment),
            })
        return out

    # --- write -------------------------------------------------------------
    def exposed_place_pending(self, symbol: str, side: str, otype: str, price: float,
                              lot: float, sl: float = 0.0, tp: float = 0.0,
                              magic: int = 0, comment: str = "", deviation: int = 20) -> dict:
        """side: 'buy'|'sell'; otype: 'stop'|'limit'."""
        _ensure()
        ot = _PENDING_TYPE.get((side, otype))
        if ot is None:
            return {"ok": False, "error": f"bad side/otype {side}/{otype}"}
        req = {"action": mt5.TRADE_ACTION_PENDING, "symbol": symbol,
               "volume": float(lot), "type": ot, "price": _round_price(symbol, price),
               "sl": _round_price(symbol, sl) if sl else 0.0,
               "tp": _round_price(symbol, tp) if tp else 0.0,
               "deviation": int(deviation), "magic": int(magic), "comment": comment,
               "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_RETURN}
        return _result(mt5.order_send(req))

    def exposed_market_order(self, symbol: str, side: str, lot: float, sl: float = 0.0,
                             tp: float = 0.0, magic: int = 0, comment: str = "",
                             deviation: int = 20) -> dict:
        _ensure()
        ot = _MARKET_TYPE.get(side)
        if ot is None:
            return {"ok": False, "error": f"bad side {side}"}
        tick = mt5.symbol_info_tick(symbol)
        price = tick.ask if side == "buy" else tick.bid
        req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(lot),
               "type": ot, "price": price,
               "sl": _round_price(symbol, sl) if sl else 0.0,
               "tp": _round_price(symbol, tp) if tp else 0.0,
               "deviation": int(deviation), "magic": int(magic), "comment": comment,
               "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC}
        return _result(mt5.order_send(req))

    def exposed_modify_sltp(self, ticket: int, sl: float = 0.0, tp: float = 0.0) -> dict:
        """Modify SL/TP of an OPEN position — the thing the EA bridge cannot do."""
        _ensure()
        pos = mt5.positions_get(ticket=ticket)
        if not pos:
            return {"ok": False, "error": f"position {ticket} not found"}
        sym = pos[0].symbol
        req = {"action": mt5.TRADE_ACTION_SLTP, "position": int(ticket), "symbol": sym,
               "sl": _round_price(sym, sl) if sl else 0.0,
               "tp": _round_price(sym, tp) if tp else 0.0}
        return _result(mt5.order_send(req))

    def exposed_modify_pending(self, ticket: int, price: float = 0.0,
                               sl: float = 0.0, tp: float = 0.0) -> dict:
        _ensure()
        od = mt5.orders_get(ticket=ticket)
        if not od:
            return {"ok": False, "error": f"order {ticket} not found"}
        sym = od[0].symbol
        req = {"action": mt5.TRADE_ACTION_MODIFY, "order": int(ticket),
               "price": _round_price(sym, price) if price else od[0].price_open,
               "sl": _round_price(sym, sl) if sl else 0.0,
               "tp": _round_price(sym, tp) if tp else 0.0,
               "type_time": mt5.ORDER_TIME_GTC}
        return _result(mt5.order_send(req))

    def exposed_partial_close(self, ticket: int, volume: float, deviation: int = 20) -> dict:
        """Close `volume` lots of an open position (partial)."""
        _ensure()
        pos = mt5.positions_get(ticket=ticket)
        if not pos:
            return {"ok": False, "error": f"position {ticket} not found"}
        p = pos[0]
        tick = mt5.symbol_info_tick(p.symbol)
        if p.type == mt5.POSITION_TYPE_BUY:
            close_type, price = mt5.ORDER_TYPE_SELL, tick.bid
        else:
            close_type, price = mt5.ORDER_TYPE_BUY, tick.ask
        req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol,
               "volume": float(volume), "type": close_type, "position": int(ticket),
               "price": price, "deviation": int(deviation), "magic": p.magic,
               "comment": "partial", "type_time": mt5.ORDER_TIME_GTC,
               "type_filling": mt5.ORDER_FILLING_IOC}
        return _result(mt5.order_send(req))

    def exposed_close_position(self, ticket: int, deviation: int = 20) -> dict:
        _ensure()
        pos = mt5.positions_get(ticket=ticket)
        if not pos:
            return {"ok": False, "error": f"position {ticket} not found"}
        return self.exposed_partial_close(ticket, pos[0].volume, deviation)

    def exposed_cancel_pending(self, ticket: int) -> dict:
        _ensure()
        return _result(mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": int(ticket)}))

    def exposed_close_all(self, symbol: str = "", magic: int = 0) -> dict:
        """Close all positions + cancel all pendings (this magic/symbol). Mirrors EA CLOSE_ALL."""
        _ensure()
        closed = cancelled = 0
        errs = []
        for o in self.exposed_pendings(symbol, magic):
            r = self.exposed_cancel_pending(o["ticket"])
            if r.get("ok"):
                cancelled += 1
            else:
                errs.append(f"cancel#{o['ticket']}:{r.get('retcode')}")
        for p in self.exposed_positions(symbol, magic):
            r = self.exposed_close_position(p["ticket"])
            if r.get("ok"):
                closed += 1
            else:
                errs.append(f"close#{p['ticket']}:{r.get('retcode')}")
        return {"ok": not errs, "closed": closed, "cancelled": cancelled, "errors": errs}

    def exposed_cancel_pendings(self, symbol: str = "", magic: int = 0) -> dict:
        """Cancel pendings only — leave positions (safe grid re-arm)."""
        _ensure()
        cancelled = 0
        errs = []
        for o in self.exposed_pendings(symbol, magic):
            r = self.exposed_cancel_pending(o["ticket"])
            if r.get("ok"):
                cancelled += 1
            else:
                errs.append(f"cancel#{o['ticket']}:{r.get('retcode')}")
        return {"ok": not errs, "cancelled": cancelled, "errors": errs}


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    if not mt5.initialize(TERMINAL_PATH):
        print(f"FATAL mt5.initialize failed: {mt5.last_error()}", flush=True)
        sys.exit(1)
    a = mt5.account_info()
    print(f"MT5 connected: {a.login} {a.server} bal {a.balance} {a.currency}", flush=True)
    print(f"MT5Service listening on 127.0.0.1:{port}", flush=True)
    ThreadedServer(
        MT5Service, hostname="127.0.0.1", port=port,
        protocol_config={"allow_public_attrs": True, "allow_all_attrs": True},
    ).start()


if __name__ == "__main__":
    main()
