from types import SimpleNamespace

from app import signals
from app.signals import SignalRules, detect_signals, estimate_edge


def _rules(**kw):
    base = dict(min_wallet_score=65, min_trade_count=20, min_trade_size=50,
                min_market_liquidity=1000, max_price_staleness_min=10_000,
                min_volume=0, min_edge=-1.0)
    base.update(kw)
    return SignalRules(**base)


def _wallet(wid=1, copy=True):
    return SimpleNamespace(id=wid, address="0xabc", label="w", copy_enabled=copy)


def _stat(score=80, n=50, win=0.7, cls="sharp", cat=None):
    return SimpleNamespace(wallet_id=1, score=score, num_trades=n, win_rate=win,
                           realized_roi=0.2, classification=cls, category_performance=cat or {})


def _market(mid="m1", liq=5000, vol=10000, resolved=False, cat="Politics"):
    return SimpleNamespace(id=mid, liquidity=liq, volume=vol, resolved=resolved, category=cat)


def _trade(wid=1, mid="m1", outcome="Yes", price=0.4, size=200, mins_ago=1):
    from datetime import datetime, timedelta, timezone
    return SimpleNamespace(id=1, wallet_id=wid, market_id=mid, outcome=outcome, side="buy",
                           price=price, size=size,
                           timestamp=datetime.now(timezone.utc) - timedelta(minutes=mins_ago))


def test_edge_estimate():
    assert estimate_edge(0.7, 0.4) == 0.3
    assert estimate_edge(0.3, 0.6) == -0.3


def test_sharp_wallet_generates_signal():
    sigs = detect_signals([_trade()], {1: _wallet()}, {1: _stat()}, {"m1": _market()}, _rules())
    assert len(sigs) == 1
    assert sigs[0].outcome == "Yes"
    assert sigs[0].confidence >= 65


def test_low_score_blocked():
    sigs = detect_signals([_trade()], {1: _wallet()}, {1: _stat(score=40, cls="neutral")},
                          {"m1": _market()}, _rules())
    assert sigs == []


def test_small_trade_blocked():
    sigs = detect_signals([_trade(size=10)], {1: _wallet()}, {1: _stat()}, {"m1": _market()}, _rules())
    assert sigs == []


def test_illiquid_market_blocked():
    sigs = detect_signals([_trade()], {1: _wallet()}, {1: _stat()}, {"m1": _market(liq=100)}, _rules())
    assert sigs == []


def test_resolved_market_blocked():
    sigs = detect_signals([_trade()], {1: _wallet()}, {1: _stat()},
                          {"m1": _market(resolved=True)}, _rules())
    assert sigs == []


def test_copy_disabled_blocked():
    sigs = detect_signals([_trade()], {1: _wallet(copy=False)}, {1: _stat()},
                          {"m1": _market()}, _rules())
    assert sigs == []


def test_min_edge_gate():
    # win_rate 0.55, price 0.5 -> edge 0.05; require 0.2 -> blocked
    sigs = detect_signals([_trade(price=0.5)], {1: _wallet()}, {1: _stat(win=0.55)},
                          {"m1": _market()}, _rules(min_edge=0.2))
    assert sigs == []


def test_multiple_sharps_boost_confidence():
    trades = [_trade(), SimpleNamespace(id=2, wallet_id=2, market_id="m1", outcome="Yes",
              side="buy", price=0.4, size=200, timestamp=_trade().timestamp)]
    wallets = {1: _wallet(1), 2: _wallet(2)}
    stats = {1: _stat(), 2: _stat()}
    stats[2].wallet_id = 2
    sigs = detect_signals(trades, wallets, stats, {"m1": _market()}, _rules())
    assert any("sharp wallets entered" in s.reason for s in sigs)
