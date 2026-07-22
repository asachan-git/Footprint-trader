"""Bounded re-placement of transiently-rejected grid legs.

A failed PLACE_PENDING leg never becomes a fillable position, so a leg lost to a
transient broker/EA rejection permanently thins that cycle's ladder. Freeze rejects were
already retried; these tests pin the widened set and the retry bound.
"""

from execution.exec_bridge import (
    Command, _MAX_LEG_RETRIES, _is_retryable_place_error,
)


# ── which errors are worth another attempt ───────────────────────────────────

def test_transient_rejections_are_retryable():
    # EA client-side pre-check — never reaches the broker
    assert _is_retryable_place_error("sell_stop inside freeze")
    assert _is_retryable_place_error("buy_stop inside freeze")
    # MT5 10015 — stop landed on the wrong side of market (seen as the SECOND failure
    # on a leg whose freeze-retry fired after price had moved past it)
    assert _is_retryable_place_error("invalid price")
    # MT5 10016 — attached TP inside the broker's min stop distance
    assert _is_retryable_place_error("invalid stops")


def test_real_rejections_are_not_retried():
    # these never fix themselves; retrying would loop every poll
    assert not _is_retryable_place_error("no money")
    assert not _is_retryable_place_error("trade disabled")
    assert not _is_retryable_place_error("invalid volume")


def test_classifier_is_case_insensitive_and_null_safe():
    assert _is_retryable_place_error("Invalid Stops")
    assert not _is_retryable_place_error(None)
    assert not _is_retryable_place_error("")


# ── the bound ────────────────────────────────────────────────────────────────

def test_retry_bound_is_finite():
    # "invalid stops" can be structural (TP genuinely too close), so the retry must not
    # be unbounded or a doomed leg re-sends on every ~1Hz poll for the cycle's life.
    assert 0 < _MAX_LEG_RETRIES <= 5


def test_retry_count_rides_on_the_command_not_the_wire():
    # retry_n must survive ack->re-stash (else the bound never binds) but must NOT be
    # sent to the EA, whose parser takes a fixed field set.
    c = Command(id="x", account="a", type="PLACE_PENDING", symbol="S",
                order_type="buy_stop", price=100.0, lot=0.25, retry_n=2)
    assert c.retry_n == 2
    assert "retry_n" not in c.to_wire()


# ── root cause: TP landing on the same HVN edge the outer leg reaches ────────

def test_tp_is_floored_at_the_brokers_min_stop_distance():
    """The real fix for 'invalid stops'.

    A wide ladder's OUTER leg can run out to meet the very HVN edge its TP targets, so
    the TP cleared the leg by a hair and MT5 rejected the order (10016: TP inside the
    min stop distance from entry). Observed gaps: 0.0287 / 0.0572 / 0.0786 / 0.1249 /
    0.1462 / 0.1638 against a live stops_dist of 0.20 — every one an outermost leg.
    """
    from execution.grid_planner import _resolve_tps, Leg

    buys = [Leg(price=4155.7813, lot=0.25)]
    sells = [Leg(price=4140.0, lot=0.25)]
    gap = 0.30   # stops_dist 0.20 * the 1.5 margin the step already uses

    # unguarded: TP can sit flush against the outer leg -> the rejected geometry
    buy_tp, _ = _resolve_tps("__nozones__", 4148.0, buys, sells,
                             atr=0.0, tp_mult=2.0, min_gap=0.0)
    assert buy_tp - 4155.7813 < gap

    # guarded: TP is pushed clear of the broker's floor
    buy_tp, sell_tp = _resolve_tps("__nozones__", 4148.0, buys, sells,
                                   atr=0.0, tp_mult=2.0, min_gap=gap)
    assert buy_tp >= 4155.7813 + gap - 1e-9
    assert sell_tp <= 4140.0 - gap + 1e-9


def test_min_gap_defaults_to_zero_so_callers_are_unaffected():
    from execution.grid_planner import _resolve_tps, Leg
    buys = [Leg(price=110.0, lot=0.1)]
    sells = [Leg(price=90.0, lot=0.1)]
    buy_tp, sell_tp = _resolve_tps("__nozones__", 100.0, buys, sells, atr=5.0, tp_mult=2.0)
    assert buy_tp == 120.0    # 110 + 2*5, untouched
    assert sell_tp == 80.0    # 90  - 2*5
