"""
Historical replay simulator (Phase 12 backbone).

Replays a strategy CONFIG over labeled samples (signals whose market has
resolved) and reports metrics — the engine behind parameter optimization and
walk-forward. NO FUTURE INFORMATION: the resolved outcome is used ONLY to settle
P&L, never inside the entry decision. Pure (no DB). PAPER ONLY.

A `Sample` is a signal joined with its realized outcome + wallet/market features.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace

from . import analytics, probability
from .sizing import size as size_position
from .strategies import Ctx, Shared, StrategyDef, decide


@dataclass
class Sample:
    created_at: datetime
    market_id: str
    outcome: str
    resolved_outcome: str
    price: float
    confidence: float
    edge: float
    liquidity: float
    category: str
    win_rate: float
    sharpe: float
    roi: float
    copyability: float
    classification: str | None
    specialization: float
    recency: float
    num_settled: int
    wallet_id: int


def _ctx(s: Sample) -> Ctx:
    return Ctx(wallet_id=s.wallet_id, classification=s.classification, confidence=s.confidence,
               edge=s.edge, liquidity=s.liquidity, age_min=0.0, price=s.price, outcome=s.outcome,
               market_id=s.market_id, category=s.category, win_rate=s.win_rate, sharpe=s.sharpe,
               roi=s.roi, copyability=s.copyability, specialization=s.specialization,
               recency=s.recency, num_settled=s.num_settled)


def build_shared(samples: list[Sample]) -> Shared:
    """Shared cohort data from THIS sample window only (no look-ahead)."""
    edges = sorted(s.edge for s in samples)
    thr = edges[int(0.90 * (len(edges) - 1))] if edges else 0.0
    pair: dict[tuple, set] = {}
    for s in samples:
        pair.setdefault((s.market_id, s.outcome), set()).add(s.wallet_id)
    # rank wallets within this window by their (static) quality metrics
    def rank(key):
        items = sorted({s.wallet_id: getattr(s, key) for s in samples}.items(),
                       key=lambda kv: kv[1], reverse=True)
        return {wid: i for i, (wid, _) in enumerate(items)}
    return Shared(rank_copyability=rank("copyability"), rank_sharpe=rank("sharpe"),
                  rank_roi=rank("roi"), rank_active=rank("num_settled"),
                  edge_pct_threshold=thr, consensus={k for k, w in pair.items() if len(w) >= 2})


def run(config: StrategyDef, samples: list[Sample], starting_bankroll: float = 10_000.0,
        shared: Shared | None = None) -> dict:
    """Simulate `config` over `samples`; settle each taken trade at resolution."""
    if not samples:
        return {"n_taken": 0, "metrics": analytics.compute_metrics([], [], [starting_bankroll], starting_bankroll, 0, 0)}
    shared = shared or build_shared(samples)
    ordered = sorted(samples, key=lambda s: s.created_at)
    exposure: dict[str, float] = {}
    closed = []
    seen = 0
    cum = 0.0
    curve = [starting_bankroll]
    for s in ordered:
        seen += 1
        ctx = _ctx(s)
        admit, _ = decide(config, ctx, shared)
        if not admit:
            continue
        p = probability.estimate(probability.ProbFeatures(
            market_price=ctx.price, edge=ctx.edge, win_rate=ctx.win_rate, sharpe=ctx.sharpe,
            roi=ctx.roi, confidence=ctx.confidence, specialization=ctx.specialization,
            liquidity=ctx.liquidity, num_settled=ctx.num_settled))
        res = size_position(config.sizing, price=ctx.price, p=p, bankroll=starting_bankroll,
                            market_exposure_used=exposure.get(s.market_id, 0.0),
                            confidence=ctx.confidence, edge=ctx.edge, quality=ctx.copyability)
        if res.stake is None:
            continue
        exposure[s.market_id] = exposure.get(s.market_id, 0.0) + res.stake
        won = s.resolved_outcome == s.outcome
        realized = round(res.shares * (1.0 if won else 0.0) - res.stake, 2)
        cum += realized
        curve.append(starting_bankroll + cum)
        closed.append(SimpleNamespace(realized_pnl=realized, unrealized_pnl=0.0, stake=res.stake,
                                      kelly_fraction=res.kelly_fraction, entry_time=s.created_at,
                                      closed_at=s.created_at, status="closed"))
    metrics = analytics.compute_metrics(closed, [], curve, starting_bankroll, seen, len(closed),
                                        first_ts=ordered[0].created_at, last_ts=ordered[-1].created_at)
    return {"n_taken": len(closed), "n_seen": seen, "metrics": metrics}


# parameter application for the optimizer ------------------------------------
def base_config() -> StrategyDef:
    from .strategies import WalletSelector, SignalFilter, MarketFilter
    from .sizing import SizingPolicy
    return StrategyDef("opt", "Optimizer", "parameter sweep base", "tuning",
                       wallet=WalletSelector("any"), signal=SignalFilter("any"),
                       market=MarketFilter("any"), sizing=SizingPolicy(mode="kelly", kelly_multiplier=0.25))


def apply_param(base: StrategyDef, param: str, value) -> StrategyDef:
    from .strategies import SignalFilter, MarketFilter
    c = copy.deepcopy(base)
    if param == "confidence":
        c.signal = SignalFilter("confidence", float(value))
    elif param == "edge":
        c.signal = SignalFilter("edge", float(value))
    elif param == "kelly":
        c.sizing.kelly_multiplier = float(value)
    elif param == "liquidity":
        c.market = MarketFilter("liquidity_min", float(value))
    return c
