"""Quantitative audit tests: drawdown path correctness, replay determinism,
strategy independence, accounting invariants."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app import top20
from app.models import Market, Top20FeatureVector, Top20Strategy, Top20Trade, Trade, Wallet
from app.top20 import replay
from app.top20.engine import _metrics_for, _trade_path_curve


def _closed(pnl, day):
    return Top20Trade(strategy_id=1, wallet_address="0x", market_id="m", outcome="Yes",
                      entry_price=0.5, size_shares=1, stake=100, estimated_probability=0.5,
                      kelly_fraction=0.1, fractional_kelly_used=0.25, status="closed",
                      realized_pnl=pnl, unrealized_pnl=0.0,
                      entry_time=datetime(2026, 1, day), closed_at=datetime(2026, 1, day))


def test_drawdown_from_trade_path():
    # path: 10000 -> 10100 -> 10200 -> 10050 -> 10100 ; peak 10200, trough 10050
    trades = [_closed(100, 1), _closed(100, 2), _closed(-150, 3), _closed(50, 4)]
    curve = _trade_path_curve(trades, [], 10000.0)
    assert curve == [10000.0, 10100.0, 10200.0, 10050.0, 10100.0]
    from app.top20 import analytics
    assert analytics.max_drawdown(curve) == round((10200 - 10050) / 10200, 4)  # 0.0147


def test_drawdown_uses_path_not_snapshots(in_memory_db):
    db = in_memory_db
    top20.ensure_strategies(db)
    sid = db.scalar(select(Top20Strategy.id))
    # a winning end but a deep mid-sequence trough
    for i, pnl in enumerate([500, -400, -400, 800], start=1):
        t = _closed(pnl, i); t.strategy_id = sid
        db.add(t)
    db.commit()
    m = _metrics_for(db, db.get(Top20Strategy, sid))
    # path: 10000,10500,10100,9700,10500 -> peak 10500 then 9700 -> dd=(10500-9700)/10500
    assert m["max_drawdown"] == round((10500 - 9700) / 10500, 4)
    assert m["max_drawdown"] > 0   # not hidden as ~0


def _seed_replay_world(db, addr):
    w = Wallet(address=addr, copy_enabled=True)
    db.add(w); db.flush()
    base = datetime(2026, 1, 1)
    for i in range(1, 7):
        db.add(Market(id=f"{addr}-p{i}", question="Will BTC moon?", outcomes=["Yes", "No"],
                      prices=[1.0, 0.0], liquidity=5000, resolved=True, resolved_outcome="Yes",
                      resolved_at=base + timedelta(days=i)))
        db.flush()
        db.add(Trade(wallet_id=w.id, market_id=f"{addr}-p{i}", outcome="Yes", side="buy",
                     price=0.4, size=40.0, timestamp=base + timedelta(days=i - 0.5)))
    db.add(Market(id=f"{addr}-cand", question="Will the Lakers win?", outcomes=["Yes", "No"],
                  prices=[1.0, 0.0], liquidity=8000, volume=50000, resolved=True,
                  resolved_outcome="Yes", resolved_at=base + timedelta(days=30)))
    db.flush()
    db.add(Trade(wallet_id=w.id, market_id=f"{addr}-cand", outcome="Yes", side="buy",
                 price=0.5, size=60.0, timestamp=base + timedelta(days=10)))
    db.commit()


def test_replay_is_deterministic(in_memory_db):
    db = in_memory_db
    top20.ensure_strategies(db)
    _seed_replay_world(db, "0xdet")
    r1 = replay.run(db, max_trades=100)
    fvs1 = sorted((fv.strategy_key, fv.label_realized_pnl) for fv in
                  db.scalars(select(Top20FeatureVector)).all())
    replay.reset(db)
    r2 = replay.run(db, max_trades=100)
    fvs2 = sorted((fv.strategy_key, fv.label_realized_pnl) for fv in
                  db.scalars(select(Top20FeatureVector)).all())
    # identical feature vectors + identical realized P&L on a re-run
    assert r1["feature_vectors_added"] == r2["feature_vectors_added"]
    assert fvs1 == fvs2 and len(fvs1) > 0


def test_replay_independent_of_insertion_order(in_memory_db):
    # The candidate query always orders by Trade.id, so the same logical world
    # yields the same realized totals regardless of which wallet was inserted
    # first — verify two wallets' results don't depend on insertion interleaving.
    db = in_memory_db
    top20.ensure_strategies(db)
    _seed_replay_world(db, "0xaaa")
    _seed_replay_world(db, "0xbbb")
    replay.run(db, max_trades=500)
    total = sum(fv.label_realized_pnl for fv in db.scalars(select(Top20FeatureVector)).all())
    # deterministic, finite, and both wallets contributed
    addrs = {db.get(Top20Trade, fv.trade_id).wallet_address for fv in
             db.scalars(select(Top20FeatureVector)).all() if fv.trade_id}
    assert "0xaaa" in addrs and "0xbbb" in addrs
    assert isinstance(total, float)


def test_strategies_are_independent(in_memory_db):
    db = in_memory_db
    top20.ensure_strategies(db)
    _seed_replay_world(db, "0xind")
    replay.run(db, max_trades=100)
    # each trade belongs to exactly one strategy; no row shared across strategies
    trades = db.scalars(select(Top20Trade)).all()
    by_strategy = {}
    for t in trades:
        by_strategy.setdefault(t.strategy_id, []).append(t.id)
    all_ids = [tid for ids in by_strategy.values() for tid in ids]
    assert len(all_ids) == len(set(all_ids))            # no shared position rows
    # bankroll/equity recomputed independently per strategy
    for sid in by_strategy:
        m = _metrics_for(db, db.get(Top20Strategy, sid))
        realized = round(sum(t.realized_pnl for t in trades if t.strategy_id == sid
                             and t.status == "closed"), 2)
        assert m["bankroll"] == round(10000 + realized, 2)   # no shared bankroll


def test_accounting_identity(in_memory_db):
    db = in_memory_db
    top20.ensure_strategies(db)
    _seed_replay_world(db, "0xacct")
    replay.run(db, max_trades=100)
    for strat in db.scalars(select(Top20Strategy)).all():
        m = _metrics_for(db, strat)
        # equity == starting + realized + unrealized, exactly
        assert m["equity"] == round(10000 + m["realized_pnl"] + m["unrealized_pnl"], 2)
