"""
Wallet scoring engine.

Aggregates a wallet's trade history into a 0..100 score and a classification.
The score blends several factors, each normalized to 0..1 and weighted:

  * realized ROI         (how profitable)
  * win rate             (how often right)
  * consistency          (low volatility of per-trade ROI)
  * recency              (traded recently?)
  * sample size          (more trades -> more trustworthy)

A Bayesian-style shrinkage is applied so wallets with tiny samples can't get an
extreme score from luck. Wallets below a minimum trade count are flagged
`insufficient_data` regardless of raw numbers.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

from .models import Trade

MIN_TRADES_FOR_SCORE = 8     # below this -> "insufficient_data"
CONFIDENT_SAMPLE = 60        # sample size at which we fully trust the win-rate

# Weights for the blended score (sum to 1.0).
WEIGHTS = {
    "roi": 0.35,
    "win_rate": 0.25,
    "consistency": 0.15,
    "recency": 0.10,
    "size": 0.05,
    "sample": 0.10,
}


@dataclass
class ScoreResult:
    num_trades: int
    realized_pnl: float
    realized_roi: float
    win_rate: float
    avg_trade_size: float
    consistency: float
    recency_score: float
    category_performance: dict
    score: float
    classification: str


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _win_rate_consistency(settled: list) -> float:
    """1.0 = win rate is steady across time; ~0 = it swings wildly.

    Buckets settled trades chronologically into up to 5 groups and returns
    1 - normalized stdev of per-bucket win rates."""
    if len(settled) < 6:
        return 0.4  # not enough to judge stability -> neutral-ish
    ordered = sorted(settled, key=lambda t: t.timestamp)
    n_buckets = min(5, len(ordered) // 3)
    if n_buckets < 2:
        return 0.4
    size = len(ordered) // n_buckets
    rates = []
    for b in range(n_buckets):
        chunk = ordered[b * size: (b + 1) * size] if b < n_buckets - 1 else ordered[b * size:]
        if chunk:
            rates.append(sum(1 for t in chunk if t.realized_pnl > 0) / len(chunk))
    if len(rates) < 2:
        return 0.4
    stdev = statistics.pstdev(rates)
    return _clip01(1.0 - stdev / 0.5)  # stdev 0 -> 1.0, stdev 0.5 -> 0.0


def _recency_score(last_ts: datetime | None) -> float:
    if last_ts is None:
        return 0.0
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - last_ts).total_seconds() / 86400.0
    # 1.0 if traded today, decaying to ~0 over ~45 days.
    return _clip01(math.exp(-days / 20.0))


def score_wallet(trades: list[Trade]) -> ScoreResult:
    n = len(trades)
    if n == 0:
        return ScoreResult(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, {}, 0.0, "insufficient_data")

    # Only *resolved* trades (those with a realized result) drive ROI/win-rate.
    settled = [t for t in trades if t.realized_pnl != 0.0]
    total_size = sum(t.size for t in trades)
    avg_size = total_size / n

    realized_pnl = sum(t.realized_pnl for t in settled)
    settled_size = sum(t.size for t in settled) or 1.0
    realized_roi = realized_pnl / settled_size  # fraction

    wins = sum(1 for t in settled if t.realized_pnl > 0)
    raw_win_rate = wins / len(settled) if settled else 0.0

    # Shrink win rate toward 0.5 for small samples (Bayesian-ish). The shrunk
    # value feeds the score; `raw_win_rate` is reported as-is for display.
    k = len(settled)
    win_rate = raw_win_rate * (k / (k + CONFIDENT_SAMPLE)) + 0.5 * (
        CONFIDENT_SAMPLE / (k + CONFIDENT_SAMPLE)
    )

    # Consistency: how STABLE the win rate is across time, not the variance of
    # per-trade ROI (which is dominated by cheap-longshot payouts on a 0..1
    # market and would look "inconsistent" even for steady winners). We bucket
    # settled trades chronologically and measure win-rate stability.
    consistency = _win_rate_consistency(settled)

    last_ts = max((t.timestamp for t in trades), default=None)
    recency = _recency_score(last_ts)

    # category performance: realized roi per category
    cat_pnl: dict[str, float] = defaultdict(float)
    cat_size: dict[str, float] = defaultdict(float)
    for t in settled:
        cat = (t.market.category if t.market else None) or "Unknown"
        cat_pnl[cat] += t.realized_pnl
        cat_size[cat] += t.size
    category_performance = {
        cat: round(cat_pnl[cat] / max(cat_size[cat], 1.0), 4) for cat in cat_pnl
    }

    # --- normalize factors to 0..1 ------------------------------------------
    # ROI: map [-0.5, +0.5] -> [0, 1], clipped.
    roi_n = _clip01(0.5 + realized_roi)
    win_n = _clip01(win_rate)
    cons_n = consistency
    rec_n = recency
    # size: gently reward bigger average size (more conviction), saturates.
    size_n = _clip01(math.log10(avg_size + 1) / 3.0)
    # sample: trust grows with trade count, saturating near CONFIDENT_SAMPLE.
    sample_n = _clip01(n / CONFIDENT_SAMPLE)

    blended = (
        WEIGHTS["roi"] * roi_n
        + WEIGHTS["win_rate"] * win_n
        + WEIGHTS["consistency"] * cons_n
        + WEIGHTS["recency"] * rec_n
        + WEIGHTS["size"] * size_n
        + WEIGHTS["sample"] * sample_n
    )
    score = round(100.0 * blended, 1)

    classification = classify(n, score)

    return ScoreResult(
        num_trades=n,
        realized_pnl=round(realized_pnl, 2),
        realized_roi=round(realized_roi, 4),
        win_rate=round(raw_win_rate, 4),
        avg_trade_size=round(avg_size, 2),
        consistency=round(consistency, 4),
        recency_score=round(recency, 4),
        category_performance=category_performance,
        score=score,
        classification=classification,
    )


def classify(num_trades: int, score: float) -> str:
    if num_trades < MIN_TRADES_FOR_SCORE:
        return "insufficient_data"
    if score >= 65:
        return "sharp"
    if score >= 45:
        return "neutral"
    return "bad"  # fade candidate
