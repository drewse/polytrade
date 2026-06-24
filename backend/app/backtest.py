"""
Historical backtest engine.

Replays historical trades in timestamp order and simulates several strategies
side by side, so you can compare "would copying these wallets have made money?"
against fade/whale/random/no-trade baselines on the SAME data.

Design choices (assumptions are surfaced to the user in the report & README):

  * Walk-forward, no lookahead: wallet classifications come from a *training*
    window (the first `train_fraction` of history). Only trades in the later
    *test* window are replayed/traded. So a wallet is judged "sharp" using only
    information available before the trade we copy.
  * Flat staking: every bet risks a fixed `max_position_pct` of the *starting*
    bankroll (no compounding). This keeps strategy comparison interpretable and
    avoids path-dependence / ruin artifacts.
  * Hold to resolution: a copied position is entered at the trade's price (plus
    liquidity-scaled slippage) and exits at the market's resolution (worth $1 if
    the chosen outcome wins, else $0), realized at `resolved_at`.
  * Only resolved markets are scored: an unresolved market has no known outcome,
    so trades on it are skipped (counted as "unscored", not as wins/losses).

The core `replay()` is pure (operates on plain objects) so it is easy to unit
test; `services.run_backtest()` wires it to the database.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime

from . import paper_trading as pt

STRATEGIES = [
    "copy_sharp_wallets",
    "fade_losing_wallets",
    "whale_shock_reversion",
    "random_baseline",
    "no_trade_baseline",
]


@dataclass
class BacktestParams:
    starting_bankroll: float = 10_000.0
    max_position_pct: float = 1.0
    slippage_cents: float = 1.5
    whale_size: float = 6_000.0
    random_prob: float = 0.15
    min_wallet_score: float = 65.0
    rng_seed: int = 7
    strategies: list[str] = field(default_factory=lambda: list(STRATEGIES))


@dataclass
class SimTrade:
    strategy: str
    wallet_id: int | None
    market_id: str
    category: str | None
    outcome: str
    side: str
    size: float
    entry_price: float
    exit_price: float
    pnl: float
    return_pct: float
    opened_at: datetime
    closed_at: datetime
    reason: str


@dataclass
class StrategyResult:
    strategy: str
    starting_bankroll: float
    ending_bankroll: float
    total_pnl: float
    roi: float
    max_drawdown: float
    win_rate: float
    num_trades: int
    avg_trade_return: float
    best_trade: float
    worst_trade: float
    equity_curve: list[dict]
    trades: list[SimTrade]


def _naive(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def split_by_time(trades: list, train_fraction: float) -> tuple[list, list]:
    """Order by timestamp, split into (train, test) at the time quantile."""
    ordered = sorted(trades, key=lambda t: _naive(t.timestamp))
    if not ordered:
        return [], []
    cut = max(1, int(len(ordered) * train_fraction))
    return ordered[:cut], ordered[cut:]


def other_outcome(outcomes: list[str], outcome: str) -> str | None:
    """The opposite side of a binary market (None if not exactly 2 outcomes)."""
    if not outcomes or len(outcomes) != 2:
        return None
    return next((o for o in outcomes if o != outcome), None)


def _decide(strategy: str, trade, market, wallet_class: dict, wallet_score: dict,
            params: BacktestParams, rng: random.Random):
    """Return (chosen_outcome, entry_price_estimate, reason) or None to skip."""
    cls = wallet_class.get(trade.wallet_id, "insufficient_data")
    score = wallet_score.get(trade.wallet_id, 0.0)

    if strategy == "no_trade_baseline":
        return None

    if strategy == "random_baseline":
        if rng.random() > params.random_prob:
            return None
        outcome = rng.choice(market.outcomes) if market.outcomes else trade.outcome
        price = trade.price if outcome == trade.outcome else round(1 - trade.price, 3)
        return outcome, price, "random entry"

    if strategy == "copy_sharp_wallets":
        if cls != "sharp" or score < params.min_wallet_score:
            return None
        return trade.outcome, trade.price, f"copy sharp wallet (score {score:.0f})"

    if strategy == "fade_losing_wallets":
        if cls != "bad":
            return None
        opp = other_outcome(market.outcomes, trade.outcome)
        if opp is None:
            return None
        return opp, round(1 - trade.price, 3), f"fade losing wallet (score {score:.0f})"

    if strategy == "whale_shock_reversion":
        if trade.size < params.whale_size:
            return None
        opp = other_outcome(market.outcomes, trade.outcome)
        if opp is None:
            return None
        return opp, round(1 - trade.price, 3), f"reversion vs ${trade.size:,.0f} whale"

    return None


def _metrics(strategy: str, trades: list[SimTrade], starting: float) -> StrategyResult:
    total_pnl = round(sum(t.pnl for t in trades), 2)
    ending = round(starting + total_pnl, 2)
    n = len(trades)
    wins = sum(1 for t in trades if t.pnl > 0)
    win_rate = round(wins / n, 4) if n else 0.0
    avg_ret = round(sum(t.return_pct for t in trades) / n, 4) if n else 0.0
    best = round(max((t.pnl for t in trades), default=0.0), 2)
    worst = round(min((t.pnl for t in trades), default=0.0), 2)

    # Equity curve over *close* times (realized PnL accrues at resolution).
    curve: list[dict] = [{"t": None, "equity": round(starting, 2)}]
    equity = starting
    peak = starting
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.closed_at):
        equity += t.pnl
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
        curve.append({"t": t.closed_at.isoformat(), "equity": round(equity, 2)})

    return StrategyResult(
        strategy=strategy,
        starting_bankroll=round(starting, 2),
        ending_bankroll=ending,
        total_pnl=total_pnl,
        roi=round(total_pnl / starting, 4) if starting else 0.0,
        max_drawdown=round(max_dd, 4),
        win_rate=win_rate,
        num_trades=n,
        avg_trade_return=avg_ret,
        best_trade=best,
        worst_trade=worst,
        equity_curve=curve,
        trades=trades,
    )


def replay(test_trades: list, markets: dict, wallet_class: dict, wallet_score: dict,
           params: BacktestParams) -> dict[str, StrategyResult]:
    """Run every strategy over the test-window trades. Pure function.

    `markets` maps market_id -> object with .resolved, .resolved_outcome,
    .resolved_at, .liquidity, .outcomes. `test_trades` are objects with
    .wallet_id, .market_id, .category, .outcome, .side, .price, .size, .timestamp.
    """
    ordered = sorted(test_trades, key=lambda t: _naive(t.timestamp))
    results: dict[str, StrategyResult] = {}
    base_size = params.starting_bankroll * (params.max_position_pct / 100.0)

    for strategy in params.strategies:
        rng = random.Random(params.rng_seed)
        sims: list[SimTrade] = []
        for trade in ordered:
            market = markets.get(trade.market_id)
            if market is None:
                continue
            decision = _decide(strategy, trade, market, wallet_class, wallet_score, params, rng)
            if decision is None:
                continue
            # Need a known outcome to score the trade.
            if not market.resolved or not market.resolved_outcome:
                continue
            closed_at = _naive(market.resolved_at)
            opened_at = _naive(trade.timestamp)
            if closed_at is None or opened_at is None or closed_at <= opened_at:
                continue

            outcome, raw_price, reason = decision
            slip = pt.effective_slippage_cents(params.slippage_cents, market.liquidity)
            entry_price = pt.apply_slippage(max(0.02, min(0.98, raw_price)), "buy", slip)
            shares = base_size / max(entry_price, 1e-6)
            exit_value = 1.0 if outcome == market.resolved_outcome else 0.0
            pnl = round(shares * exit_value - base_size, 2)
            ret = round((exit_value - entry_price) / entry_price, 4)
            sims.append(
                SimTrade(
                    strategy=strategy, wallet_id=trade.wallet_id, market_id=trade.market_id,
                    category=trade.category, outcome=outcome, side="buy", size=round(base_size, 2),
                    entry_price=entry_price, exit_price=exit_value, pnl=pnl, return_pct=ret,
                    opened_at=opened_at, closed_at=closed_at, reason=reason,
                )
            )
        results[strategy] = _metrics(strategy, sims, params.starting_bankroll)
    return results
