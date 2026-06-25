"""
TOP 20 — a modular paper-trading research platform (PAPER ONLY).

Phase 10 architecture: each concern is an isolated, swappable module.

    signals (services.py)             produce PaperSignal rows
        |
    strategies.py   --- entry logic (WalletSelector / SignalFilter / MarketFilter)
    probability.py  --- weighted statistical P(win) estimator  [ML-swappable]
    sizing.py       --- fractional-Kelly + variants, hard caps  (risk mgmt)
    exits.py        --- exit policies (hold / TP-SL / time / mirror)
    explain.py      --- per-trade + per-signal explanations
    analytics.py    --- Sharpe / Sortino / drawdown / expectancy ... (Phase 1)
    leaderboard.py  --- weighted risk-adjusted ranking + reasons  (Phase 7)
        |
    engine.py       --- the only module that touches the DB; wires it together

The public surface used by services.py / main.py is re-exported here so callers
import `from . import top20` and call top20.run_cycle(...), etc.
"""
from __future__ import annotations

from sqlalchemy import func  # re-exported for tests/back-compat

from . import analytics, exits, leaderboard, probability, sizing, strategies
from .engine import (
    ensure_strategies,
    evaluate_signals,
    explain_signal,
    forward_test,
    leaderboard as leaderboard_view,
    list_strategies,
    list_trades,
    portfolio,
    reset_paper,
    run_cycle,
    settle_and_mark,
    snapshot,
    strategy_detail,
    wallet_profile,
)
from .probability import ProbFeatures
from .probability import estimate as estimate_probability
from .sizing import SizingPolicy, SizingResult
from .sizing import size as size_position
from .strategies import STRATEGIES, CONFIG_BY_KEY

__all__ = [
    "analytics", "exits", "leaderboard", "probability", "sizing", "strategies",
    "ensure_strategies", "evaluate_signals", "settle_and_mark", "snapshot",
    "run_cycle", "list_strategies", "strategy_detail", "list_trades",
    "leaderboard_view", "portfolio", "wallet_profile", "explain_signal",
    "forward_test", "reset_paper", "estimate_probability", "ProbFeatures",
    "SizingPolicy", "SizingResult", "size_position", "STRATEGIES", "CONFIG_BY_KEY",
    "func",
]
