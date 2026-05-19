"""Thin CLI wrapper over eval.prompt_ab."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval.prompt_ab import main  # noqa: E402

if __name__ == "__main__":
    main()
