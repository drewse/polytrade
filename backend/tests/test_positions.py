"""Tests for live position reconstruction + realized P&L (positions.py)."""
from __future__ import annotations

from app import positions
from tests.conftest import make_market, make_trade


def _markets(*ms):
    return {m.id: m for m in ms}


def test_winning_buy_held_to_resolution():
    # Buy 100 shares of "Yes" at 0.40 ($40 cost). "Yes" wins -> payout 100*1.0.
    trades = [make_trade(1, "m1", "Yes", 0.40, 40.0)]
    markets = _markets(make_market("m1", resolved_outcome="Yes"))
    [pos] = positions.settled_positions(trades, markets)
    assert pos.size == 40.0                 # cost basis
    assert pos.realized_pnl == 60.0         # 100*1.0 - 40
    assert pos.settled


def test_losing_buy_held_to_resolution():
    # Buy 100 shares of "No" at 0.40 ($40). "Yes" wins -> "No" pays 0.
    trades = [make_trade(1, "m1", "No", 0.40, 40.0)]
    markets = _markets(make_market("m1", resolved_outcome="Yes"))
    [pos] = positions.settled_positions(trades, markets)
    assert pos.realized_pnl == -40.0        # 0 - 40


def test_unresolved_market_is_not_settled():
    trades = [make_trade(1, "m1", "Yes", 0.40, 40.0)]
    markets = _markets(make_market("m1", resolved=False, resolved_outcome=None))
    assert positions.settled_positions(trades, markets) == []


def test_partial_sell_before_resolution():
    # Buy 100 @ 0.40 ($40), sell 40 shares @ 0.60 ($24 proceeds). 60 shares left.
    # "Yes" wins -> payout 60*1.0. realized = 24 + 60 - 40 = 44.
    trades = [
        make_trade(1, "m1", "Yes", 0.40, 40.0, days_ago=10, side="buy"),
        make_trade(1, "m1", "Yes", 0.60, 24.0, days_ago=5, side="sell"),
    ]
    markets = _markets(make_market("m1", resolved_outcome="Yes"))
    [pos] = positions.settled_positions(trades, markets)
    assert pos.realized_pnl == 44.0
    assert pos.size == 40.0                  # basis = total bought


def test_multiple_buys_average_into_one_position():
    # Two buys of the same (market, outcome) net into a single position.
    trades = [
        make_trade(1, "m1", "Yes", 0.40, 40.0),   # 100 sh
        make_trade(1, "m1", "Yes", 0.50, 50.0),   # 100 sh
    ]
    markets = _markets(make_market("m1", resolved_outcome="Yes"))
    settled = positions.settled_positions(trades, markets)
    assert len(settled) == 1                 # one position, not two
    assert settled[0].size == 90.0           # 40 + 50 cost basis
    assert settled[0].realized_pnl == 110.0  # 200*1.0 - 90


def test_sell_only_position_is_skipped():
    # No buy in the window -> no cost basis -> not settled (never fabricate P&L).
    trades = [make_trade(1, "m1", "Yes", 0.60, 24.0, side="sell")]
    markets = _markets(make_market("m1", resolved_outcome="Yes"))
    assert positions.settled_positions(trades, markets) == []


def test_min_settled_gate_needs_resolved_markets():
    # 10 buys but only 3 on resolved markets -> 3 settled positions.
    trades = [make_trade(1, f"r{i}", "Yes", 0.4, 40.0) for i in range(3)]
    trades += [make_trade(1, f"o{i}", "Yes", 0.4, 40.0) for i in range(7)]
    markets = _markets(*[make_market(f"r{i}", resolved_outcome="Yes") for i in range(3)])
    for i in range(7):
        markets[f"o{i}"] = make_market(f"o{i}", resolved=False, resolved_outcome=None)
    assert len(positions.settled_positions(trades, markets)) == 3
