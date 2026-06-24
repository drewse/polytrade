from types import SimpleNamespace

from app import paper_trading as pt


def test_position_sizing():
    assert pt.position_size(10_000, 1.0) == 100.0
    assert pt.position_size(10_000, 2.5) == 250.0


def test_slippage_buy_worse_sell_better():
    assert pt.apply_slippage(0.50, "buy", 2.0) == 0.52
    assert pt.apply_slippage(0.50, "sell", 2.0) == 0.48


def test_slippage_clamped():
    assert pt.apply_slippage(0.99, "buy", 5.0) <= 0.99
    assert pt.apply_slippage(0.01, "sell", 5.0) >= 0.01


def test_effective_slippage_scales_on_thin_markets():
    base = 1.5
    assert pt.effective_slippage_cents(base, 5000) == base       # deep market: unchanged
    thin = pt.effective_slippage_cents(base, 200)                # thin market: worse
    assert thin > base
    assert pt.effective_slippage_cents(base, 1) <= base * 8 + 1e-6  # capped at 8x


def test_market_exposure_and_caps():
    positions = [
        SimpleNamespace(market_id="m1", size=100, status="open"),
        SimpleNamespace(market_id="m1", size=150, status="open"),
        SimpleNamespace(market_id="m2", size=200, status="open"),
    ]
    assert pt.market_exposure(positions, "m1") == 250
    risk = pt.RiskConfig(bankroll=10_000, max_position_pct=1.0,
                         max_market_exposure_pct=5.0, slippage_cents=1.5, min_confidence=50)
    # cap = 5% of 10k = 500; current m1 = 250; adding 300 -> 550 > 500 -> blocked
    ok, why = pt.can_open(risk, 80, positions, "m1", 300)
    assert not ok and "exposure" in why
    # adding 200 -> 450 <= 500 -> allowed
    ok, _ = pt.can_open(risk, 80, positions, "m1", 200)
    assert ok


def test_confidence_gate():
    risk = pt.RiskConfig(bankroll=10_000, max_position_pct=1.0,
                         max_market_exposure_pct=5.0, slippage_cents=1.5, min_confidence=60)
    ok, why = pt.can_open(risk, 55, [], "m1", 100)
    assert not ok and "confidence" in why


def test_mark_to_market_and_realized():
    pos = SimpleNamespace(size=100, shares=250.0)  # entered 250 shares at 0.40
    # price rises to 0.50 -> value 125 -> +25
    assert pt.mark_to_market(pos, 0.50) == 25.0
    # resolves to 1.0 -> value 250 -> +150
    assert pt.realized_on_close(pos, 1.0) == 150.0
    # resolves to 0.0 -> -100
    assert pt.realized_on_close(pos, 0.0) == -100.0
