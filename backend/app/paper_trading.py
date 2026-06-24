"""
Paper trading simulator.

Pure-ish helpers for opening, marking, and closing simulated positions. The
actual DB writes happen in `services.py`; this module holds the math and the
risk-rule checks so they're easy to read and test.

Risk rules (all configurable via Settings):
  * max single position = max_position_pct % of bankroll
  * max total exposure per market = max_market_exposure_pct % of bankroll
  * simulated slippage = slippage_cents (added to buys, subtracted from sells)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .models import Market, PaperPosition


@dataclass
class RiskConfig:
    bankroll: float
    max_position_pct: float
    max_market_exposure_pct: float
    slippage_cents: float
    min_confidence: float


def apply_slippage(price: float, side: str, slippage_cents: float) -> float:
    """Slippage in *cents* on a 0..1 priced market. Buys fill worse (higher)."""
    delta = slippage_cents / 100.0
    filled = price + delta if side == "buy" else price - delta
    return round(min(0.99, max(0.01, filled)), 4)


def effective_slippage_cents(base_cents: float, liquidity: float, thin_threshold: float = 2000.0) -> float:
    """Scale slippage up on thin markets — low liquidity means worse fills.

    At/above `thin_threshold` liquidity, slippage = base. Below it, slippage
    grows inversely with liquidity (capped at 8x) so thin markets can erase the
    edge entirely.
    """
    if liquidity is None or liquidity >= thin_threshold or liquidity <= 0:
        return base_cents
    mult = min(8.0, thin_threshold / liquidity)
    return round(base_cents * mult, 3)


def position_size(bankroll: float, max_position_pct: float) -> float:
    return round(bankroll * (max_position_pct / 100.0), 2)


def market_exposure(open_positions: list[PaperPosition], market_id: str) -> float:
    return sum(p.size for p in open_positions if p.market_id == market_id and p.status == "open")


def can_open(
    risk: RiskConfig,
    confidence: float,
    open_positions: list[PaperPosition],
    market_id: str,
    intended_size: float,
) -> tuple[bool, str]:
    """Return (allowed, reason_if_blocked)."""
    if confidence < risk.min_confidence:
        return False, f"confidence {confidence:.0f} < min {risk.min_confidence:.0f}"
    cap = risk.bankroll * (risk.max_market_exposure_pct / 100.0)
    current = market_exposure(open_positions, market_id)
    if current + intended_size > cap + 1e-6:
        return False, (
            f"market exposure cap hit (${current:,.0f}+${intended_size:,.0f} > ${cap:,.0f})"
        )
    return True, ""


def mark_to_market(position: PaperPosition, current_price: float) -> float:
    """Unrealized PnL for an open position given the latest price.

    shares = size / entry_price; value now = shares * current_price.
    """
    value_now = position.shares * current_price
    return round(value_now - position.size, 2)


def realized_on_close(position: PaperPosition, exit_price: float) -> float:
    value = position.shares * exit_price
    return round(value - position.size, 2)


def resolution_exit_price(market: Market, outcome: str) -> float | None:
    """If the market resolved, the position's outcome is worth $1 or $0."""
    if not market.resolved or market.resolved_outcome is None:
        return None
    return 1.0 if outcome == market.resolved_outcome else 0.0


def now() -> datetime:
    return datetime.now(timezone.utc)
