from app import backtest as bt
from tests.conftest import make_market, make_trade


def _world():
    markets = {
        "m1": make_market("m1", resolved=True, resolved_outcome="Yes"),
        "m2": make_market("m2", resolved=True, resolved_outcome="No"),
        "mopen": make_market("mopen", resolved=False, resolved_outcome=None),
    }
    trades = [
        make_trade(1, "m1", "Yes", 0.40, 200, days_ago=30),   # sharp buys winner
        make_trade(2, "m2", "Yes", 0.60, 200, days_ago=25),   # bad buys loser (No wins)
        make_trade(1, "m1", "Yes", 0.45, 8000, days_ago=20),  # whale on winning side
        make_trade(1, "mopen", "Yes", 0.50, 300, days_ago=15),  # unresolved -> unscored
    ]
    for t in trades:
        t.category = "Politics"
    wallet_class = {1: "sharp", 2: "bad"}
    wallet_score = {1: 80.0, 2: 20.0}
    return trades, markets, wallet_class, wallet_score


def test_other_outcome():
    assert bt.other_outcome(["Yes", "No"], "Yes") == "No"
    assert bt.other_outcome(["Yes", "No"], "No") == "Yes"
    assert bt.other_outcome(["A", "B", "C"], "A") is None  # not binary


def test_split_by_time_orders_and_splits():
    trades, *_ = _world()
    train, test = bt.split_by_time(trades, 0.5)
    assert len(train) + len(test) == len(trades)
    # train is strictly earlier than test
    assert max(t.timestamp for t in train) <= min(t.timestamp for t in test)


def test_no_trade_baseline_does_nothing():
    trades, markets, wc, ws = _world()
    params = bt.BacktestParams(strategies=["no_trade_baseline"])
    res = bt.replay(trades, markets, wc, ws, params)["no_trade_baseline"]
    assert res.num_trades == 0
    assert res.total_pnl == 0.0
    assert res.ending_bankroll == res.starting_bankroll


def test_copy_sharp_only_copies_sharp_and_profits():
    trades, markets, wc, ws = _world()
    params = bt.BacktestParams(strategies=["copy_sharp_wallets"], min_wallet_score=65)
    res = bt.replay(trades, markets, wc, ws, params)["copy_sharp_wallets"]
    # only wallet 1 (sharp) trades on resolved markets are copied -> 2 trades
    assert res.num_trades == 2
    assert all(t.wallet_id == 1 for t in res.trades)
    assert res.total_pnl > 0  # both were winners


def test_fade_losing_takes_opposite_and_profits():
    trades, markets, wc, ws = _world()
    params = bt.BacktestParams(strategies=["fade_losing_wallets"])
    res = bt.replay(trades, markets, wc, ws, params)["fade_losing_wallets"]
    assert res.num_trades == 1
    t = res.trades[0]
    assert t.outcome == "No"      # faded the bad wallet's "Yes"
    assert t.pnl > 0               # No won on m2


def test_whale_reversion_detects_and_loses_here():
    trades, markets, wc, ws = _world()
    params = bt.BacktestParams(strategies=["whale_shock_reversion"], whale_size=6000)
    res = bt.replay(trades, markets, wc, ws, params)["whale_shock_reversion"]
    assert res.num_trades == 1     # only the 8000-size trade
    assert res.trades[0].outcome == "No"   # reversion vs the whale's "Yes"
    assert res.trades[0].pnl < 0   # Yes actually won


def test_metrics_consistency_and_equity_curve():
    trades, markets, wc, ws = _world()
    params = bt.BacktestParams(strategies=["copy_sharp_wallets"])
    res = bt.replay(trades, markets, wc, ws, params)["copy_sharp_wallets"]
    assert abs(res.ending_bankroll - (res.starting_bankroll + res.total_pnl)) < 1e-6
    assert res.equity_curve[0]["equity"] == res.starting_bankroll
    assert len(res.equity_curve) == res.num_trades + 1
    assert 0.0 <= res.win_rate <= 1.0
    assert res.max_drawdown >= 0.0


def test_unresolved_markets_are_not_scored():
    # a sharp trade on an unresolved market must not produce a backtest trade
    trades, markets, wc, ws = _world()
    params = bt.BacktestParams(strategies=["copy_sharp_wallets"])
    res = bt.replay(trades, markets, wc, ws, params)["copy_sharp_wallets"]
    assert all(t.market_id != "mopen" for t in res.trades)
