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

WEIGHTS = {
    "roi": 0.26,
    "win": 0.18,
    "consistency": 0.14,
    "recency": 0.10,
    "notional": 0.08,
    "diversity": 0.12,
    "specialization": 0.06,
    "sample": 0.06,
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
    settled = [t for t in trades if getattr(t, "realized_pnl", 0.0)]
    n_settled = len(settled)
    distinct_markets = len({t.market_id for t in trades})
    avg_size = float(getattr(stat, "avg_trade_size", 0.0) or 0.0)
    win_rate = float(getattr(stat, "win_rate", 0.0) or 0.0)
    roi = float(getattr(stat, "realized_roi", 0.0) or 0.0)
    consistency = float(getattr(stat, "consistency", 0.0) or 0.0)
    recency = float(getattr(stat, "recency_score", 0.0) or 0.0)
    cats = getattr(stat, "category_performance", {}) or {}

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

    # ---- normalized factors (0..1) ----------------------------------------
    roi_n = _clip(0.5 + roi)                          # -0.5..+0.5 -> 0..1
    win_n = _clip((win_rate - 0.45) / 0.35)           # 0.45->0, 0.80->1
    cons_n = _clip(consistency)
    rec_n = _clip(recency)
    notional_n = _clip(math.log10(avg_size + 1) / 3.0)
    diversity_n = _clip(distinct_markets / 20.0)
    spec = max(cats.values()) if cats else 0.0
    spec_n = _clip(spec / 0.4)
    sample_n = _clip(n_settled / TRUST_SETTLED)

    base = (
        WEIGHTS["roi"] * roi_n
        + WEIGHTS["win"] * win_n
        + WEIGHTS["consistency"] * cons_n
        + WEIGHTS["recency"] * rec_n
        + WEIGHTS["notional"] * notional_n
        + WEIGHTS["diversity"] * diversity_n
        + WEIGHTS["specialization"] * spec_n
        + WEIGHTS["sample"] * sample_n
    )
    score = 100.0 * base

    # ---- small-sample shrinkage toward neutral (50) -----------------------
    trust = _clip(n_settled / TRUST_SETTLED)
    score = 50.0 + (score - 50.0) * (0.45 + 0.55 * trust)

    # ---- too-good-to-be-true ---------------------------------------------
    if win_rate > 0.9 and n_settled < 25:
        score = min(score, 47.0)
        suspected_noise = True
        reasons.append(f"too-good-to-be-true: {win_rate*100:.0f}% win on only {n_settled} settled")

    if suspected_noise:
        score = min(score, 30.0)

    score = round(_clip(score, 0.0, 100.0), 1)

    # ---- positive descriptors --------------------------------------------
    if roi > 0.15:
        reasons.append(f"strong ROI ({roi*100:.0f}%)")
    if win_rate >= 0.6 and not suspected_noise:
        reasons.append(f"solid win rate ({win_rate*100:.0f}%)")
    if distinct_markets >= 12:
        reasons.append(f"diversified across {distinct_markets} markets")
    if spec > 0.2:
        best = max(cats, key=cats.get)
        reasons.append(f"specialist edge in {best} ({cats[best]*100:.0f}%)")
    if recency > 0.6:
        reasons.append("recently active")

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
