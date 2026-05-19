"""A/B compare two prompt versions on a fixed dataset.

For each row in the training dataset, run Claude with prompt vA, then prompt vB.
Score each decision against the recorded outcome. Report hit rate, expectancy,
max drawdown per version.

Run: python -m eval.prompt_ab --a v1 --b v2 --dataset data/datasets/train_v1.jsonl
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from llm.client import ClaudeClient, ClientConfig
from prompts.builder import cached_prefix as build_prefix

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Stats:
    n: int = 0
    wins: int = 0
    losses: int = 0
    flats: int = 0
    sum_r: float = 0.0
    max_dd: float = 0.0
    running_low: float = 0.0
    running: float = 0.0

    def update(self, decision_side: str, outcome_r: float) -> None:
        self.n += 1
        if decision_side == "flat":
            self.flats += 1
            return
        self.sum_r += outcome_r
        self.running += outcome_r
        if outcome_r > 0:
            self.wins += 1
        else:
            self.losses += 1
        if self.running < self.running_low:
            self.running_low = self.running
        dd = self.running - self.running_low
        if dd > self.max_dd:
            self.max_dd = dd


def _prefix_for_version(version: str) -> str:
    # Temporarily swap current.txt for this run
    sys_path = ROOT / "prompts" / "system" / f"{version}.txt"
    if not sys_path.exists():
        raise FileNotFoundError(sys_path)
    # Bypass current.txt indirection — just read directly.
    text = sys_path.read_text()
    fewshot_path = ROOT / "prompts" / "few_shot" / "examples.jsonl"
    fs = "\n\nExamples:\n" + "\n".join(fewshot_path.read_text().splitlines()[:4]) if fewshot_path.exists() else ""
    return text + fs


def run(dataset: Path, version_a: str, version_b: str) -> dict:
    client = ClaudeClient(ClientConfig())
    prefix_a = _prefix_for_version(version_a)
    prefix_b = _prefix_for_version(version_b)

    sa, sb = Stats(), Stats()
    with dataset.open() as fh:
        for line in fh:
            row = json.loads(line)
            suffix = json.dumps({"context": row.get("decision", {}).get("rationale", "")})
            outcome_r = row["outcome"]["realized_r"]
            da = client.decide(prefix_a, suffix)
            db = client.decide(prefix_b, suffix)
            sa.update(da.side, outcome_r)
            sb.update(db.side, outcome_r)
    return {
        version_a: sa.__dict__,
        version_b: sb.__dict__,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True)
    ap.add_argument("--b", required=True)
    ap.add_argument("--dataset", required=True)
    args = ap.parse_args()
    report = run(Path(args.dataset), args.a, args.b)
    out = ROOT / "eval" / "reports" / f"ab_{args.a}_vs_{args.b}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
