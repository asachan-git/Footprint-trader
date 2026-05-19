"""Post-replay: label outcomes for all decisions captured in the current state_store.

Run AFTER a replay scrub session finishes:
    python scripts/run_replay.py

Reads data/decisions.jsonl, labels each non-flat decision against forward bars
in the in-memory state_store, appends to data/outcomes.jsonl, then builds the
training dataset.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backtest.outcome_labeler import label_all  # noqa: E402
from backtest.dataset_builder import build       # noqa: E402


def main() -> None:
    n_labeled = label_all()
    print(f"labeled {n_labeled} outcomes")
    out = ROOT / "data" / "datasets" / "train_v1.jsonl"
    n_rows = build(out)
    print(f"wrote {n_rows} dataset rows → {out}")


if __name__ == "__main__":
    main()
