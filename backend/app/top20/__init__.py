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

from . import (
    analytics,
    benchmark,
    ensembles,
    exits,
    leaderboard,
    market_intel,
    montecarlo,
    optimize,
    probability,
    replay,
    reputation,
    report,
    simulate,
    sizing,
    strategies,
)
from .engine import (
    ensemble_view,
    ensure_strategies,
    evaluate_signals,
    explain_signal,
    feature_vectors,
    forward_test,
    leaderboard as leaderboard_view,
    list_strategies,
    list_trades,
    market_intelligence,
    market_regimes,
    monte_carlo,
    optimize_param,
    portfolio,
    probability_benchmark,
    recommend_retirements,
    realistic_view,
    replay_backfill_markets,
    replay_backfill_wallets,
    replay_comparison,
    replay_reset,
    replay_reset_realistic,
    replay_run,
    replay_run_realistic,
    replay_status,
    research_report,
    reset_paper,
    run_cycle,
    set_status,
    settle_and_mark,
    snapshot,
    strategy_detail,
    strategy_drift,
    wallet_evolution,
    wallet_profile,
    walk_forward_param,
)
from .probability import ProbFeatures
from .probability import estimate as estimate_probability
from .sizing import SizingPolicy, SizingResult
from .sizing import size as size_position
from .strategies import STRATEGIES, CONFIG_BY_KEY

__all__ = [
    "analytics", "ensembles", "exits", "leaderboard", "market_intel", "montecarlo",
    "optimize", "probability", "reputation", "report", "simulate", "sizing", "strategies",
    "ensure_strategies", "evaluate_signals", "settle_and_mark", "snapshot",
    "run_cycle", "list_strategies", "strategy_detail", "list_trades",
    "leaderboard_view", "portfolio", "wallet_profile", "explain_signal",
    "forward_test", "reset_paper", "estimate_probability", "ProbFeatures",
    "SizingPolicy", "SizingResult", "size_position", "STRATEGIES", "CONFIG_BY_KEY",
    "ensemble_view", "feature_vectors", "market_intelligence", "monte_carlo",
    "optimize_param", "recommend_retirements", "research_report", "set_status",
    "walk_forward_param", "func", "benchmark", "replay",
    "probability_benchmark", "strategy_drift", "market_regimes", "wallet_evolution",
    "replay_status", "replay_backfill_markets", "replay_backfill_wallets",
    "replay_run", "replay_reset", "replay_run_realistic", "realistic_view",
    "replay_comparison", "replay_reset_realistic",
]
