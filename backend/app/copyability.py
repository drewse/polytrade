"""
Copyability scoring — "how good a *copy target* is this wallet?"

This is deliberately SEPARATE from `scoring.py` (raw profitability). A wallet can
be profitable yet a poor copy target (tiny sample, one lucky market, spoofy
micro-trades). Copyability blends profitability with reliability/robustness and
actively penalizes the patterns that make a copy strategy blow up:

  * tiny samples (can't trust the edge yet)            -> shrink toward neutral
  * too-good-to-be-true (very high win, few trades)    -> capped + flagged
  * suspected spoof / noise (micro-notional spam, very
    low consistency, huge volume in 1–2 markets)       -> forced toward "ignore"

Inputs are duck-typed (a WalletStat-like object + a list of trades), so the
engine is trivially unit-testable without a database.

Output: a 0..100 `copyability_score`, a `classification`, human-readable
`reasons`, and a `suspected_noise` flag.

Classifications: elite_candidate | good_candidate | watchlist | ignore | insufficient_data
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

# thresholds (score bands)
ELITE = 79.0
GOOD = 60.0
WATCH = 40.0

MIN_SETTLED = 8           # need at least this many *resolved* trades to judge edge
TRUST_SETTLED = 45        # sample size at which we fully trust the numbers

# Redesigned weights (sum = 1.0). Rationale — copyability must reflect whether a
# wallet ACTUALLY MAKES MONEY, not how often it wins. A high win rate on a 0..1
# prediction market is cheap (buy favorites at 0.9, win 90% of the time, still
# lose money on the 10% that miss). So profitability metrics dominate and win
# rate is heavily de-weighted:
#   roi 0.28          — the core question: did it make money per dollar risked?
#   profit_factor 0.20 — gross wins / gross losses; <1 means a net loser. Directly
#                        catches the "many small wins, few big losses" trap.
#   sharpe 0.14       — risk-adjusted return; rewards steady edge over lucky spikes.
#   drawdown 0.10     — penalize wallets that bleed deeply even if they recover.
#   expectancy 0.08   — average $ P&L per settled position (EV sign + magnitude).
#   sample 0.08       — more settled positions => more trustworthy.
#   consistency 0.07  — stable win rate across time, not one hot streak.
#   win 0.05          — REDUCED from 0.18; a tiebreaker, never the driver.
WEIGHTS = {
    "roi": 0.28,
    "profit_factor": 0.20,
    "sharpe": 0.14,
    "drawdown": 0.10,
    "expectancy": 0.08,
    "sample": 0.08,
    "consistency": 0.07,
    "win": 0.05,
}


@dataclass
class CopyabilityResult:
    copyability_score: float
    classification: str
    suspected_noise: bool
    distinct_markets: int
    num_settled: int
    reasons: list[str] = field(default_factory=list)


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def score_copyability(stat, trades, min_trade_count: int = 15) -> CopyabilityResult:
    """`stat` needs: num_trades, realized_roi, win_rate, avg_trade_size,
    recency_score, consistency, category_performance. `trades` is the wallet's
    trade list (objects with .market_id and .realized_pnl)."""
    n = int(getattr(stat, "num_trades", 0) or 0)
    # Settled count: live mode reconstructs resolved positions upstream and stores
    # the count on the stat (fills carry no per-trade P&L); mock/legacy fills carry
    # realized P&L directly. Take the max so whichever source has it wins — and a
    # stale/zero-default stat can't mask trades that clearly settled.
    n_settled = max(
        int(getattr(stat, "num_settled", 0) or 0),
        len([t for t in trades if getattr(t, "realized_pnl", 0.0)]),
    )
    distinct_markets = len({t.market_id for t in trades})
    avg_size = float(getattr(stat, "avg_trade_size", 0.0) or 0.0)
    win_rate = float(getattr(stat, "win_rate", 0.0) or 0.0)
    roi = float(getattr(stat, "realized_roi", 0.0) or 0.0)
    consistency = float(getattr(stat, "consistency", 0.0) or 0.0)
    recency = float(getattr(stat, "recency_score", 0.0) or 0.0)
    cats = getattr(stat, "category_performance", {}) or {}
    # profitability metrics (the new drivers)
    pf = float(getattr(stat, "profit_factor", 0.0) or 0.0)
    expectancy = float(getattr(stat, "expectancy", 0.0) or 0.0)
    sharpe = float(getattr(stat, "sharpe", 0.0) or 0.0)
    drawdown = float(getattr(stat, "max_drawdown", 0.0) or 0.0)

    reasons: list[str] = []

    # ---- spoof / noise detection ------------------------------------------
    suspected_noise = False
    if n >= 20 and avg_size < 8:
        suspected_noise = True
        reasons.append(f"noise: micro-notional spam (avg ${avg_size:.0f})")
    if n >= 25 and consistency < 0.12:
        suspected_noise = True
        reasons.append("noise: erratic per-trade returns (very low consistency)")
    if n >= 30 and distinct_markets <= 2:
        suspected_noise = True
        reasons.append(f"noise: {n} trades across only {distinct_markets} market(s)")

    # ---- insufficient data ------------------------------------------------
    if n < min_trade_count or n_settled < MIN_SETTLED:
        reasons.append(f"insufficient sample (n={n}, settled={n_settled})")
        return CopyabilityResult(
            copyability_score=round(min(40.0, 5.0 + n), 1),
            classification="insufficient_data",
            suspected_noise=suspected_noise,
            distinct_markets=distinct_markets,
            num_settled=n_settled,
            reasons=reasons,
        )

    # ---- normalized factors (0..1) — profitability-centric ----------------
    roi_n = _clip((roi + 0.10) / 0.40)        # ROI -10%->0, +30%->1 (neg ROI ~0)
    pf_n = _clip((pf - 1.0) / 1.5)            # PF 1.0->0, 2.5->1 (PF<1 -> 0)
    sharpe_n = _clip((sharpe + 0.2) / 1.2)    # -0.2->0, 1.0->1
    dd_n = _clip(1.0 - drawdown / 0.5)        # 0% DD->1, 50%+ DD->0
    exp_n = _clip(expectancy / 100.0)         # $0/pos->0, $100/pos->1
    sample_n = _clip(n_settled / TRUST_SETTLED)
    cons_n = _clip(consistency)
    win_n = _clip((win_rate - 0.45) / 0.35)   # 0.45->0, 0.80->1 (reduced weight)

    base = (
        WEIGHTS["roi"] * roi_n
        + WEIGHTS["profit_factor"] * pf_n
        + WEIGHTS["sharpe"] * sharpe_n
        + WEIGHTS["drawdown"] * dd_n
        + WEIGHTS["expectancy"] * exp_n
        + WEIGHTS["sample"] * sample_n
        + WEIGHTS["consistency"] * cons_n
        + WEIGHTS["win"] * win_n
    )
    score = 100.0 * base

    # ---- small-sample shrinkage toward neutral (50) -----------------------
    trust = _clip(n_settled / TRUST_SETTLED)
    score = 50.0 + (score - 50.0) * (0.45 + 0.55 * trust)

    # ---- PROFITABILITY GATE (authoritative) -------------------------------
    # A wallet that loses money must never outrank a profitable one. Any of
    # negative ROI, profit factor < 1, or non-positive expectancy caps the
    # score below the watchlist floor (=> at most 'ignore'), regardless of how
    # high its win rate is.
    unprofitable = roi < 0 or pf < 1.0 or expectancy <= 0
    if unprofitable:
        score = min(score, WATCH - 1.0)  # <40 -> ignore tier
        reasons.append(
            f"unprofitable: ROI {roi*100:.1f}%, PF {pf:.2f}, expectancy ${expectancy:.0f}")

    # ---- too-good-to-be-true ---------------------------------------------
    if win_rate > 0.9 and n_settled < 25:
        score = min(score, 47.0)
        suspected_noise = True
        reasons.append(f"too-good-to-be-true: {win_rate*100:.0f}% win on only {n_settled} settled")

    if suspected_noise:
        score = min(score, 30.0)

    score = round(_clip(score, 0.0, 100.0), 1)

    # ---- descriptors ------------------------------------------------------
    if roi > 0.15:
        reasons.append(f"strong ROI ({roi*100:.0f}%)")
    if pf >= 1.5:
        reasons.append(f"profit factor {pf:.2f}")
    if sharpe >= 0.4:
        reasons.append(f"solid Sharpe ({sharpe:.2f})")
    if drawdown > 0.4:
        reasons.append(f"deep drawdown ({drawdown*100:.0f}%)")
    if win_rate >= 0.6 and roi > 0:
        reasons.append(f"win rate {win_rate*100:.0f}%")

    classification = classify(score, suspected_noise)
    if not reasons:
        reasons.append("mixed signals")
    return CopyabilityResult(
        copyability_score=score,
        classification=classification,
        suspected_noise=suspected_noise,
        distinct_markets=distinct_markets,
        num_settled=n_settled,
        reasons=reasons,
    )


def classify(score: float, suspected_noise: bool) -> str:
    if suspected_noise or score < WATCH:
        return "ignore"
    if score < GOOD:
        return "watchlist"
    if score < ELITE:
        return "good_candidate"
    return "elite_candidate"
