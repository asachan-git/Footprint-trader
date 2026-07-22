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
