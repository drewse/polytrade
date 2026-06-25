"""
Probability estimation (Phase 4 + Phase 10: isolated estimator).

A WEIGHTED STATISTICAL model — deliberately not ML, but structured so the whole
estimator can be swapped for an ML model later WITHOUT touching the engine: the
engine only calls `estimate(features) -> p`. Every weight is documented below.

We blend two views and average them:

  A) Market-anchored view  = market_price + historical_edge
     (a wallet buying BELOW its long-run win rate looks favourable)

  B) Evidence view (weighted sum of normalized 0..1 signals):
        0.34  wallet win rate            (does this wallet win?)
        0.20  wallet Sharpe (proxy)      (risk-adjusted skill)
        0.14  wallet ROI                 (profitability)
        0.12  signal confidence          (engine's own conviction)
        0.10  market implied prob (price)(crowd wisdom)
        0.06  wallet specialization      (edge in this category)
        0.04  liquidity trust            (thin markets => shrink to price)
     (weights sum to 1.0)

The two views are averaged, then SHRUNK toward the market price by a trust
factor (more wallet evidence => trust the model more). Output is clamped to
0.01..0.99 so we NEVER fabricate certainty.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

PRICE_FLOOR, PRICE_CEIL = 0.01, 0.99

# Documented evidence weights (sum = 1.0).
W_WIN = 0.34
W_SHARPE = 0.20
W_ROI = 0.14
W_CONFIDENCE = 0.12
W_PRICE = 0.10
W_SPECIALIZATION = 0.06
W_LIQUIDITY = 0.04
_W_SUM = W_WIN + W_SHARPE + W_ROI + W_CONFIDENCE + W_PRICE + W_SPECIALIZATION + W_LIQUIDITY
assert abs(_W_SUM - 1.0) < 1e-9, f"probability weights must sum to 1.0 (got {_W_SUM})"

# trust grows with settled sample, saturating near this many settled positions.
TRUST_SETTLED = 40.0


def clamp(x: float, lo: float = PRICE_FLOOR, hi: float = PRICE_CEIL) -> float:
    return max(lo, min(hi, x))


def _norm01(x: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 0.5
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


@dataclass
class ProbFeatures:
    market_price: float          # 0..1 implied probability
    edge: float | None = None    # historical edge (win_rate - price) if known
    win_rate: float | None = None        # 0..1
    sharpe: float | None = None          # wallet Sharpe proxy (~ -1..3)
    roi: float | None = None             # fraction, e.g. 0.2
    confidence: float | None = None      # 0..100
    specialization: float | None = None  # category ROI, fraction
    liquidity: float | None = None       # USD
    num_settled: int | None = None       # evidence depth


def estimate(f: ProbFeatures) -> float:
    """Return P(win) in 0.01..0.99 from the weighted statistical model."""
    price = clamp(_safe(f.market_price, 0.5))

    # View A — market-anchored.
    if f.edge is not None:
        view_a = price + float(f.edge)
    else:
        view_a = price

    # View B — weighted evidence blend (each term normalized to 0..1).
    win_n = clamp(_safe(f.win_rate, price), 0.0, 1.0)
    sharpe_n = _norm01(_safe(f.sharpe, 0.0), -0.5, 2.5)       # -0.5..2.5 -> 0..1
    roi_n = clamp(0.5 + _safe(f.roi, 0.0), 0.0, 1.0)          # -0.5..+0.5 -> 0..1
    conf_n = clamp(_safe(f.confidence, 50.0) / 100.0, 0.0, 1.0)
    price_n = price
    spec_n = clamp(0.5 + _safe(f.specialization, 0.0), 0.0, 1.0)
    liq_n = clamp(math.log10(max(1.0, _safe(f.liquidity, 1.0))) / 5.0, 0.0, 1.0)  # $1..$100k

    view_b = (
        W_WIN * win_n + W_SHARPE * sharpe_n + W_ROI * roi_n
        + W_CONFIDENCE * conf_n + W_PRICE * price_n
        + W_SPECIALIZATION * spec_n + W_LIQUIDITY * liq_n
    )

    blended = 0.5 * view_a + 0.5 * view_b

    # Shrink toward market price by evidence trust (sparse history => trust crowd).
    trust = clamp(_safe(f.num_settled, 0) / TRUST_SETTLED, 0.0, 1.0)
    p = price + (blended - price) * (0.35 + 0.65 * trust)
    return round(clamp(p), 4)


def weights() -> dict:
    """Expose the documented weights (for the UI / transparency)."""
    return {
        "win_rate": W_WIN, "sharpe": W_SHARPE, "roi": W_ROI,
        "confidence": W_CONFIDENCE, "market_price": W_PRICE,
        "specialization": W_SPECIALIZATION, "liquidity": W_LIQUIDITY,
        "trust_settled": TRUST_SETTLED,
    }


def _safe(v, default: float) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default
