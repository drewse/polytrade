"""Live execution test-layer validation: fixed-$ sizing, absolute caps, halt
latch, one-order mode, duplicate prevention, safety switch, settlement,
reconciliation, real-executor refuses without key."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app import live
from app.live import ExecutionRejected
from app.models import LiveExecution, LiveState, Market


def _market(db, mid="m1", resolved=False, outcome="Yes"):
    m = Market(id=mid, question="Q", outcomes=["Yes", "No"],
               token_ids=["tokenYES", "tokenNO"], prices=[0.5, 0.5],
               resolved=resolved, resolved_outcome=outcome if resolved else None)
    db.add(m); db.flush()
    return m


@pytest.fixture
def cfg_enabled(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("LIVE_EXECUTOR", "dry_run")
    monkeypatch.setenv("LIVE_STARTING_BANKROLL", "40.0")
    monkeypatch.setenv("LIVE_MAX_ORDERS", "0")    # unlimited unless a test sets it
    return live.get_config()


# --- sizing: tiny fixed dollar, absolute caps -------------------------------
def test_fixed_dollar_sizing():
    cfg = live.get_config()  # defaults: position_usd=2
    s = live.conservative_stake(cfg, available_cash=40, total_open=0,
                                wallet_exposure=0, market_exposure=0)
    assert s == 2.0


def test_sizing_capped_by_market_wallet_total_and_cash():
    cfg = live.get_config()
    # per-market cap $4, already $3 -> $1 room
    assert live.conservative_stake(cfg, available_cash=40, total_open=3,
                                   wallet_exposure=0, market_exposure=3) == 1.0
    # per-wallet cap $8, already $7.5 -> $0.5 < min_stake $1 -> None
    assert live.conservative_stake(cfg, available_cash=40, total_open=7.5,
                                   wallet_exposure=7.5, market_exposure=0) is None
    # available cash limits it
    assert live.conservative_stake(cfg, available_cash=1.5, total_open=0,
                                   wallet_exposure=0, market_exposure=0) == 1.5
    # total-risk cap $40 reached -> None
    assert live.conservative_stake(cfg, available_cash=40, total_open=40,
                                   wallet_exposure=0, market_exposure=0) is None


# --- safety switch ----------------------------------------------------------
def test_disabled_by_default(in_memory_db, monkeypatch):
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    cfg = live.get_config()
    assert cfg.enabled is False
    ok, reason = live.check_can_open(in_memory_db, cfg, wallet="0xw", market_id="m")
    assert not ok and "false" in reason.lower()


def test_disabled_process_logs_rejected(in_memory_db, monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    m = _market(in_memory_db)
    out = live.process_signal(in_memory_db, strategy_key="s", wallet="0xw", signal_id=1,
                              market=m, outcome="Yes", price=0.5, entry_reason="t")
    assert out is None
    ex = in_memory_db.scalar(select(LiveExecution))
    assert ex.status == "rejected"


# --- dry-run placement + forensics + idempotency ----------------------------
def test_dry_run_places_with_forensics(in_memory_db, cfg_enabled):
    m = _market(in_memory_db)
    ex = live.process_signal(in_memory_db, strategy_key="highest_edge", wallet="0xw",
                             signal_id=10, market=m, outcome="Yes", price=0.5, entry_reason="copy")
    assert ex.status == "open" and ex.size_usd == 2.0 and ex.shares == 4.0
    assert ex.expected_price == 0.5 and ex.fill_price == 0.5 and ex.slippage == 0.0
    assert ex.limit_price == 0.5 and ex.bankroll_before == 40.0
    assert ex.order_latency_ms is not None


def test_duplicate_signal_not_ordered_twice(in_memory_db, cfg_enabled):
    m = _market(in_memory_db)
    a = live.process_signal(in_memory_db, strategy_key="s", wallet="0xw", signal_id=7,
                            market=m, outcome="Yes", price=0.5, entry_reason="t")
    b = live.process_signal(in_memory_db, strategy_key="s", wallet="0xw", signal_id=7,
                            market=m, outcome="Yes", price=0.5, entry_reason="t")
    assert a is not None and b is None
    assert len(in_memory_db.scalars(select(LiveExecution)).all()) == 1


# --- one-order test mode ----------------------------------------------------
def test_one_order_mode_halts_after_first(in_memory_db, monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("LIVE_EXECUTOR", "dry_run")
    monkeypatch.setenv("LIVE_STARTING_BANKROLL", "40.0")
    monkeypatch.setenv("LIVE_MAX_ORDERS", "1")
    m1 = _market(in_memory_db, "ma")
    m2 = _market(in_memory_db, "mb")
    first = live.process_signal(in_memory_db, strategy_key="s", wallet="0xw", signal_id=1,
                                market=m1, outcome="Yes", price=0.5, entry_reason="t")
    assert first is not None and first.status == "open"
    assert live.get_state(in_memory_db).halted          # auto-halted after one order
    # a second attempt is blocked
    second = live.process_signal(in_memory_db, strategy_key="s", wallet="0xw", signal_id=2,
                                 market=m2, outcome="Yes", price=0.5, entry_reason="t")
    assert second is None
    rej = in_memory_db.scalars(select(LiveExecution).where(LiveExecution.status == "rejected")).all()
    assert rej and "halt" in (rej[0].exit_reason or "").lower()
    live.resume(in_memory_db)
    assert not live.get_state(in_memory_db).halted      # manual resume clears it


# --- hard limits ------------------------------------------------------------
def test_max_open_positions(in_memory_db, cfg_enabled):
    for i in range(10):
        in_memory_db.add(LiveExecution(idempotency_key=f"k{i}", strategy_key="s",
                                       wallet_address=f"0x{i}", market_id=f"m{i}", outcome="Yes",
                                       expected_price=0.5, size_usd=2.0, status="open", bankroll_before=40))
    in_memory_db.commit()
    ok, reason = live.check_can_open(in_memory_db, cfg_enabled, wallet="0xn", market_id="mn")
    assert not ok and "max open positions" in reason


def test_daily_loss_stop_halts(in_memory_db, cfg_enabled):
    in_memory_db.add(LiveExecution(idempotency_key="l1", strategy_key="s", wallet_address="0xw",
                                   market_id="m", outcome="Yes", expected_price=0.5, size_usd=10,
                                   status="closed", realized_pnl=-10.0, closed_at=datetime.utcnow(),
                                   bankroll_before=40))
    in_memory_db.commit()
    ok, reason = live.check_can_open(in_memory_db, cfg_enabled, wallet="0xw", market_id="m")
    assert not ok and live.get_state(in_memory_db).halted and "daily" in live.get_state(in_memory_db).halt_reason


def test_total_loss_stop_halts(in_memory_db, cfg_enabled):
    in_memory_db.add(LiveExecution(idempotency_key="t1", strategy_key="s", wallet_address="0xw",
                                   market_id="m", outcome="Yes", expected_price=0.5, size_usd=40,
                                   status="closed", realized_pnl=-40.0,
                                   closed_at=datetime.utcnow() - timedelta(days=3), bankroll_before=40))
    in_memory_db.commit()
    ok, _ = live.check_can_open(in_memory_db, cfg_enabled, wallet="0xw", market_id="m")
    assert not ok and live.get_state(in_memory_db).halted


# --- settlement + accounting + reconciliation -------------------------------
def test_settlement_and_reconcile(in_memory_db, cfg_enabled):
    m = _market(in_memory_db, "ms")
    live.get_state(in_memory_db)
    ex = live.process_signal(in_memory_db, strategy_key="s", wallet="0xw", signal_id=1,
                             market=m, outcome="Yes", price=0.5, entry_reason="t")
    m.resolved = True; m.resolved_outcome = "Yes"; in_memory_db.commit()
    res = live.settle_live(in_memory_db)
    ex2 = in_memory_db.get(LiveExecution, ex.id)
    assert res["closed"] == 1 and ex2.realized_pnl == 2.0          # 4 sh @0.5 win -> +2
    assert ex2.bankroll_after == 42.0
    assert live.reconcile(in_memory_db, 42.0)["reconciled"]
    assert live.reconcile(in_memory_db, 39.0)["drift"] == -3.0


# --- real executor refuses without key (fail closed) ------------------------
def test_polymarket_refuses_without_key(in_memory_db, monkeypatch):
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    m = _market(in_memory_db)
    cfg = live.get_config()
    with pytest.raises(ExecutionRejected):
        live.PolymarketExecutor().place(db=in_memory_db, market=m, outcome="Yes",
                                        price=0.5, size_usd=2.0, cfg=cfg)


def test_polymarket_path_rejects_and_logs_not_halt_on_no_key(in_memory_db, monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("LIVE_EXECUTOR", "polymarket")
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    m = _market(in_memory_db)
    out = live.process_signal(in_memory_db, strategy_key="s", wallet="0xw", signal_id=1,
                              market=m, outcome="Yes", price=0.5, entry_reason="t")
    assert out is None
    ex = in_memory_db.scalar(select(LiveExecution).where(LiveExecution.status == "rejected"))
    assert ex is not None and "PRIVATE_KEY" in (ex.exit_reason or "")


def test_status_caps_max_loss_at_40(in_memory_db, monkeypatch):
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    s = live.status(in_memory_db)
    assert s["live_trading_enabled"] is False
    assert s["sizing"]["position_usd"] == 2.0 and s["sizing"]["method"] == "fixed_dollar"
    assert s["limits_usd"]["total_loss_stop"] == 40.0
    assert s["max_possible_loss"] == 40.0
