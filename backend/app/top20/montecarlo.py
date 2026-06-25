"""
Monte Carlo risk analysis (Phase 14).

Bootstrap-resamples a strategy's realized per-trade P&L to build thousands of
randomized equity paths, then estimates ruin / drawdown / return distributions
with confidence intervals. Pure + SEEDED, so results are reproducible (same
seed + inputs => identical output). PAPER ONLY — analysis of simulated P&L.
"""
from __future__ import annotations

import random


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def simulate(pnls: list[float], starting_bankroll: float = 10_000.0,
             sims: int = 2000, seed: int = 42, ruin_fraction: float = 0.5) -> dict:
    """Bootstrap `sims` equity paths from observed per-trade P&L.

    ruin = equity falls to <= ruin_fraction of the starting bankroll at any point.
    Returns probability of ruin, expected / 95% drawdown, median final return /
    CAGR-style growth, and a return distribution with a 90% confidence interval.
    """
    n = len(pnls)
    if n == 0:
        return {"insufficient_data": True, "sims": 0, "n_trades": 0}

    rng = random.Random(seed)
    ruin_level = ruin_fraction * starting_bankroll
    final_returns: list[float] = []
    drawdowns: list[float] = []
    ruined = 0

    for _ in range(sims):
        equity = starting_bankroll
        peak = equity
        mdd = 0.0
        is_ruined = False
        for _ in range(n):
            equity += pnls[rng.randrange(n)]   # resample with replacement
            if equity > peak:
                peak = equity
            if peak > 0:
                mdd = max(mdd, (peak - equity) / peak)
            if equity <= ruin_level:
                is_ruined = True
        final_returns.append((equity - starting_bankroll) / starting_bankroll)
        drawdowns.append(mdd)
        if is_ruined:
            ruined += 1

    final_returns.sort()
    drawdowns.sort()
    return {
        "paper_only": True, "insufficient_data": n < 10, "sims": sims, "n_trades": n,
        "seed": seed, "ruin_fraction": ruin_fraction,
        "probability_of_ruin": round(ruined / sims, 4),
        "expected_drawdown": round(sum(drawdowns) / sims, 4),
        "drawdown_p95": round(_percentile(drawdowns, 0.95), 4),
        "median_return": round(_percentile(final_returns, 0.50), 4),
        "return_distribution": {
            "p05": round(_percentile(final_returns, 0.05), 4),
            "p25": round(_percentile(final_returns, 0.25), 4),
            "p50": round(_percentile(final_returns, 0.50), 4),
            "p75": round(_percentile(final_returns, 0.75), 4),
            "p95": round(_percentile(final_returns, 0.95), 4),
        },
        "return_ci_90": [round(_percentile(final_returns, 0.05), 4),
                         round(_percentile(final_returns, 0.95), 4)],
    }
