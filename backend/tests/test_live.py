"""Live execution validation tests: sizing, hard limits, halt latch, exposure
caps, duplicate prevention, safety switch, settlement, reconciliation."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app import live
from app.models import LiveExecution, LiveState, Market


@pytest.fixture
def cfg_enabled(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("LIVE_EXECUTOR", "dry_run")
    monkeypatch.setenv("LIVE_STARTING_BANKROLL", "100.0")
    return live.get_config()


# --- sizing (pure, conservative, NOT Kelly) --------------------------------
def test_conservative_stake_is_2pct():
    cfg = live.get_config()
    assert live.conservative_stake(100.0, cfg) == 2.0          # 2% of 100
    assert live.conservative_stake(250.0, cfg) == 5.0          # 2% of 250


def test_stake_capped_by_wallet_and_market_exposure():
    cfg = live.get_config()
    # wallet cap 10% of 100 = 10; already 9 used -> only 1 room
    assert live.conservative_stake(100.0, cfg, wallet_exposure=9.0) == 1.0
    # market cap 5% of 100 = 5; already 4.5 used -> 0.5 room < min_stake -> None
    assert live.conservative_stake(100.0, cfg, market_exposure=4.5) is None


def test_stake_none_when_no_bankroll():
    assert live.conservative_stake(0.0, live.get_config()) is None


# --- safety switch ----------------------------------------------------------
def test_disabled_by_default_blocks_orders(in_memory_db, monkeypatch):
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    cfg = live.get_config()
    assert cfg.enabled is False
    ok, reason = live.check_can_open(in_memory_db, cfg, wallet="0xw", market_id="m")
    assert not ok and "false" in reason.lower()


def test_disabled_process_signal_logs_rejected(in_memory_db, monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    out = live.process_signal(in_memory_db, strategy_key="s", wallet="0xw", signal_id=1,
                              market_id="m", market_question="Q", outcome="Yes", price=0.5,
                              entry_reason="t")
    assert out is None
    ex = in_memory_db.scalar(select(LiveExecution))
    assert ex.status == "rejected" and ex.size_usd == 0.0


# --- order placement + forensics + idempotency ------------------------------
def test_dry_run_places_and_logs_forensics(in_memory_db, cfg_enabled):
    ex = live.process_signal(in_memory_db, strategy_key="top_decile_edge", wallet="0xw",
                             signal_id=10, market_id="m1", market_question="Q", outcome="Yes",
                             price=0.5, entry_reason="copy 0xw")
    assert ex is not None and ex.status == "open"
    assert ex.size_usd == 2.0 and ex.shares == 4.0           # 2% of 100, 2/0.5
    assert ex.expected_price == 0.5 and ex.fill_price == 0.5 and ex.slippage == 0.0
    assert ex.bankroll_before == 100.0
    for f in ("order_latency_ms", "confirm_latency_ms"):
        assert getattr(ex, f) is not None


def test_duplicate_signal_not_ordered_twice(in_memory_db, cfg_enabled):
    a = live.process_signal(in_memory_db, strategy_key="s", wallet="0xw", signal_id=7,
                            market_id="m", market_question="Q", outcome="Yes", price=0.5,
                            entry_reason="t")
    b = live.process_signal(in_memory_db, strategy_key="s", wallet="0xw", signal_id=7,
                            market_id="m", market_question="Q", outcome="Yes", price=0.5,
                            entry_reason="t")
    assert a is not None and b is None
    assert in_memory_db.scalar(select(LiveState))  # state created
    assert len(in_memory_db.scalars(select(LiveExecution)).all()) == 1


# --- hard limits ------------------------------------------------------------
def test_max_simultaneous_positions(in_memory_db, cfg_enabled):
    for i in range(5):
        in_memory_db.add(LiveExecution(idempotency_key=f"k{i}", strategy_key="s",
                                       wallet_address=f"0x{i}", market_id=f"m{i}", outcome="Yes",
                                       expected_price=0.5, size_usd=2.0, status="open",
                                       bankroll_before=100))
    in_memory_db.commit()
    ok, reason = live.check_can_open(in_memory_db, cfg_enabled, wallet="0xnew", market_id="mn")
    assert not ok and "max simultaneous" in reason


def test_daily_loss_trips_halt_and_requires_manual_resume(in_memory_db, cfg_enabled):
    # closed losses today summing to -10% of 100 -> daily limit
    in_memory_db.add(LiveExecution(idempotency_key="l1", strategy_key="s", wallet_address="0xw",
                                   market_id="m", outcome="Yes", expected_price=0.5, size_usd=10,
                                   status="closed", realized_pnl=-10.0, closed_at=datetime.utcnow(),
                                   bankroll_before=100))
    in_memory_db.commit()
    ok, reason = live.check_can_open(in_memory_db, cfg_enabled, wallet="0xw", market_id="m")
    assert not ok and "halt" in reason.lower()
    st = live.get_state(in_memory_db)
    assert st.halted and "daily" in st.halt_reason
    # blocked until manual resume
    ok2, _ = live.check_can_open(in_memory_db, cfg_enabled, wallet="0xw", market_id="m2")
    assert not ok2
    live.resume(in_memory_db)
    assert not live.get_state(in_memory_db).halted


def test_weekly_loss_trips_halt(in_memory_db, cfg_enabled):
    in_memory_db.add(LiveExecution(idempotency_key="w1", strategy_key="s", wallet_address="0xw",
                                   market_id="m", outcome="Yes", expected_price=0.5, size_usd=20,
                                   status="closed", realized_pnl=-20.0,
                                   closed_at=datetime.utcnow() - timedelta(days=2), bankroll_before=100))
    in_memory_db.commit()
    ok, reason = live.check_can_open(in_memory_db, cfg_enabled, wallet="0xw", market_id="m")
    assert not ok and live.get_state(in_memory_db).halted


# --- settlement + accounting ------------------------------------------------
def test_settlement_updates_pnl_and_bankroll(in_memory_db, cfg_enabled):
    in_memory_db.add(Market(id="ms", question="Q", outcomes=["Yes", "No"], prices=[1.0, 0.0],
                            resolved=True, resolved_outcome="Yes"))
    in_memory_db.flush()
    live.get_state(in_memory_db)  # init bankroll 100
    ex = live.process_signal(in_memory_db, strategy_key="s", wallet="0xw", signal_id=1,
                             market_id="ms", market_question="Q", outcome="Yes", price=0.5,
                             entry_reason="t")
    assert ex.status == "open"
    res = live.settle_live(in_memory_db)
    assert res["closed"] == 1
    ex2 = in_memory_db.get(LiveExecution, ex.id)
    # bought 4 shares @0.5 ($2), Yes wins -> payout 4, pnl = 4 - 2 = +2
    assert ex2.status == "closed" and ex2.realized_pnl == 2.0
    assert ex2.bankroll_after == 102.0 and live.get_state(in_memory_db).bankroll == 102.0


# --- reconciliation ---------------------------------------------------------
def test_reconciliation_detects_drift(in_memory_db, cfg_enabled):
    live.get_state(in_memory_db)  # start 100, no trades
    good = live.reconcile(in_memory_db, reported_balance=100.0)
    assert good["reconciled"] and good["drift"] == 0.0
    bad = live.reconcile(in_memory_db, reported_balance=95.0)
    assert not bad["reconciled"] and bad["drift"] == -5.0


# --- real submission is a guarded stub --------------------------------------
def test_polymarket_executor_refuses_until_implemented():
    with pytest.raises(NotImplementedError):
        live.PolymarketExecutor().place(market_id="m", outcome="Yes", side="buy",
                                        price=0.5, size_usd=2.0)


def test_status_reports_safety_state(in_memory_db, monkeypatch):
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    s = live.status(in_memory_db)
    assert s["live_trading_enabled"] is False
    assert s["real_submission_implemented"] is False
    assert s["sizing"]["method"] == "fixed_fractional" and s["sizing"]["position_pct"] == 0.02
    assert s["limits"]["max_positions"] == 5
