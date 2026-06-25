"""Tests for the TOP 20 paper-strategy lab: Kelly sizing, clamping, caps,
duplicate prevention, independent evaluation, API shape, and paper-only safety."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app import top20
from app.models import (
    Market,
    PaperSignal,
    Top20Strategy,
    Top20Trade,
    Wallet,
    WalletCandidate,
    WalletStat,
)
from app.top20 import StrategyConfig, estimate_probability, kelly_stake


# --- pure: Kelly sizing -----------------------------------------------------
DEFAULT = top20.CONFIG_BY_KEY["balanced"]


def test_kelly_fraction_formula():
    # price 0.5, p 0.6 -> b=1, q=0.4, kelly=(1*0.6-0.4)/1 = 0.20
    res = kelly_stake(0.5, 0.6, 10_000, DEFAULT)
    assert abs(res.kelly_fraction - 0.20) < 1e-9
    assert res.stake is not None and res.stake > 0


def test_kelly_non_positive_is_skipped():
    # price 0.6, p 0.5 -> negative edge -> Kelly <= 0 -> skip, no negative stake
    res = kelly_stake(0.6, 0.5, 10_000, DEFAULT)
    assert res.stake is None
    assert res.kelly_fraction <= 0
    assert res.shares == 0.0


def test_zero_price_no_division_by_zero():
    res = kelly_stake(0.0, 0.5, 10_000, DEFAULT)  # price clamped to 0.01
    assert res.stake is not None
    assert res.stake >= DEFAULT.min_bet


def test_price_one_is_clamped():
    res = kelly_stake(1.0, 0.99, 10_000, DEFAULT)  # price clamped to 0.99
    # should not raise and not produce a negative stake
    assert res.stake is None or res.stake >= 0


def test_stake_never_exceeds_caps():
    # huge edge would want a big bet; caps must hold
    res = kelly_stake(0.5, 0.99, 10_000, DEFAULT)
    assert res.stake is not None
    pos_cap = min(DEFAULT.max_bet, DEFAULT.max_position_pct * 10_000)
    assert res.stake <= pos_cap + 1e-9
    assert res.stake <= DEFAULT.max_bet


def test_min_bet_floor():
    res = kelly_stake(0.5, 0.51, 10_000, DEFAULT)  # tiny positive edge
    if res.stake is not None:
        assert res.stake >= DEFAULT.min_bet


def test_market_exposure_cap_blocks_when_full():
    # market exposure already at the 10% cap -> no room -> skip
    used = DEFAULT.max_market_exposure_pct * 10_000  # = 1000
    res = kelly_stake(0.5, 0.7, 10_000, DEFAULT, market_exposure_used=used)
    assert res.stake is None


def test_market_exposure_cap_limits_stake():
    used = 900.0  # cap is 1000 -> only 100 room
    res = kelly_stake(0.5, 0.9, 10_000, DEFAULT, market_exposure_used=used)
    assert res.stake is not None
    assert res.stake <= 100 + 1e-9


# --- pure: probability estimation / clamping --------------------------------
def test_probability_uses_price_plus_edge_and_clamps_high():
    assert estimate_probability(0.5, 0.9, None, None) == 0.99  # 1.4 -> clamp


def test_probability_clamps_low():
    assert estimate_probability(0.5, -0.9, None, None) == 0.01  # -0.4 -> clamp


def test_probability_blend_when_no_edge():
    p = estimate_probability(0.6, None, win_rate=0.8, confidence=70)
    # 0.5*0.8 + 0.3*0.7 + 0.2*0.6 = 0.4 + 0.21 + 0.12 = 0.73
    assert abs(p - 0.73) < 1e-9


def test_probability_handles_garbage_inputs():
    p = estimate_probability(None, None, None, None)
    assert 0.01 <= p <= 0.99


# --- DB: seeding + evaluation ----------------------------------------------
def _seed_max_signal(db, *, price=0.5, conf=95.0, edge=0.12, liquidity=20_000.0,
                     classification="good_candidate", win_rate=0.8, age_min=5):
    """A signal that passes EVERY strategy filter, from the #1-ranked wallet."""
    w = Wallet(address="0xtopwallet", copy_enabled=True)
    db.add(w)
    db.flush()
    db.add(WalletStat(wallet_id=w.id, num_trades=300, score=80.0, win_rate=win_rate,
                      classification="sharp", num_settled=200))
    db.add(WalletCandidate(wallet_id=w.id, copyability_score=90.0,
                           classification=classification))
    db.add(Market(id="0xmktA", question="Will X happen?", outcomes=["Yes", "No"],
                  prices=[price, 1 - price], liquidity=liquidity, resolved=False))
    db.flush()
    sig = PaperSignal(
        wallet_id=w.id, market_id="0xmktA", outcome="Yes", side="buy",
        observed_price=price, suggested_entry=price, confidence=conf,
        edge_estimate=edge, reason="test",
        created_at=datetime.utcnow() - timedelta(minutes=age_min),
    )
    db.add(sig)
    db.commit()
    return sig


