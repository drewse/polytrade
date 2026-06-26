"""Live execution test-layer validation: fixed-$ sizing, absolute caps, halt
latch, one-order mode, duplicate prevention, safety switch, settlement,
reconciliation, real-executor refuses without key."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app import live
from app.live import ExecutionRejected
from app.models import (
    LiveExecution, LiveSignalDecision, LiveState, Market,
    PaperSignal, Wallet, WalletCandidate, WalletStat,
)


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
    # no key -> wallet_check is invalid -> blocked at the gate (fail closed, no submit)
    assert ex is not None and "config invalid" in (ex.exit_reason or "")


def _derive(key):
    from eth_account import Account
    return Account.from_key(key).address


KEY = "0x" + "1" * 64  # deterministic test key (never a real funded wallet)


def test_wallet_check_eoa_match_valid(monkeypatch):
    addr = _derive(KEY)
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", KEY)
    monkeypatch.setenv("RELAYER_API_KEY_ADDRESS", addr)
    monkeypatch.setenv("POLYMARKET_SIGNATURE_TYPE", "0")
    wc = live.wallet_check()
    assert wc["derived_eoa"].lower() == addr.lower()
    assert wc["addresses_match"] is True
    assert wc["recommended_signature_type"] == 0
    assert wc["configuration_valid"] is True


def test_wallet_check_proxy_mismatch_invalid_when_sig0(monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", KEY)
    monkeypatch.setenv("RELAYER_API_KEY_ADDRESS", "0x000000000000000000000000000000000000dEaD")
    monkeypatch.setenv("POLYMARKET_SIGNATURE_TYPE", "0")
    wc = live.wallet_check()
    assert wc["addresses_match"] is False
    assert wc["configuration_valid"] is False        # EOA sig on a proxy wallet
    assert wc["recommended_signature_type"] is None  # 1 vs 2 not determinable
    assert "proxy" in wc["note"].lower()


def test_wallet_check_proxy_valid_when_proxy_sig_set(monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", KEY)
    monkeypatch.setenv("RELAYER_API_KEY_ADDRESS", "0x000000000000000000000000000000000000dEaD")
    monkeypatch.setenv("POLYMARKET_SIGNATURE_TYPE", "1")
    wc = live.wallet_check()
    assert wc["addresses_match"] is False and wc["configuration_valid"] is True


def test_wallet_check_no_key(monkeypatch):
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    wc = live.wallet_check()
    assert wc["derived_eoa"] is None and wc["configuration_valid"] is False


def test_invalid_wallet_config_blocks_real_order(in_memory_db, monkeypatch):
    # proxy wallet + sig_type 0 -> placement must be refused before any submission
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("LIVE_EXECUTOR", "polymarket")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", KEY)
    monkeypatch.setenv("RELAYER_API_KEY_ADDRESS", "0x000000000000000000000000000000000000dEaD")
    monkeypatch.setenv("POLYMARKET_SIGNATURE_TYPE", "0")
    cfg = live.get_config()
    ok, reason = live.check_can_open(in_memory_db, cfg, wallet="0xw", market_id="m")
    assert not ok and "wallet config invalid" in reason


def test_status_reports_py_clob_installed(in_memory_db):
    s = live.status(in_memory_db)
    assert "py_clob_client_installed" in s["auth"]
    assert s["auth"]["py_clob_client_installed"] is live.py_clob_installed()


def test_reset_test_state_clears_attempts_and_enables_bankroll(in_memory_db):
    db = in_memory_db
    # rejected (polymarket) + dry-run attempts exist -> set_bankroll blocked
    db.add(LiveExecution(idempotency_key="r1", executor="polymarket", strategy_key="s",
                         wallet_address="0xw", market_id="m", outcome="Yes", expected_price=0.5,
                         size_usd=0.0, status="rejected", bankroll_before=100))
    db.add(LiveExecution(idempotency_key="d1", executor="dry_run", strategy_key="s",
                         wallet_address="0xw", market_id="m2", outcome="Yes", expected_price=0.5,
                         size_usd=2.0, status="open", bankroll_before=100))
    db.commit()
    assert live.set_bankroll(db, 41.41)["ok"] is False        # blocked by attempts
    res = live.reset_test_state(db)
    assert res["ok"] and res["cleared_attempts"] == 2
    assert _count(db) == 0                                     # all attempts gone
    assert live.set_bankroll(db, 41.41)["ok"] is True          # now allowed
    assert live.get_state(db).bankroll == 41.41


def test_reset_test_state_refuses_with_real_filled_order(in_memory_db):
    db = in_memory_db
    db.add(LiveExecution(idempotency_key="real1", executor="polymarket", strategy_key="s",
                         wallet_address="0xw", market_id="m", outcome="Yes", expected_price=0.5,
                         size_usd=1.0, status="closed", realized_pnl=0.5, bankroll_before=41,
                         order_id="0xabc"))
    db.commit()
    res = live.reset_test_state(db)
    assert res["ok"] is False and "real" in res["error"].lower()
    assert _count(db) == 1                                     # real order untouched


def test_run_once_is_diagnostic_only(in_memory_db, monkeypatch):
    # /api/live/run-once => run_pipeline(place=False): never places, never records
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("LIVE_EXECUTOR", "polymarket")
    out = live.run_pipeline(in_memory_db, place=False)
    assert out["mode"] == "diagnostic" and out["placed"] == 0
    assert out["executor_called"] is False
    assert _count(in_memory_db) == 0                      # no executions
    assert _decisions(in_memory_db) == 0                 # no decision rows written


def _count(db):
    from sqlalchemy import func
    return db.scalar(select(func.count()).select_from(LiveExecution)) or 0


def _decisions(db):
    from sqlalchemy import func
    from app.models import LiveSignalDecision
    return db.scalar(select(func.count()).select_from(LiveSignalDecision)) or 0


def test_status_caps_max_loss_at_40(in_memory_db, monkeypatch):
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    s = live.status(in_memory_db)
    assert s["live_trading_enabled"] is False
    assert s["sizing"]["position_usd"] == 2.0 and s["sizing"]["method"] == "fixed_dollar"
    assert s["limits_usd"]["total_loss_stop"] == 40.0
    assert s["max_possible_loss"] == 40.0


# ===========================================================================
# Event-driven execution pipeline + decision observability
# ===========================================================================
def _eligible_wallet(db, addr="0xwin"):
    """A wallet that clears the production filters (profitable, active, settled)."""
    w = Wallet(address=addr, copy_enabled=True,
               last_active=datetime.utcnow() - timedelta(days=2))
    db.add(w); db.flush()
    db.add(WalletStat(wallet_id=w.id, num_trades=100, num_settled=100, realized_roi=0.28,
                      win_rate=0.6, profit_factor=2.2, expectancy=10.0, sharpe=0.5,
                      recency_score=0.9, partial_history=False, consistency=0.6))
    db.add(WalletCandidate(wallet_id=w.id, copyability_score=70.0, classification="good_candidate"))
    return w


def _signal(db, w, m, *, edge=0.1, conf=80, age_min=0, outcome="Yes", price=0.5):
    s = PaperSignal(wallet_id=w.id, market_id=m.id, outcome=outcome, side="buy",
                    observed_price=price, suggested_entry=price, confidence=conf,
                    edge_estimate=edge, reason="t",
                    created_at=datetime.utcnow() - timedelta(minutes=age_min))
    db.add(s); db.flush()
    return s


@pytest.fixture
def live_env(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("LIVE_EXECUTOR", "dry_run")
    monkeypatch.setenv("LIVE_STARTING_BANKROLL", "40")
    monkeypatch.setenv("LIVE_MAX_ORDERS", "0")
    monkeypatch.setenv("LIVE_MIN_EDGE", "0.05")
    monkeypatch.setenv("LIVE_MIN_CONFIDENCE", "65")
    monkeypatch.setenv("LIVE_SIGNAL_TTL_MIN", "30")


def test_event_driven_places_and_records_full_trail(in_memory_db, live_env):
    db = in_memory_db
    w = _eligible_wallet(db); m = _market(db, "mw"); _signal(db, w, m); db.commit()
    rep = live.run_pipeline(db, place=True)
    assert rep["mode"] == "execute" and rep["placed"] == 1 and rep["executor_called"] is True
    assert rep["eligible"] == 1 and rep["signals_seen"] == 1 and rep["new_evaluated"] == 1
    ex = db.scalars(select(LiveExecution).where(LiveExecution.status == "open")).all()
    assert len(ex) == 1 and ex[0].wallet_address == "0xwin"
    d = db.scalar(select(LiveSignalDecision))
    assert d.status == "filled" and d.category == "filled" and d.execution_id == ex[0].id
    # full audit trail: detected -> wallet -> edge -> confidence -> open -> fresh -> risk -> filled
    for g in ("trading_enabled", "wallet_eligible", "edge_ok", "confidence_ok",
              "market_open", "fresh", "duplicate_check", "risk_passed", "submitted", "filled"):
        assert d.gates[g] is True


def test_no_double_execution(in_memory_db, live_env):
    db = in_memory_db
    w = _eligible_wallet(db); m = _market(db, "mw"); _signal(db, w, m); db.commit()
    r1 = live.run_pipeline(db, place=True)
    r2 = live.run_pipeline(db, place=True)        # second cycle: same signal
    assert r1["placed"] == 1 and r2["placed"] == 0
    assert r2["filtered"]["already_processed"] == 1 and r2["new_evaluated"] == 0
    assert _count(db) == 1 and _decisions(db) == 1   # exactly one order + one decision, ever


def test_non_qualifying_recorded_with_exact_reason(in_memory_db, live_env):
    db = in_memory_db
    w = _eligible_wallet(db); m = _market(db, "mw")
    _signal(db, w, m, edge=0.01)                  # below LIVE_MIN_EDGE
    db.commit()
    rep = live.run_pipeline(db, place=True)
    assert rep["placed"] == 0 and rep["filtered"]["low_edge"] == 1
    d = db.scalar(select(LiveSignalDecision))
    assert d.status == "skipped" and d.category == "low_edge"
    assert d.gates["wallet_eligible"] is True and d.gates["edge_ok"] is False
    assert "edge" in d.reason


def test_wallet_not_eligible_skipped(in_memory_db, live_env):
    db = in_memory_db
    # losing wallet -> fails production filters
    los = Wallet(address="0xlose", copy_enabled=True, last_active=datetime.utcnow())
    db.add(los); db.flush()
    db.add(WalletStat(wallet_id=los.id, num_trades=100, num_settled=100, realized_roi=-0.05,
                      win_rate=0.4, profit_factor=0.8, recency_score=0.5))
    m = _market(db, "mw"); _signal(db, los, m); db.commit()
    rep = live.run_pipeline(db, place=True)
    assert rep["placed"] == 0 and rep["filtered"]["wallet_not_eligible"] == 1
    assert db.scalar(select(LiveSignalDecision)).category == "wallet_not_eligible"


def test_stale_signal_expired(in_memory_db, live_env):
    db = in_memory_db
    w = _eligible_wallet(db); m = _market(db, "mw")
    _signal(db, w, m, age_min=60)                 # older than TTL (30m), inside window (120m)
    db.commit()
    rep = live.run_pipeline(db, place=True)
    assert rep["filtered"]["stale"] == 1 and rep["placed"] == 0
    assert db.scalar(select(LiveSignalDecision)).status == "expired"


def test_run_once_diagnoses_without_side_effects(in_memory_db, live_env):
    db = in_memory_db
    w = _eligible_wallet(db); m = _market(db, "mw"); _signal(db, w, m); db.commit()
    rep = live.run_pipeline(db, place=False)      # the run-once diagnostic
    assert rep["mode"] == "diagnostic" and rep["placed"] == 0 and rep["eligible"] == 1
    cand = rep["candidates"][0]
    assert cand["status"] == "eligible" and cand["category"] == "would_execute"
    assert cand["production_score"] > 0 and cand["wallet"] == "0xwin"
    assert _count(db) == 0 and _decisions(db) == 0    # zero side effects
    assert live.run_pipeline(db, place=True)["placed"] == 1   # worker then executes it


def test_one_order_mode_stops_after_one(in_memory_db, live_env, monkeypatch):
    monkeypatch.setenv("LIVE_MAX_ORDERS", "1")
    db = in_memory_db
    w = _eligible_wallet(db)
    m1 = _market(db, "m1"); m2 = _market(db, "m2")
    _signal(db, w, m1); _signal(db, w, m2)        # two qualifying signals
    db.commit()
    rep = live.run_pipeline(db, place=True)
    assert rep["placed"] == 1                      # exactly one, despite two qualifying
    assert live.get_state(db).halted is True       # auto-halt after one-order test
    assert _count(db) == 1                          # only one execution exists
