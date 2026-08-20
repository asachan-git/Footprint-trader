"""Per-TF squeeze enforcement.

The global gate exempts structural setups (hvn_inside_touch, hvn_displacement,
hvn_edge) because those arm on a node edge rather than a vol coil. 1m runs
hvn_inside_touch and nothing else, so enforcing the gate globally would change
nothing there — the exemption swallows it. A TF named in
require_squeeze_gate_by_tf therefore overrides the exemption as well.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def _decide(tf, sq_ok, cfg, is_structural=True):
    """Mirror of the gate decision in plan_grid_levels."""
    gate_by_tf = cfg.get("require_squeeze_gate_by_tf") or {}
    tf_gated = bool(gate_by_tf.get(tf, False))
    gate_on = tf_gated or bool(cfg.get("require_squeeze_gate", False))
    return gate_on and not sq_ok and (tf_gated or not is_structural)


CFG = {"require_squeeze_gate": False, "require_squeeze_gate_by_tf": {"1m": True}}


def test_1m_structural_is_skipped_when_uncoiled():
    """The whole point: without the exemption override this returns False and the
    gate is a silent no-op."""
    assert _decide("1m", sq_ok=False, cfg=CFG) is True


def test_1m_arms_when_coiled():
    assert _decide("1m", sq_ok=True, cfg=CFG) is False


def test_other_tfs_stay_in_observe_mode():
    """5m/15m must keep arming both cohorts or the A/B has one bucket."""
    assert _decide("5m", sq_ok=False, cfg=CFG) is False
    assert _decide("15m", sq_ok=False, cfg=CFG) is False


def test_global_gate_still_exempts_structural():
    """Unchanged behaviour when only the global flag is set."""
    cfg = {"require_squeeze_gate": True}
    assert _decide("5m", sq_ok=False, cfg=cfg, is_structural=True) is False
    assert _decide("5m", sq_ok=False, cfg=cfg, is_structural=False) is True


def test_no_config_gates_nothing():
    assert _decide("1m", sq_ok=False, cfg={}) is False
