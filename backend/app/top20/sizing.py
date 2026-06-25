"""
Position sizing (Phase 10: isolated risk/sizing component).

All sizers return a SizingResult; the engine never sizes inline. Fractional
Kelly is the core; other sizers (fixed $, fixed %, confidence/edge/quality/
volatility-adjusted Kelly) are thin wrappers that scale the Kelly fraction or
bypass it. Hard caps are applied uniformly so NO sizer can ever produce a
negative, zero-price, or oversized stake. PAPER ONLY.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

PRICE_FLOOR, PRICE_CEIL = 0.01, 0.99

# Global hard caps (defaults; a strategy may tighten via its SizingPolicy).
MIN_BET = 5.0
MAX_BET = 250.0
MAX_POSITION_PCT = 0.05          # 5% of strategy bankroll per position
MAX_MARKET_EXPOSURE_PCT = 0.10   # 10% of strategy bankroll per market


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _safe(v, default: float) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


@dataclass
class SizingPolicy:
    """How a strategy turns a (price, p, edge, confidence, quality) into a stake."""
    mode: str = "kelly"                  # kelly|fixed_dollar|fixed_pct
    kelly_multiplier: float = 0.25       # fractional Kelly (0.25 = quarter)
    fixed_dollar: float = 100.0          # for mode=fixed_dollar
    fixed_pct: float = 0.02              # for mode=fixed_pct (of bankroll)
    adjust: str | None = None            # None|confidence|edge|quality|volatility
    min_bet: float = MIN_BET
    max_bet: float = MAX_BET
    max_position_pct: float = MAX_POSITION_PCT
    max_market_exposure_pct: float = MAX_MARKET_EXPOSURE_PCT


@dataclass
class SizingResult:
    stake: float | None              # None => skip
    kelly_fraction: float            # raw Kelly (can be <= 0)
    target_fraction: float           # fraction of bankroll the sizer wanted (pre-cap)
    shares: float
    reason: str


def raw_kelly(price: float, p: float) -> float:
    """Raw Kelly fraction for a 0..1 priced binary share. price clamped away
    from 0 so b is finite and there is no division by zero."""
    price = clamp(_safe(price, 0.5), PRICE_FLOOR, PRICE_CEIL)
    p = clamp(_safe(p, 0.5), PRICE_FLOOR, PRICE_CEIL)
    b = (1.0 - price) / price
    q = 1.0 - p
    return (b * p - q) / b


def size(policy: SizingPolicy, *, price: float, p: float, bankroll: float,
         market_exposure_used: float = 0.0, confidence: float = 50.0,
         edge: float = 0.0, quality: float = 50.0) -> SizingResult:
    """Compute a capped paper stake. Skips (stake=None) on non-positive Kelly or
    when caps leave no room. Never negative, never oversized, never div-by-zero."""
    price_c = clamp(_safe(price, 0.5), PRICE_FLOOR, PRICE_CEIL)
    p_c = clamp(_safe(p, 0.5), PRICE_FLOOR, PRICE_CEIL)
    kelly = raw_kelly(price_c, p_c)
    if bankroll <= 0:
        return SizingResult(None, round(kelly, 4), 0.0, 0.0, "no bankroll")

    # --- target fraction of bankroll, by mode ---
    if policy.mode == "fixed_dollar":
        target_fraction = policy.fixed_dollar / bankroll
        reason = f"fixed ${policy.fixed_dollar:.0f}"
    elif policy.mode == "fixed_pct":
        target_fraction = policy.fixed_pct
        reason = f"fixed {policy.fixed_pct*100:.1f}% of bankroll"
    else:  # kelly
        if kelly <= 0:
            return SizingResult(None, round(kelly, 4), 0.0, 0.0, "kelly<=0")
        mult = policy.kelly_multiplier
        adj_note = ""
        if policy.adjust == "confidence":
            f = clamp(confidence / 100.0, 0.0, 1.0)
            mult *= f
            adj_note = f" x conf {f:.2f}"
        elif policy.adjust == "edge":
            f = clamp(0.5 + edge * 5.0, 0.25, 1.0)   # bigger edge -> bigger size
            mult *= f
            adj_note = f" x edge {f:.2f}"
        elif policy.adjust == "quality":
            f = clamp(quality / 100.0, 0.0, 1.0)
            mult *= f
            adj_note = f" x quality {f:.2f}"
        elif policy.adjust == "volatility":
            # variance of a Bernoulli at this price is p(1-p); lower vol -> larger.
            vol = math.sqrt(max(1e-6, price_c * (1 - price_c)))
            f = clamp(0.35 / vol, 0.25, 1.0)
            mult *= f
            adj_note = f" x vol {f:.2f}"
        target_fraction = kelly * mult
        reason = f"kelly={kelly:.3f} x {policy.kelly_multiplier}{adj_note}"

    target_fraction = max(0.0, target_fraction)
    stake = target_fraction * bankroll

    # --- hard caps (uniform across all sizers) ---
    pos_cap = min(policy.max_bet, policy.max_position_pct * bankroll)
    exposure_room = policy.max_market_exposure_pct * bankroll - market_exposure_used
    upper = min(pos_cap, exposure_room)
    if upper < policy.min_bet:
        return SizingResult(None, round(kelly, 4), round(target_fraction, 4), 0.0,
                            "caps below min_bet")
    stake = min(stake, upper)
    stake = max(stake, policy.min_bet)
    stake = round(stake, 2)
    shares = round(stake / price_c, 4)
    return SizingResult(stake, round(kelly, 4), round(target_fraction, 4), shares,
                        f"{reason} -> ${stake:.2f} (cap ${upper:.2f})")
