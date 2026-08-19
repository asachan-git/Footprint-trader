"""grid_scorecard: the arithmetic the merge gate depends on."""
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "grid_scorecard.py"
FIXTURE = ROOT / "tests" / "fixtures" / "grids_sample.csv"


def _run(*args):
    p = subprocess.run([sys.executable, str(SCRIPT), *args],
                       capture_output=True, text=True, cwd=ROOT)
    return p.returncode, p.stdout


def test_shape_classification_and_rates():
    """3 filled grids, 2 one-sided → 66.7%. The no-fill grid must not count:
    a cancelled grid took no risk and says nothing about the level."""
    _, out = _run("--broker", str(FIXTURE), "--json")
    o = json.loads(out)["overall"]
    assert o["grids"] == 4
    assert o["no_fill"] == 1
    assert o["one_sided"] == 2
    assert o["both"] == 1
    assert abs(o["one_sided_pct"] - 66.666) < 0.01


def test_usc_per_lot_uses_total_volume():
    """net / (lots_b + lots_s), not net / grids."""
    _, out = _run("--broker", str(FIXTURE), "--json")
    o = json.loads(out)["overall"]
    assert abs(o["lots"] - 4.5) < 1e-9
    assert abs(o["net"] - 2022.25) < 1e-9
    assert abs(o["usc_per_lot"] - 2022.25 / 4.5) < 1e-6


def test_opposing_depth_counts_only_filled_grids():
    _, out = _run("--broker", str(FIXTURE), "--json")
    depth = json.loads(out)["overall"]["opp_depth"]
    # keys arrive as strings through the JSON round-trip
    assert {int(k): v for k, v in depth.items()} == {0: 2, 1: 1}


def test_gate_fails_below_threshold_and_sets_exit_status():
    """66.7% one-sided passes, but +449/lot vs a 10 floor also passes, so this
    fixture is a PASS; the exit status must be 0. The negative control is
    covered by the Aug-3 reference in scripts/baselines/grid_baseline.json."""
    rc, out = _run("--broker", str(FIXTURE))
    assert "one-sided rate" in out
    assert rc == 0


def test_baseline_file_is_loadable_and_has_both_reference_runs():
    ref = json.loads((ROOT / "scripts" / "baselines" / "grid_baseline.json").read_text())
    runs = ref["runs"]
    assert "2026-06-22..26" in runs
    assert runs["2026-06-22..26"]["one_sided_pct"] > ref["_thresholds"]["min_one_sided_pct"]
    assert runs["2026-08-03 final-v1"]["one_sided_pct"] < ref["_thresholds"]["min_one_sided_pct"]
