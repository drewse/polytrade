"""Guarded bankroll re-baseline: aligns the local bankroll baseline with venue
reality WITHOUT changing trading behavior. Writes only bankroll + starting_bankroll;
preserves realized P/L, open positions, executions and history."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select

from app import live
from app.models import LiveExecution, LiveState, Market


def _seed(db, *, starting=41.41, bankroll=24.78):
    db.add(LiveState(id=1, starting_bankroll=starting, bankroll=bankroll, halted=False))
    db.add(Market(id="mopen", question="Q", outcomes=["Yes", "No"], token_ids=["t1", "t2"],
                  prices=[0.5, 0.5], resolved=False))
    # one OPEN position (cost basis $8) + one CLOSED with realized -$16.63
    db.add(LiveExecution(idempotency_key="o1", executor="polymarket", strategy_key="s",
                         wallet_address="0xa", market_id="mopen", outcome="Yes", expected_price=0.5,
                         fill_price=0.5, size_usd=8.0, shares=16.0, status="open", order_id="0x1"))
    db.add(Market(id="mclosed", question="Q", outcomes=["Yes", "No"], token_ids=["t1", "t2"],
                  prices=[1.0, 0.0], resolved=True, resolved_outcome="No"))
    db.add(LiveExecution(idempotency_key="c1", executor="polymarket", strategy_key="s",
                         wallet_address="0xb", market_id="mclosed", outcome="Yes", expected_price=0.5,
                         fill_price=0.5, size_usd=16.63, shares=33.0, status="closed",
                         realized_pnl=-16.63, order_id="0x2"))
    db.commit()


_VENUE = lambda **_: {"available_usdc": 188.94, "source": "test", "error": None}


def test_rebaseline_sets_bankroll_to_venue_plus_exposure(in_memory_db):
    db = in_memory_db
    _seed(db)
    out = live.rebaseline_bankroll(db, confirm=True, balance_fn=_VENUE)
    assert out["ok"] is True
    st = live.get_state(db)
    # bankroll = venue_cash (188.94) + open_exposure (8.00)
    assert st.bankroll == 196.94 and out["new_bankroll"] == 196.94
    # starting_bankroll = bankroll - realized_pnl (-16.63) -> 196.94 + 16.63
    assert st.starting_bankroll == 213.57 and out["new_starting_bankroll"] == 213.57
    # the invariant bankroll == starting + realized holds
    assert round(st.starting_bankroll + live._realized_total(db), 2) == st.bankroll


def test_realized_pnl_and_positions_preserved(in_memory_db):
    db = in_memory_db
    _seed(db)
    n_exec = db.scalar(select(func.count()).select_from(LiveExecution))
    open_before = [e.size_usd for e in live._open_active(db)]
    realized_before = live._realized_total(db)
    live.rebaseline_bankroll(db, confirm=True, balance_fn=_VENUE)
    # executions, open positions, fill prices, realized P/L all unchanged
    assert db.scalar(select(func.count()).select_from(LiveExecution)) == n_exec
    assert [e.size_usd for e in live._open_active(db)] == open_before
    assert live._realized_total(db) == realized_before == -16.63
    closed = db.scalar(select(LiveExecution).where(LiveExecution.idempotency_key == "c1"))
    assert closed.realized_pnl == -16.63 and closed.fill_price == 0.5    # untouched
    assert out_no_orders(db)


def out_no_orders(db) -> bool:
    # no execution was created by the re-baseline
    return db.scalar(select(func.count()).select_from(LiveExecution)) == 2


def test_refuses_without_confirm(in_memory_db):
    db = in_memory_db
    _seed(db)
    out = live.rebaseline_bankroll(db, confirm=False, balance_fn=_VENUE)
    assert out["ok"] is False and "confirm" in out["error"].lower()
    assert live.get_state(db).bankroll == 24.78          # unchanged


def test_refuses_when_venue_balance_unavailable(in_memory_db):
    db = in_memory_db
    _seed(db)
    out = live.rebaseline_bankroll(db, confirm=True,
                                   balance_fn=lambda **_: {"available_usdc": None, "error": "sdk down"})
    assert out["ok"] is False and "unavailable" in out["error"].lower()
    assert live.get_state(db).bankroll == 24.78          # unchanged


def test_no_order_created(in_memory_db):
    db = in_memory_db
    _seed(db)
    before = db.scalar(select(func.count()).select_from(LiveExecution))
    live.rebaseline_bankroll(db, confirm=True, balance_fn=_VENUE)
    assert db.scalar(select(func.count()).select_from(LiveExecution)) == before


def test_reconcile_account_includes_rebaseline_recommendation_on_drift(in_memory_db):
    db = in_memory_db
    _seed(db)
    out = live.reconcile_account(db, client=type("C", (), {"get_markets_by_conditions": lambda *a, **k: []})(),
                                 balance_fn=_VENUE)
    rec = out["rebaseline_recommendation"]
    assert rec is not None and abs(out["drift"]) > 1.0
    assert rec["old_bankroll"] == 24.78 and rec["proposed_bankroll"] == 196.94
    assert rec["proposed_starting_bankroll"] == 213.57 and rec["realized_pnl"] == -16.63


def test_no_recommendation_when_reconciled(in_memory_db):
    db = in_memory_db
    # bankroll already aligned: venue 8.00 cash + open 8.00 exposure, realized 0
    db.add(LiveState(id=1, starting_bankroll=16.0, bankroll=16.0, halted=False))
    db.add(Market(id="mopen", question="Q", outcomes=["Yes", "No"], token_ids=["t1", "t2"],
                  prices=[0.5, 0.5], resolved=False))
    db.add(LiveExecution(idempotency_key="o1", executor="polymarket", strategy_key="s",
                         wallet_address="0xa", market_id="mopen", outcome="Yes", expected_price=0.5,
                         fill_price=0.5, size_usd=8.0, shares=16.0, status="open", order_id="0x1"))
    db.commit()
    out = live.reconcile_account(db, client=type("C", (), {"get_markets_by_conditions": lambda *a, **k: []})(),
                                 balance_fn=lambda **_: {"available_usdc": 8.0, "source": "t", "error": None})
    assert out["rebaseline_recommendation"] is None      # drift ~0 -> no recommendation