def test_creates_exactly_20_strategies(in_memory_db):
    strategies = top20.ensure_strategies(in_memory_db)
    assert len(strategies) == 20
    # idempotent
    assert len(top20.ensure_strategies(in_memory_db)) == 20
    keys = {s.key for s in strategies}
    assert len(keys) == 20  # all distinct


def test_each_strategy_independently_evaluates_same_signal(in_memory_db):
    db = in_memory_db
    _seed_max_signal(db)
    summary = top20.evaluate_signals(db)
    assert summary["signals"] == 1
    trades = db.scalars(select(Top20Trade)).all()
    # a maximal signal passes all 20 filters -> 20 independent entries
    assert len(trades) == 20
    # each strategy entered it exactly once
    per_strat = {}
    for t in trades:
        per_strat[t.strategy_id] = per_strat.get(t.strategy_id, 0) + 1
    assert all(c == 1 for c in per_strat.values())
    assert len(per_strat) == 20


def test_duplicate_signal_not_entered_twice(in_memory_db):
    db = in_memory_db
    _seed_max_signal(db)
    top20.evaluate_signals(db)
    n1 = db.scalar(select(top20.func.count()).select_from(Top20Trade))
    # re-running must not create duplicates (watermark + unique constraint)
    top20.evaluate_signals(db)
    n2 = db.scalar(select(top20.func.count()).select_from(Top20Trade))
    assert n1 == n2 == 20


def test_filters_exclude_signals(in_memory_db):
    db = in_memory_db
    # low confidence, low edge, illiquid, watchlist wallet -> many strategies skip
    _seed_max_signal(db, conf=50, edge=0.0, liquidity=100, classification="watchlist")
    top20.evaluate_signals(db)
    entered = {db.get(Top20Strategy, t.strategy_id).key
               for t in db.scalars(select(Top20Trade)).all()}
    # confidence/edge/liquidity gated strategies must NOT have entered
    for k in ("conf70", "conf80", "conf90", "edge3", "edge5", "edge10",
              "liq1k", "liq5k", "liq10k", "good_only"):
        assert k not in entered


def test_market_exposure_cap_enforced_across_signals(in_memory_db):
    db = in_memory_db
    # many signals on the SAME market; balanced strategy must not exceed 10% cap
    w = Wallet(address="0xw", copy_enabled=True)
    db.add(w); db.flush()
    db.add(WalletStat(wallet_id=w.id, num_trades=300, score=80, win_rate=0.8, num_settled=200))
    db.add(WalletCandidate(wallet_id=w.id, copyability_score=90, classification="good_candidate"))
    db.add(Market(id="0xmkt", question="Q", outcomes=["Yes", "No"], prices=[0.5, 0.5],
                  liquidity=50_000, resolved=False))
    db.flush()
    for i in range(40):
        db.add(PaperSignal(wallet_id=w.id, market_id="0xmkt", outcome="Yes", side="buy",
                           observed_price=0.5, suggested_entry=0.5, confidence=95,
                           edge_estimate=0.2, reason="x", created_at=datetime.utcnow()))
    db.commit()
    top20.evaluate_signals(db)
    strat = db.scalar(select(Top20Strategy).where(Top20Strategy.key == "balanced"))
    exposure = sum(t.stake for t in db.scalars(
        select(Top20Trade).where(Top20Trade.strategy_id == strat.id)).all())
    cap = 0.10 * strat.starting_bankroll
    assert exposure <= cap + 1e-6


def test_settlement_computes_realized_pnl(in_memory_db):
    db = in_memory_db
    _seed_max_signal(db, price=0.5)
    top20.evaluate_signals(db)
    # resolve the market in favour of "Yes"
    m = db.get(Market, "0xmktA")
    m.resolved = True
    m.resolved_outcome = "Yes"
    db.commit()
    top20.settle_and_mark(db)
    closed = db.scalars(select(Top20Trade).where(Top20Trade.status == "closed")).all()
    assert len(closed) == 20
    for t in closed:
        # bought "Yes" at 0.5 and it won -> shares*1 - stake = stake (since shares=stake/0.5)
        assert t.realized_pnl > 0
        assert abs(t.realized_pnl - (t.size_shares * 1.0 - t.stake)) < 0.01


def test_api_returns_20_strategies_and_is_paper_only(in_memory_db):
    from app import main
    payload = main.top20_strategies(db=in_memory_db)
    assert payload["paper_only"] is True
    assert len(payload["strategies"]) == 20
    for s in payload["strategies"]:
        assert s["paper_only"] is True
        # required display fields are present
        for field in ("name", "description", "active", "bankroll", "open_positions",
                      "closed_positions", "total_pnl", "realized_pnl", "unrealized_pnl",
                      "win_rate", "avg_return_per_trade", "max_drawdown",
                      "signals_evaluated", "trades_entered", "last_trade_at"):
            assert field in s


def test_reset_paper_clears_trades(in_memory_db):
    db = in_memory_db
    _seed_max_signal(db)
    top20.evaluate_signals(db)
    assert db.scalar(select(top20.func.count()).select_from(Top20Trade)) == 20
    top20.reset_paper(db)
    assert db.scalar(select(top20.func.count()).select_from(Top20Trade)) == 0
    for s in db.scalars(select(Top20Strategy)).all():
        assert s.last_signal_id == 0 and s.trades_entered == 0
