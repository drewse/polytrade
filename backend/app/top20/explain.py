"""
Explainability (Phase 8 + Phase 10: isolated explanation component).

Builds a structured, human-readable record for every paper trade (why entered)
and for every skipped signal (why not). Stored on the trade as JSON and rendered
verbatim in the UI. PAPER ONLY — these are research annotations, not orders.
"""
from __future__ import annotations

from .strategies import Ctx
from .sizing import SizingResult


def build_entry(ctx: Ctx, sizing: SizingResult, p: float, exit_policy: str,
                rank: int | None = None) -> dict:
    """Structured explanation for a taken trade."""
    ev = round((p - ctx.price), 4)  # expected value proxy vs price paid
    rank_txt = f"#{rank + 1}" if rank is not None else "unranked"
    factors = [
        f"Copied wallet ranked {rank_txt} by copyability ({ctx.copyability:.0f}).",
        f"Wallet history: win {ctx.win_rate*100:.0f}%, ROI {ctx.roi*100:.0f}%, Sharpe~{ctx.sharpe:.2f}.",
        f"Observed edge {ctx.edge*100:.1f}% at price {ctx.price:.2f}.",
        f"Estimated win probability {p*100:.0f}% (model).",
        f"Sizing: {sizing.reason}.",
        f"Signal confidence {ctx.confidence:.0f}; liquidity ${ctx.liquidity:,.0f}.",
        f"Expected value vs price: {'positive' if ev > 0 else 'non-positive'} ({ev:+.2f}).",
        f"Exit policy: {exit_policy}.",
    ]
    return {
        "wallet_copyability": round(ctx.copyability, 1),
        "wallet_win_rate": round(ctx.win_rate, 4),
        "wallet_sharpe": round(ctx.sharpe, 2),
        "wallet_roi": round(ctx.roi, 4),
        "edge": round(ctx.edge, 4),
        "price": round(ctx.price, 4),
        "estimated_probability": round(p, 4),
        "kelly_fraction": sizing.kelly_fraction,
        "target_fraction": sizing.target_fraction,
        "stake": sizing.stake,
        "confidence": round(ctx.confidence, 1),
        "liquidity": round(ctx.liquidity, 0),
        "expected_value": ev,
        "category": ctx.category,
        "exit_policy": exit_policy,
        "wallet_rank": rank,
        "summary": " ".join(factors),
    }


def build_skip(strategy_name: str, reason: str) -> dict:
    return {"strategy": strategy_name, "decision": "skip", "reason": reason}
