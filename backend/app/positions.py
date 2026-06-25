"""
Position reconstruction + realized P&L from raw fills.

The Polymarket trades endpoint returns raw *fills* (a side, an outcome, a price
and a size) — never a per-trade profit. To know whether a wallet actually *wins*
we have to reconstruct positions from those fills and settle them against real
market resolutions:

    raw fills --group by (market, outcome)--> net position --resolution--> realized P&L

A position is only ever **settled** when its market has resolved AND we observed
the entry cost (at least one buy in the window). Unresolved markets stay
unsettled; positions whose opening buys fall outside the recent-history window
are skipped rather than guessed. We never fabricate P&L.

Raw `Trade` rows are treated as immutable; this module derives positions on the
fly so the result is always recomputable from the source fills.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime


@dataclass
class WalletPosition:
    """A reconstructed, resolved position. Duck-types as a "settled trade" for the
    scoring engines: it exposes `.realized_pnl`, `.size`, `.timestamp`, `.market`
    and `.market_id`."""

    market_id: str
    outcome: str
    realized_pnl: float   # payout + sale proceeds - cost basis (USD)
    size: float           # cost basis = total bought notional (USD)
    timestamp: datetime   # last fill in the position
    market: object        # ORM Market (for category lookups); may be None
    settled: bool = True


def _shares(size: float, price: float) -> float:
    """Outcome-token count from USD notional and price. Trade rows store USD
    `size` and `price` (0..1) but not raw shares, so we back them out here."""
    return (size / price) if price else 0.0


def settled_positions(trades, markets_by_id) -> list[WalletPosition]:
    """Reconstruct resolved positions for a wallet's fills.

    `trades` is the wallet's raw fill rows (each with market_id, outcome, side,
    price, size, timestamp). `markets_by_id` maps market_id -> ORM Market (with
    `.resolved`, `.resolved_outcome`). Returns one WalletPosition per resolved
    (market, outcome) the wallet actually paid into.
    """
    groups: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"buy_size": 0.0, "sell_size": 0.0, "buy_sh": 0.0, "sell_sh": 0.0, "last_ts": None}
    )
    for t in trades:
        g = groups[(t.market_id, t.outcome)]
        sh = _shares(t.size, t.price)
        if str(t.side).lower() == "sell":
            g["sell_size"] += t.size
            g["sell_sh"] += sh
        else:
            g["buy_size"] += t.size
            g["buy_sh"] += sh
        if g["last_ts"] is None or t.timestamp > g["last_ts"]:
            g["last_ts"] = t.timestamp

    out: list[WalletPosition] = []
    for (market_id, outcome), g in groups.items():
        market = markets_by_id.get(market_id)
        # Unresolved (or unknown) markets stay unsettled.
        if not (market and market.resolved and market.resolved_outcome is not None):
            continue
        # No observed entry cost -> we can't establish a basis; skip, don't guess.
        if g["buy_size"] <= 0:
            continue
        # Net shares still held at resolution (clamp: selling more than we saw
        # bought means the opening buys predate our window — settle only what we
        # can account for rather than inventing a short).
        net_shares = max(0.0, g["buy_sh"] - g["sell_sh"])
        payout_per_share = 1.0 if outcome == market.resolved_outcome else 0.0
        payout = net_shares * payout_per_share
        realized = g["sell_size"] + payout - g["buy_size"]
        out.append(
            WalletPosition(
                market_id=market_id,
                outcome=outcome,
                realized_pnl=round(realized, 2),
                size=round(g["buy_size"], 2),
                timestamp=g["last_ts"],
                market=market,
            )
        )
    return out
