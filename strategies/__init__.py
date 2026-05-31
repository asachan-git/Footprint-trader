"""Multi-strategy framework.

A *strategy* is a self-contained directional signal + execution policy. The
StrategyManager deploys many of them side by side on the same live data feed.

Design split (per user requirement):
  - COMMON  : market data (footprint bars, VP), the shared logs directory, and a
              global decisions stream — every strategy reads the same world.
  - PER-STRATEGY : results. Each strategy owns its own positions / cycles /
              decisions / equity files under data/strategies/<name>/ so PnL,
              win-rate and equity curves never cross-contaminate.

Entry points:
  from strategies.manager import StrategyManager
  mgr = StrategyManager.from_settings(settings)
  mgr.tick(symbol, tf, bar, settings)        # call on each bar close
  mgr.results("democracy")                    # per-strategy stats + equity
"""

from .base import Strategy, StrategyContext, StrategyResult
from .manager import StrategyManager

__all__ = ["Strategy", "StrategyContext", "StrategyResult", "StrategyManager"]
