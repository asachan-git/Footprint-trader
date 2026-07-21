"""Broker simulator for the grid backtest — models the BROKER, not the strategy.

This is the only genuinely new logic in the harness, and its scope is deliberately
narrow. It never decides anything: it receives the commands the REAL server emits
(PLACE_PENDING / CANCEL_PENDINGS / CLOSE_ALL / CLOSE_SIDE / MOVE_BE /
MODIFY_PENDING / MODIFY_POSITION) and resolves them against historical bars, then
reports open state back so the server's own cycle machine sees a live account.

Fill semantics are inherited from scripts/grid_levels_sim.py._label, which already
learned these the hard way:

  * WRONG-SIDE STOPS ARE REJECTED. A BuyStop must sit above the market and a
    SellStop below; MT5 rejects otherwise. Accepting them produced fake instant-fill
    wins in the earlier sim. The EA enforces the same thing (ExecPlacePending's
    freeze guard), so a rejected leg here mirrors a rejected leg live.
  * A LEG CANNOT FILL AND HIT ITS TP IN THE SAME BAR. One candle touching both the
    stop and the target is path-ambiguous; resolving it optimistically is how a
    backtest invents profit.
  * AMBIGUOUS WHIPSAW TAKES THE WORSE SIDE. If a bar's range contains both a
    position's SL and its TP, the SL wins.

Intrabar path: callers should step 1m bars even when the cycle TF is higher — the
finer the path, the fewer ambiguous bars. Within a single bar the path is unknown,
so the pessimistic ordering above applies.

Phase 2a scope: fills, TP/SL, and open-state reporting — enough to make cycles turn
over so arm fidelity (G1-G4) can be measured. Spread/commission/swap/margin/stop-out
are Phase 2d and are NOT modelled here yet; P&L from this module is gross and must
not be reported as a result.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Pending:
    cmd_id: str
    magic: int
    symbol: str
    order_type: str          # buy_stop | sell_stop | buy_limit | sell_limit
    price: float
    lot: float
    sl: float = 0.0
    tp: float = 0.0
    comment: str = ""

    @property
    def is_buy(self) -> bool:
        return self.order_type.startswith("buy")


@dataclass
class Position:
    magic: int
    symbol: str
    is_buy: bool
    entry: float
    lot: float
    sl: float = 0.0
    tp: float = 0.0
    comment: str = ""
    opened_ts: int = 0

    def pnl_at(self, px: float) -> float:
        """Gross P&L in price-units × lot (no costs — see module docstring)."""
        return (px - self.entry) * self.lot if self.is_buy else (self.entry - px) * self.lot


@dataclass
class FillEvent:
    ts: int
    kind: str                # fill | tp | sl | close_all | close_side | cancel | reject
    magic: int
    price: float = 0.0
    lot: float = 0.0
    pnl: float = 0.0
    detail: str = ""


@dataclass
class Broker:
    """Simulated MT5 account for one symbol."""
    symbol: str
    stops_dist: float = 0.0          # broker freeze band; a stop inside it is rejected
    pendings: list[Pending] = field(default_factory=list)
    positions: list[Position] = field(default_factory=list)
    events: list[FillEvent] = field(default_factory=list)
    realized: float = 0.0

    # ── command intake ────────────────────────────────────────────────────────
    def apply_command(self, cmd: dict, mid: float, ts: int) -> dict:
        """Execute one server command. Returns an ack dict for /exec/ack."""
        ctype = cmd.get("type")
        cid = str(cmd.get("id") or "")
        magic = int(cmd.get("magic", 0) or 0)

        if ctype == "PLACE_PENDING":
            return self._place_pending(cmd, cid, magic, mid, ts)
        if ctype == "CANCEL_PENDINGS":
            side = str(cmd.get("side") or "")
            n = self._cancel_pendings(magic, side, ts)
            return {"id": cid, "ok": True, "cancelled": n}
        if ctype == "CLOSE_ALL":
            closed, cancelled = self._close_all(magic, mid, ts)
            return {"id": cid, "ok": True, "closed": closed, "cancelled": cancelled}
        if ctype == "CLOSE_SIDE":
            n = self._close_side(magic, str(cmd.get("side") or ""),
                                 float(cmd.get("frac") or 0.5), mid, ts)
            return {"id": cid, "ok": True, "closed": n}
        if ctype == "MOVE_BE":
            n = self._move_be(magic, str(cmd.get("side") or ""))
            return {"id": cid, "ok": True, "moved": n}
        if ctype == "MODIFY_PENDING":
            n = self._modify_pending(magic, str(cmd.get("side") or ""),
                                     float(cmd.get("price_delta") or 0.0),
                                     float(cmd.get("tp") or 0.0), mid)
            return {"id": cid, "ok": True, "modified": n}
        if ctype == "MODIFY_POSITION":
            n = self._modify_position(magic, str(cmd.get("side") or ""),
                                      float(cmd.get("sl") or 0.0),
                                      float(cmd.get("tp") or 0.0))
            return {"id": cid, "ok": True, "modified": n}
        return {"id": cid, "ok": False, "error": f"unknown type {ctype}"}

    def _place_pending(self, cmd: dict, cid: str, magic: int, mid: float, ts: int) -> dict:
        otype = str(cmd.get("order_type") or "")
        price = float(cmd.get("price") or 0.0)
        lot = float(cmd.get("lot") or 0.0)
        if price <= 0 or lot <= 0 or not otype:
            self.events.append(FillEvent(ts, "reject", magic, price, lot, detail="bad fields"))
            return {"id": cid, "ok": False, "error": "bad fields"}

        # Wrong-side / freeze-band rejection, mirroring ExecPlacePending's guard.
        band = self.stops_dist
        bad = (
            (otype == "buy_stop" and price < mid + band)
            or (otype == "sell_stop" and price > mid - band)
            or (otype == "buy_limit" and price > mid - band)
            or (otype == "sell_limit" and price < mid + band)
        )
        if bad:
            self.events.append(FillEvent(ts, "reject", magic, price, lot,
                                         detail=f"{otype} inside freeze/wrong side"))
            return {"id": cid, "ok": False, "error": f"{otype} inside freeze"}

        self.pendings.append(Pending(
            cmd_id=cid, magic=magic, symbol=str(cmd.get("symbol") or self.symbol),
            order_type=otype, price=price, lot=lot,
            sl=float(cmd.get("sl") or 0.0), tp=float(cmd.get("tp") or 0.0),
            comment=str(cmd.get("comment") or ""),
        ))
        return {"id": cid, "ok": True, "ticket": len(self.pendings), "retcode": 10009}

    def _cancel_pendings(self, magic: int, side: str, ts: int) -> int:
        keep, n = [], 0
        for p in self.pendings:
            if magic and p.magic != magic:
                keep.append(p); continue
            if side == "buy" and not p.is_buy:
                keep.append(p); continue
            if side == "sell" and p.is_buy:
                keep.append(p); continue
            n += 1
            self.events.append(FillEvent(ts, "cancel", p.magic, p.price, p.lot))
        self.pendings = keep
        return n

    def _close_all(self, magic: int, mid: float, ts: int) -> tuple[int, int]:
        cancelled = self._cancel_pendings(magic, "", ts)
        keep, closed = [], 0
        for pos in self.positions:
            if magic and pos.magic != magic:
                keep.append(pos); continue
            pnl = pos.pnl_at(mid)
            self.realized += pnl
            closed += 1
            self.events.append(FillEvent(ts, "close_all", pos.magic, mid, pos.lot, pnl))
        self.positions = keep
        return closed, cancelled

    def _close_side(self, magic: int, side: str, frac: float, mid: float, ts: int) -> int:
        want_buy = side == "buy"
        pool = [p for p in self.positions if p.magic == magic and p.is_buy == want_buy]
        if not pool:
            return 0
        # biggest lots booked first — matches ExecCloseSide's SortTicketsByVolumeDesc,
        # which leaves the smallest lot alive as the breakeven runner.
        pool.sort(key=lambda p: p.lot, reverse=True)
        n_close = max(1, int(-(-len(pool) * frac // 1)))   # ceil
        doomed = set(id(p) for p in pool[:n_close])
        keep, closed = [], 0
        for p in self.positions:
            if id(p) in doomed:
                pnl = p.pnl_at(mid)
                self.realized += pnl
                closed += 1
                self.events.append(FillEvent(ts, "close_side", p.magic, mid, p.lot, pnl))
            else:
                keep.append(p)
        self.positions = keep
        return closed

    def _move_be(self, magic: int, side: str) -> int:
        want_buy = side == "buy"
        n = 0
        for p in self.positions:
            if p.magic != magic or (side and p.is_buy != want_buy):
                continue
            # only ever tighten toward breakeven, never loosen (ExecMoveBE's `improve`)
            if p.is_buy and (p.sl <= 0 or p.entry > p.sl):
                p.sl = p.entry; n += 1
            elif not p.is_buy and (p.sl <= 0 or p.entry < p.sl):
                p.sl = p.entry; n += 1
        return n

    def _modify_pending(self, magic: int, side: str, delta: float, tp: float,
                        mid: float) -> int:
        n = 0
        for p in self.pendings:
            if magic and p.magic != magic:
                continue
            if side == "buy" and not p.is_buy:
                continue
            if side == "sell" and p.is_buy:
                continue
            new_price = p.price + delta
            # freeze guard on the NEW price, same as ExecModifyPending
            if p.order_type == "buy_stop" and new_price < mid + self.stops_dist:
                continue
            if p.order_type == "sell_stop" and new_price > mid - self.stops_dist:
                continue
            p.price = new_price
            if tp > 0:
                p.tp = tp
            n += 1
        return n

    def _modify_position(self, magic: int, side: str, sl: float, tp: float) -> int:
        want_buy = side == "buy"
        n = 0
        for p in self.positions:
            if p.magic != magic or (side and p.is_buy != want_buy):
                continue
            if sl > 0:
                p.sl = sl
            if tp > 0:
                p.tp = tp
            n += 1
        return n

    # ── bar stepping ──────────────────────────────────────────────────────────
    def step_bar(self, o: float, h: float, lo: float, c: float, ts: int) -> None:
        """Advance one bar: resolve exits on PRIOR positions, then apply new fills.

        Order matters and is deliberately pessimistic:
          1. SL/TP on positions opened BEFORE this bar. SL is checked first, so a bar
             straddling both resolves as the loss.
          2. Pending fills from this bar's range. A leg filled here is not eligible
             for its own TP until the next bar (see module docstring).
        """
        self._resolve_exits(h, lo, c, ts)
        self._apply_fills(h, lo, ts)

    def _resolve_exits(self, h: float, lo: float, c: float, ts: int) -> None:
        survivors = []
        for p in self.positions:
            hit_sl = p.sl > 0 and ((lo <= p.sl) if p.is_buy else (h >= p.sl))
            hit_tp = p.tp > 0 and ((h >= p.tp) if p.is_buy else (lo <= p.tp))
            # SL first: if one bar contains both, the intrabar path is unknown and the
            # honest assumption is the adverse one.
            if hit_sl:
                pnl = p.pnl_at(p.sl)
                self.realized += pnl
                self.events.append(FillEvent(ts, "sl", p.magic, p.sl, p.lot, pnl))
                continue
            if hit_tp:
                pnl = p.pnl_at(p.tp)
                self.realized += pnl
                self.events.append(FillEvent(ts, "tp", p.magic, p.tp, p.lot, pnl))
                continue
            survivors.append(p)
        self.positions = survivors

    def _apply_fills(self, h: float, lo: float, ts: int) -> None:
        still = []
        for p in self.pendings:
            if p.order_type == "buy_stop":
                filled = h >= p.price
            elif p.order_type == "sell_stop":
                filled = lo <= p.price
            elif p.order_type == "buy_limit":
                filled = lo <= p.price
            else:  # sell_limit
                filled = h >= p.price
            if not filled:
                still.append(p)
                continue
            self.positions.append(Position(
                magic=p.magic, symbol=p.symbol, is_buy=p.is_buy, entry=p.price,
                lot=p.lot, sl=p.sl, tp=p.tp, comment=p.comment, opened_ts=ts,
            ))
            self.events.append(FillEvent(ts, "fill", p.magic, p.price, p.lot))
        self.pendings = still

    # ── state reporting (what the EA would send in a poll) ────────────────────
    def magics_json(self, mid: float) -> list[dict]:
        """Per-magic open state, in the exact shape FBExecBridge.BuildMagicsJson emits."""
        agg: dict[int, dict] = {}

        def _row(m: int) -> dict:
            return agg.setdefault(m, {"magic": m, "buys": 0, "sells": 0, "pendings": 0,
                                      "pnl": 0.0, "buy_pnl": 0.0, "sell_pnl": 0.0,
                                      "buy_lots": 0.0, "sell_lots": 0.0,
                                      "buy_pendings": 0, "sell_pendings": 0})

        for p in self.positions:
            r = _row(p.magic)
            pnl = p.pnl_at(mid)
            if p.is_buy:
                r["buys"] += 1; r["buy_pnl"] += pnl; r["buy_lots"] += p.lot
            else:
                r["sells"] += 1; r["sell_pnl"] += pnl; r["sell_lots"] += p.lot
            r["pnl"] += pnl
        for p in self.pendings:
            r = _row(p.magic)
            r["pendings"] += 1
            if p.is_buy:
                r["buy_pendings"] += 1
            else:
                r["sell_pendings"] += 1
        return list(agg.values())

    def totals(self, mid: float) -> dict:
        buys = sum(1 for p in self.positions if p.is_buy)
        sells = len(self.positions) - buys
        return {"buys": buys, "sells": sells, "pendings": len(self.pendings),
                "pnl": round(sum(p.pnl_at(mid) for p in self.positions), 2)}

    def floating(self, mid: float) -> float:
        return sum(p.pnl_at(mid) for p in self.positions)
