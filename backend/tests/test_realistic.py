"""Validation tests for the realistic capital-constrained replay (Issue 2)."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app import top20
from app.models import Market, Top20Strategy, Top20Trade, Trade, Wallet
from app.top20 import replay
from app.top20.engine import _metrics_for

BASE = datetime(2026, 1, 1)


def _qualify(db, w, prefix):
    """6 prior winning resolved positions so the wallet qualifies by day 10."""
    for i in range(1, 7):
        db.add(Market(id=f"{prefix}-p{i}", question="Will BTC moon?", outcomes=["Yes", "No"],
                      prices=[1.0, 0.0], liquidity=5000, volume=20000, resolved=True,
                      resolved_outcome="Yes", resolved_at=BASE + timedelta(days=i)))
        db.flush()
        db.add(Trade(wallet_id=w.id, market_id=f"{prefix}-p{i}", outcome="Yes", side="buy",
                     price=0.4, size=40.0, timestamp=BASE + timedelta(days=i - 0.5)))


def _candidate(db, w, mid, won=True, resolve_day=30, signal_day=10, price=0.5):
    db.add(Market(id=mid, question="Will the Lakers win?", outcomes=["Yes", "No"],
                  prices=[1.0, 0.0] if won else [0.0, 1.0], liquidity=8000, volume=50000,
                  resolved=True, resolved_outcome="Yes" if won else "No",
                  resolved_at=BASE + timedelta(days=resolve_day)))
    db.flush()
    db.add(Trade(wallet_id=w.id, market_id=mid, outcome="Yes", side="buy", price=price,
                 size=60.0, timestamp=BASE + timedelta(days=signal_day)))


def test_realistic_reconciles_and_never_negative(in_memory_db):
    db = in_memory_db
    top20.ensure_strategies(db)
    w = Wallet(address="0xr1", copy_enabled=True); db.add(w); db.flush()
    _qualify(db, w, "r1")
    _candidate(db, w, "r1-c1", won=True)
    _candidate(db, w, "r1-c2", won=False, signal_day=11, resolve_day=31)
    db.commit()
    res = top20.replay_run_realistic(db)
    assert res["mode"] == "realistic"
    for strat in db.scalars(select(Top20Strategy)).all():
        m = strat.realistic_metrics or {}
        if not m.get("trades"):
            continue
        trades = db.scalars(select(Top20Trade).where(
            Top20Trade.strategy_id == strat.id, Top20Trade.source == "realistic")).all()
        realized = round(sum(t.realized_pnl for t in trades), 2)
        # capital correctly released: final equity == start + realized
        assert m["final_equity"] == round(10000 + realized, 2)
        assert m["final_equity"] >= 0          # never negative
        for t in trades:
            assert t.stake <= 250 + 1e-6        # caps respected
            assert t.stake <= 10000             # no leverage


def test_realistic_is_deterministic(in_memory_db):
    db = in_memory_db
    top20.ensure_strategies(db)
    w = Wallet(address="0xr2", copy_enabled=True); db.add(w); db.flush()
    _qualify(db, w, "r2")
    _candidate(db, w, "r2-c1", won=True)
    db.commit()
    a = top20.replay_run_realistic(db)["strategies"]
    b = top20.replay_run_realistic(db)["strategies"]
    # identical realistic metrics on a re-run
    assert a == b


def test_realistic_capital_constraint_and_bankruptcy(in_memory_db):
    db = in_memory_db
    top20.ensure_strategies(db)
    w = Wallet(address="0xr3", copy_enabled=True); db.add(w); db.flush()
    _qualify(db, w, "r3")
    # 80 concurrent LOSING positions all opening day 10, resolving day 100 ->
    # capital must run out -> rejections, and equity must never go negative.
    for i in range(80):
        _candidate(db, w, f"r3-c{i}", won=False, signal_day=10, resolve_day=100)
    db.commit()
    top20.replay_run_realistic(db)
    saw_rejection = False
    for strat in db.scalars(select(Top20Strategy)).all():
        m = strat.realistic_metrics or {}
        if not m:
            continue
        assert m["final_equity"] >= 0                       # bankruptcy protection
        # NO LEVERAGE: committed capital never exceeds equity at any instant
        assert m["peak_capital_utilization"] <= 1.0 + 1e-6
        if m.get("rejected_trades", 0) > 0:
            saw_rejection = True
    assert saw_rejection   # some strategy ran out of capital


def test_notional_metrics_exclude_realistic(in_memory_db):
    db = in_memory_db
    top20.ensure_strategies(db)
    w = Wallet(address="0xr4", copy_enabled=True); db.add(w); db.flush()
    _qualify(db, w, "r4")
    _candidate(db, w, "r4-c1", won=True)
    db.commit()
    # notional replay first
    top20.replay_run(db, max_trades=100)
    sid = db.scalar(select(Top20Strategy.id))
    before = _metrics_for(db, db.get(Top20Strategy, sid))
    # realistic run must NOT change notional metrics
    top20.replay_run_realistic(db)
    after = _metrics_for(db, db.get(Top20Strategy, sid))
    assert before["realized_pnl"] == after["realized_pnl"]
    assert before["closed_positions"] == after["closed_positions"]
