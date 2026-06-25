"""
Performance analytics (Phase 1 + Phase 10: isolated analytics component).

Pure functions over a strategy's trades + equity series. No DB, no engine
coupling — the engine calls `compute_metrics(...)` and persists the result.
Everything is paper P&L.

Metric conventions:
  * per-trade return  = realized_pnl / stake
  * Sharpe / Sortino  = mean / stdev of per-trade returns (dimensionless,
                        annualization factor reported separately as info)
  * drawdown          = max peak-to-trough fraction of the equity curve
  * Kelly growth rate = mean(log(1 + realized_pnl / starting_bankroll))
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


# Below this many closed trades, point estimates (Sharpe etc.) are unreliable.
MIN_RELIABLE_TRADES = 10
MIN_DAYS_FOR_CAGR = 3.0


def sharpe(returns: list[float], rf: float = 0.0) -> float:
    """Mean/stdev of returns. 0 if <2 samples or zero variance."""
    if len(returns) < 2:
        return 0.0
    excess = [r - rf for r in returns]
    sd = statistics.pstdev(excess)
    if sd == 0:
        return 0.0
    return round(_mean(excess) / sd, 4)


def sharpe_ci(returns: list[float]) -> list[float]:
    """95% confidence interval for the Sharpe ratio (Lo's standard error:
    SE ≈ sqrt((1 + 0.5·SR²)/n)). Wide for small n — that's the point."""
    n = len(returns)
    if n < 2:
        return [0.0, 0.0]
    sr = sharpe(returns)
    se = math.sqrt((1 + 0.5 * sr * sr) / n)
    return [round(sr - 1.96 * se, 4), round(sr + 1.96 * se, 4)]


def sortino_ci(returns: list[float]) -> list[float]:
    n = len(returns)
    if n < 2:
        return [0.0, 0.0]
    so = sortino(returns)
    se = math.sqrt((1 + 0.5 * so * so) / n)
    return [round(so - 1.96 * se, 4), round(so + 1.96 * se, 4)]


def sortino(returns: list[float], rf: float = 0.0) -> float:
    """Mean/downside-deviation (only negative returns penalize)."""
    if len(returns) < 2:
        return 0.0
    excess = [r - rf for r in returns]
    downside = [min(0.0, e) for e in excess]
    dd = math.sqrt(_mean([d * d for d in downside]))
    if dd == 0:
        return 0.0
    return round(_mean(excess) / dd, 4)


def profit_factor(pnls: list[float]) -> float:
    """gross wins / gross losses. inf-ish capped at 999 when no losses."""
    wins = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p < 0)
    if losses == 0:
        return round(min(999.0, wins), 4) if wins else 0.0
    return round(wins / losses, 4)


def expectancy(pnls: list[float]) -> float:
    """Average P&L per closed trade (USD)."""
    return round(_mean(pnls), 4)


def max_drawdown(equity_curve: list[float]) -> float:
    """Max peak-to-trough drop as a fraction (0..1)."""
    peak = None
    mdd = 0.0
    for e in equity_curve:
        if peak is None or e > peak:
            peak = e
        if peak and peak > 0:
            mdd = max(mdd, (peak - e) / peak)
    return round(mdd, 4)


def streaks(pnls: list[float]) -> tuple[int, int]:
    """(max consecutive wins, max consecutive losses), in chronological order."""
    best_w = best_l = cur_w = cur_l = 0
    for p in pnls:
        if p > 0:
            cur_w += 1
            cur_l = 0
        elif p < 0:
            cur_l += 1
            cur_w = 0
        else:
            cur_w = cur_l = 0
        best_w = max(best_w, cur_w)
        best_l = max(best_l, cur_l)
    return best_w, best_l


def consistency(equity_curve: list[float]) -> float:
    """Fraction of steps where equity made a new high (steady-up => ~1, choppy => low)."""
    if len(equity_curve) < 2:
        return 0.0
    peak = equity_curve[0]
    highs = 0
    for e in equity_curve[1:]:
        if e >= peak:
            highs += 1
            peak = e
    return round(highs / (len(equity_curve) - 1), 4)


