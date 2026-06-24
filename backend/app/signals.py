"""
Signal detection.

Scans newly ingested trades and emits copy-trade signals when a trade looks
worth paper-copying. A signal is created only if ALL gates pass:

  * wallet score >= min_wallet_score
  * wallet has >= min_trade_count trades
  * trade size >= min_trade_size
  * market liquidity >= min_market_liquidity
  * market is not resolved
  * the observed price is not too stale

Confidence (0..100) starts from the wallet score and is boosted when several
sharp wallets pile into the same market+outcome within the same scan window.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

from .models import Market, PaperSignal, Trade, Wallet, WalletStat


@dataclass
class SignalRules:
    min_wallet_score: float
    min_trade_count: int
    min_trade_size: float
    min_market_liquidity: float
    max_price_staleness_min: int
    min_volume: float = 0.0
    min_edge: float = 0.0


def estimate_edge(win_rate: float, observed_price: float) -> float:
    """Rough edge = estimated P(win) - price paid. Uses the wallet's historical
    win rate as a (crude) proxy for P(this outcome wins). Positive = +EV."""
    return round(win_rate - observed_price, 4)


def _age_minutes(ts: datetime) -> float:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 60.0


def detect_signals(
    new_trades: list[Trade],
    wallets_by_id: dict[int, Wallet],
    stats_by_id: dict[int, WalletStat],
    markets_by_id: dict[str, Market],
    rules: SignalRules,
) -> list[PaperSignal]:
    """Pure function: returns PaperSignal objects (not yet persisted)."""

    # Pre-count how many distinct sharp wallets hit each (market, outcome) in
    # this batch, to drive the "multiple sharps agree" confidence boost.
    cluster: dict[tuple[str, str], set[int]] = defaultdict(set)
    for t in new_trades:
        stat = stats_by_id.get(t.wallet_id)
        if stat and stat.classification == "sharp":
            cluster[(t.market_id, t.outcome)].add(t.wallet_id)

    signals: list[PaperSignal] = []
    for t in new_trades:
        wallet = wallets_by_id.get(t.wallet_id)
        stat = stats_by_id.get(t.wallet_id)
        market = markets_by_id.get(t.market_id)
        if not (wallet and stat and market):
            continue

        # --- gates -----------------------------------------------------------
        if not wallet.copy_enabled:
            continue
        if stat.num_trades < rules.min_trade_count:
            continue
        if stat.score < rules.min_wallet_score:
            continue
        if t.size < rules.min_trade_size:
            continue
        if market.resolved:
            continue
        if market.liquidity < rules.min_market_liquidity:
            continue
        if market.volume < rules.min_volume:
            continue
        if _age_minutes(t.timestamp) > rules.max_price_staleness_min:
            continue

        edge = estimate_edge(stat.win_rate, t.price)
        if edge < rules.min_edge:
            continue

        # --- confidence ------------------------------------------------------
        confidence = stat.score  # base on wallet quality
        reasons = [
            f"{wallet.label or wallet.address[:10]} is {stat.classification} "
            f"(score {stat.score:.0f}, ROI {stat.realized_roi*100:.1f}%, "
            f"win {stat.win_rate*100:.0f}% over {stat.num_trades} trades)"
        ]

        agree = cluster.get((t.market_id, t.outcome), set())
        if len(agree) >= 2:
            boost = min(15.0, 5.0 * (len(agree) - 1))
            confidence = min(100.0, confidence + boost)
            reasons.append(f"{len(agree)} sharp wallets entered {t.outcome} together (+{boost:.0f})")

        # Category strength of this wallet.
        cat = market.category or "Unknown"
        cat_roi = (stat.category_performance or {}).get(cat)
        if cat_roi is not None and cat_roi > 0:
            confidence = min(100.0, confidence + 3.0)
            reasons.append(f"positive history in {cat} ({cat_roi*100:.1f}% ROI)")

        # Bigger trades = more conviction.
        if t.size >= 3 * rules.min_trade_size:
            confidence = min(100.0, confidence + 3.0)
            reasons.append(f"large entry (${t.size:,.0f})")

        signals.append(
            PaperSignal(
                wallet_id=t.wallet_id,
                market_id=t.market_id,
                trade_id=t.id,
                outcome=t.outcome,
                side="buy",
                observed_price=t.price,
                suggested_entry=t.price,
                confidence=round(confidence, 1),
                edge_estimate=edge,
                reason="; ".join(reasons),
                copied=False,
            )
        )
    return signals