def kelly_growth_rate(pnls: list[float], starting_bankroll: float) -> float:
    """Expected log-growth proxy: mean(log(1 + pnl/bankroll))."""
    if not pnls or starting_bankroll <= 0:
        return 0.0
    gs = []
    for p in pnls:
        x = 1.0 + p / starting_bankroll
        if x <= 0:
            x = 1e-6
        gs.append(math.log(x))
    return round(_mean(gs), 6)


def _holding_minutes(closed) -> list[float]:
    out = []
    for t in closed:
        if t.entry_time and t.closed_at:
            out.append((t.closed_at - t.entry_time).total_seconds() / 60.0)
        elif getattr(t, "holding_minutes", None) is not None:
            out.append(float(t.holding_minutes))
    return out


def compute_metrics(closed: list, open_: list, equity_curve: list[float],
                    starting_bankroll: float, signals_seen: int, signals_taken: int,
                    first_ts: datetime | None = None, last_ts: datetime | None = None) -> dict:
    """Full Phase-1 metric bundle. `closed`/`open_` are Top20Trade-like objects."""
    pnls = [t.realized_pnl for t in closed]
    returns = [t.realized_pnl / t.stake for t in closed if t.stake]
    realized = round(sum(pnls), 2)
    unrealized = round(sum(t.unrealized_pnl for t in open_), 2)
    bankroll = round(starting_bankroll + realized, 2)
    equity = round(bankroll + unrealized, 2)

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = round(len(wins) / len(closed), 4) if closed else 0.0
    avg_win = round(_mean(wins), 2)
    avg_loss = round(_mean(losses), 2)
    best_w, best_l = streaks(pnls)
    holds = _holding_minutes(closed)
    avg_kelly = round(_mean([t.kelly_fraction for t in (closed + open_) if t.kelly_fraction]), 4)
    avg_size = round(_mean([t.stake for t in (closed + open_)]), 2)

    total_return = round((equity - starting_bankroll) / starting_bankroll, 4)
    # Annualized (CAGR) only over a real elapsed span — never extrapolate a few
    # hours of trading into a yearly figure. Flag when we can't.
    annualized = 0.0
    annualized_valid = False
    days = 0.0
    if first_ts and last_ts:
        days = max((last_ts - first_ts).total_seconds() / 86400.0, 0.0)
        if days >= MIN_DAYS_FOR_CAGR and equity > 0 and starting_bankroll > 0:
            annualized = round((equity / starting_bankroll) ** (365.0 / days) - 1.0, 4)
            annualized_valid = True

    return {
        "total_return": total_return,
        "annualized_return": annualized,
        "annualized_valid": annualized_valid,
        "elapsed_days": round(days, 2),
        "insufficient_history": len(closed) < MIN_RELIABLE_TRADES,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "total_pnl": round(realized + unrealized, 2),
        "bankroll": bankroll,
        "equity": equity,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor(pnls),
        "expectancy": expectancy(pnls),
        "sharpe": sharpe(returns),
        "sharpe_ci": sharpe_ci(returns),
        "sortino": sortino(returns),
        "sortino_ci": sortino_ci(returns),
        "max_drawdown": max_drawdown(equity_curve),
        "avg_holding_min": round(_mean(holds), 1),
        "median_holding_min": round(statistics.median(holds), 1) if holds else 0.0,
        "largest_win": round(max(pnls), 2) if pnls else 0.0,
        "largest_loss": round(min(pnls), 2) if pnls else 0.0,
        "consecutive_wins": best_w,
        "consecutive_losses": best_l,
        "kelly_growth_rate": kelly_growth_rate(pnls, starting_bankroll),
        "consistency": consistency(equity_curve),
        "signals_seen": signals_seen,
        "signals_taken": signals_taken,
        "signal_acceptance": round(signals_taken / signals_seen, 4) if signals_seen else 0.0,
        "avg_kelly_fraction": avg_kelly,
        "avg_position_size": avg_size,
        "open_positions": len(open_),
        "closed_positions": len(closed),
        "num_trades": len(closed) + len(open_),
    }
