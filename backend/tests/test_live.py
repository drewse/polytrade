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


# --- NO lifetime order cap: trading is bounded by concurrent open positions --
def test_lifetime_orders_do_not_halt(in_memory_db, monkeypatch):
    """Placing multiple orders over a session must NOT auto-halt — the retired
    validation cap is gone; concurrent open positions are the only bound."""
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("LIVE_EXECUTOR", "dry_run")
    monkeypatch.setenv("LIVE_STARTING_BANKROLL", "40.0")
    monkeypatch.setenv("LIVE_MAX_OPEN_POSITIONS", "10")
    # distinct wallets so per-wallet exposure never gates ($2 each, cap $8)
    first = live.process_signal(in_memory_db, strategy_key="s", wallet="0xa", signal_id=1,
                                market=_market(in_memory_db, "ma"), outcome="Yes", price=0.5, entry_reason="t")
    assert first is not None and first.status == "open"
    assert not live.get_state(in_memory_db).halted      # NO lifetime-cap halt
    second = live.process_signal(in_memory_db, strategy_key="s", wallet="0xb", signal_id=2,
                                 market=_market(in_memory_db, "mb"), outcome="Yes", price=0.5, entry_reason="t")
    assert second is not None and second.status == "open"   # second order placed fine
    assert not live.get_state(in_memory_db).halted


# --- hard limits ------------------------------------------------------------
def test_max_open_positions(in_memory_db, cfg_enabled):
    for i in range(10):
        in_memory_db.add(LiveExecution(idempotency_key=f"k{i}", strategy_key="s",
                                       wallet_address=f"0x{i}", market_id=f"m{i}", outcome="Yes",
                                       expected_price=0.5, size_usd=2.0, status="open", bankroll_before=40))
    in_memory_db.commit()
    ok, reason = live.check_can_open(in_memory_db, cfg_enabled, wallet="0xn", market_id="mn")
    assert not ok and "max open positions" in reason


def test_open_position_limit_blocks_without_halt_then_reopens_after_close(in_memory_db, monkeypatch):
    """At the concurrent-open-positions limit: refuse a NEW entry WITHOUT halting;
    as soon as one position closes, a new entry is allowed again automatically."""
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("LIVE_EXECUTOR", "dry_run")
    monkeypatch.setenv("LIVE_STARTING_BANKROLL", "40.0")
    monkeypatch.setenv("LIVE_MAX_OPEN_POSITIONS", "2")
    cfg = live.get_config()
    assert cfg.max_positions == 2
    for i in range(2):
        in_memory_db.add(LiveExecution(idempotency_key=f"k{i}", strategy_key="s",
                                       wallet_address=f"0x{i}", market_id=f"m{i}", outcome="Yes",
                                       expected_price=0.5, size_usd=2.0, status="open", bankroll_before=40))
    in_memory_db.commit()
    # at the limit -> blocked, but NOT halted (existing positions keep running)
    ok, reason = live.check_can_open(in_memory_db, cfg, wallet="0xn", market_id="mn")
    assert not ok and "max open positions" in reason
    assert not live.get_state(in_memory_db).halted
    # one position settles/closes -> a slot frees up -> next entry allowed again
    pos = in_memory_db.scalars(select(LiveExecution).where(LiveExecution.status == "open")).first()
    pos.status = "closed"; pos.closed_at = datetime.utcnow(); in_memory_db.commit()
    ok2, _ = live.check_can_open(in_memory_db, cfg, wallet="0xn", market_id="mn")
    assert ok2     # automatically resumes opening new positions, no manual resume needed


def test_legacy_max_positions_env_still_honored(in_memory_db, monkeypatch):
    """Backwards compatibility: the old LIVE_MAX_POSITIONS still sets the limit
    when the new LIVE_MAX_OPEN_POSITIONS is unset."""
    monkeypatch.delenv("LIVE_MAX_OPEN_POSITIONS", raising=False)
    monkeypatch.setenv("LIVE_MAX_POSITIONS", "3")
    assert live.get_config().max_positions == 3
    # the new var takes precedence when both are set
    monkeypatch.setenv("LIVE_MAX_OPEN_POSITIONS", "7")
    assert live.get_config().max_positions == 7


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


def test_pipeline_fills_up_to_open_position_limit_no_halt(in_memory_db, live_env, monkeypatch):
    """The pipeline keeps opening qualifying signals up to the concurrent limit,
    then simply stops opening NEW ones — it does NOT halt (no lifetime cap)."""
    monkeypatch.setenv("LIVE_MAX_OPEN_POSITIONS", "1")
    db = in_memory_db
    w = _eligible_wallet(db)
    m1 = _market(db, "m1"); m2 = _market(db, "m2")
    _signal(db, w, m1); _signal(db, w, m2)        # two qualifying signals
    db.commit()
    from sqlalchemy import func
    rep = live.run_pipeline(db, place=True)
    assert rep["placed"] == 1                      # one slot only, despite two qualifying
    assert live.get_state(db).halted is False      # NOT halted — limit just refuses new entries
    open_n = db.scalar(select(func.count()).select_from(LiveExecution).where(LiveExecution.status == "open"))
    assert open_n == 1                              # exactly one OPEN position (the cap)
    assert rep["filtered"].get("risk_blocked", 0) == 1   # 2nd blocked by the open-position limit


# ===========================================================================
# Limit-at-reference execution (do NOT chase price/slippage)
# ===========================================================================
class _FakeClient:
    """Mocks ONLY the network calls of py-clob-client-v2; the executor's own logic
    (limit pricing, TTL, fill check, cancel) runs for real against it. Mirrors the
    v2 API: get_tick_size, dict order book, OrderArgsV2, cancel_orders([id])."""
    def __init__(self, *, ask, tick=0.01, book_tick=None, book_min=None, get_order=None,
                 post_resp=None, post_exc=None, cancel_exc=None):
        self.ask = ask
        self.tick = tick                  # value returned by get_tick_size() fallback
        self.book_tick = book_tick        # tick_size present IN the book response (or None)
        self.book_min = book_min          # min_order_size present IN the book response (or None)
        self._get_order = get_order
        self._post_resp = post_resp or {"orderID": "oid-1", "status": "live"}
        self._post_exc = post_exc
        self._cancel_exc = cancel_exc
        self.tick_size_calls = 0
        self.posted = []          # OrderArgsV2 submitted
        self.cancelled = []       # order ids cancelled

    def get_tick_size(self, token_id):
        self.tick_size_calls += 1
        return str(self.tick)

    def get_order_book(self, token_id):
        book = {"asks": [{"price": str(self.ask), "size": "1000"}]}   # v2: raw dict
        if self.book_tick is not None:
            book["tick_size"] = str(self.book_tick)
        if self.book_min is not None:
            book["min_order_size"] = str(self.book_min)
        return book

    def create_order(self, order_args, options=None):
        self.posted.append(order_args)
        return {"signed": True}

    def post_order(self, order, order_type, post_only=False, defer_exec=False):
        if self._post_exc:
            raise self._post_exc
        return self._post_resp

    def get_order(self, order_id):
        return self._get_order if self._get_order is not None else {"size_matched": 0, "status": "live"}

    def cancel_orders(self, order_ids):     # v2: list, no single cancel()
        if self._cancel_exc:
            raise self._cancel_exc
        self.cancelled.extend(order_ids)
        return {"canceled": list(order_ids)}


def _poly_cfg(monkeypatch):
    monkeypatch.setenv("LIVE_EXECUTOR", "polymarket")
    monkeypatch.setenv("LIVE_POSITION_USD", "1.10")
    monkeypatch.setenv("ORDER_TTL_SECONDS", "0")          # no real sleep in tests
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", KEY)     # past the no-key guard
    # funder == signer EOA -> EOA-valid config (overrides the baked proxy default)
    monkeypatch.setenv("POLYMARKET_FUNDER", _derive(KEY))
    monkeypatch.setenv("POLYMARKET_SIGNATURE_TYPE", "0")
    return live.get_config()


def _place(monkeypatch, db, fake, *, price=0.50, size=1.10):
    monkeypatch.setattr(live.PolymarketExecutor, "_build_client", lambda self, key: fake)
    cfg = _poly_cfg(monkeypatch)
    m = _market(db)
    return live.PolymarketExecutor().place(db=db, market=m, outcome="Yes",
                                           price=price, size_usd=size, cfg=cfg)


def test_limit_filled_at_reference(in_memory_db, monkeypatch):
    # 1. price available at reference -> order submitted at reference and filled
    fake = _FakeClient(ask=0.48, get_order={"size_matched": 999, "status": "matched"})
    res = _place(monkeypatch, in_memory_db, fake)
    assert res.outcome == "filled"
    assert fake.posted[0].price == 0.50 and res.limit_price == 0.50   # limit AT reference
    assert res.filled_shares == round(1.10 / 0.50, 2) and not fake.cancelled


def test_limit_not_chased_when_price_worse(in_memory_db, monkeypatch):
    # 2. ask (0.60) worse than reference (0.50) -> limit stays at 0.50, NOT chased
    fake = _FakeClient(ask=0.60, get_order={"size_matched": 0, "status": "live"})
    with pytest.raises(ExecutionRejected) as ei:
        _place(monkeypatch, in_memory_db, fake)
    assert ei.value.outcome == "unfilled_cancelled"
    assert fake.posted[0].price == 0.50                  # submitted at reference, not 0.60 ask
    assert fake.cancelled == ["oid-1"]                   # remainder cancelled


def test_unfilled_after_ttl_is_cancelled(in_memory_db, monkeypatch):
    # 3. unfilled after TTL -> cancel called, logged unfilled_cancelled
    fake = _FakeClient(ask=0.50, get_order={"size_matched": 0, "status": "live"})
    with pytest.raises(ExecutionRejected) as ei:
        _place(monkeypatch, in_memory_db, fake)
    assert ei.value.outcome == "unfilled_cancelled" and fake.cancelled == ["oid-1"]


def test_partial_fill_keeps_filled_cancels_remainder(in_memory_db, monkeypatch):
    # 4. partial fill -> keep filled portion, cancel the remainder
    full = round(1.10 / 0.50, 2)                          # 2.2 shares
    fake = _FakeClient(ask=0.50, get_order={"size_matched": full / 2, "status": "live"})
    res = _place(monkeypatch, in_memory_db, fake)
    assert res.outcome == "partially_filled_cancelled"
    assert res.filled_shares == round(full / 2, 2) and fake.cancelled == ["oid-1"]
    # through process_signal the position records the FILLED amount + intended stake
    monkeypatch.setattr(live.PolymarketExecutor, "_build_client", lambda self, key: fake)
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    m = _market(in_memory_db, "m2")
    ex = live.process_signal(in_memory_db, strategy_key="s", wallet="0xw", signal_id=55,
                             market=m, outcome="Yes", price=0.50, entry_reason="copy")
    assert ex.status == "open" and ex.fill_outcome == "partially_filled_cancelled"
    assert ex.size_usd == round((full / 2) * 0.50, 2) and ex.requested_size_usd == 1.10


def test_submit_error_logs_full_venue_text(in_memory_db, monkeypatch):
    # 5a. submit error -> full PolyApiException text captured (not truncated)
    from py_clob_client_v2.exceptions import PolyApiException
    exc = PolyApiException(error_msg="not enough balance / allowance")
    fake = _FakeClient(ask=0.50, post_exc=exc)
    with pytest.raises(ExecutionRejected) as ei:
        _place(monkeypatch, in_memory_db, fake)
    assert ei.value.outcome == "submit_error"
    assert "not enough balance / allowance" in (ei.value.venue_error or "")
    # through process_signal the FULL text lands in venue_error (exit_reason is short)
    monkeypatch.setattr(live.PolymarketExecutor, "_build_client", lambda self, key: fake)
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    m = _market(in_memory_db, "m3")
    live.process_signal(in_memory_db, strategy_key="s", wallet="0xw", signal_id=66,
                        market=m, outcome="Yes", price=0.50, entry_reason="copy")
    rej = in_memory_db.scalar(select(LiveExecution).where(LiveExecution.signal_id == 66))
    assert rej.status == "rejected" and rej.fill_outcome == "submit_error"
    assert "not enough balance / allowance" in (rej.venue_error or "")
    assert len(rej.venue_error) > 40                      # full text, not the 40-char exit_reason


def test_cancel_error_logs_full_venue_text(in_memory_db, monkeypatch):
    # 5b. cancel error -> full venue text captured, outcome cancel_error
    from py_clob_client_v2.exceptions import PolyApiException
    fake = _FakeClient(ask=0.50, get_order={"size_matched": 0, "status": "live"},
                       cancel_exc=PolyApiException(error_msg="order already cancelled"))
    with pytest.raises(ExecutionRejected) as ei:
        _place(monkeypatch, in_memory_db, fake)
    assert ei.value.outcome == "cancel_error"
    assert "order already cancelled" in (ei.value.venue_error or "")


# --- v2 SDK migration: guards, schema/geoblock categorization, paper mode ----
def test_archived_v1_sdk_rejected_for_real_trading(in_memory_db, monkeypatch):
    # 1. archived py-clob-client (v1) present -> hard fail-closed for real trading
    monkeypatch.setattr(live, "archived_v1_present", lambda: True)
    with pytest.raises(ExecutionRejected) as ei:
        live._assert_real_sdk()
    assert ei.value.outcome == "archived_sdk"
    # and the executor refuses to place
    fake = _FakeClient(ask=0.50, get_order={"size_matched": 999, "status": "matched"})
    with pytest.raises(ExecutionRejected) as ei2:
        _place(monkeypatch, in_memory_db, fake)
    assert ei2.value.outcome == "archived_sdk"


def test_v2_sdk_missing_rejected(monkeypatch):
    # the v2 SDK itself must be present for real trading
    monkeypatch.setattr(live, "py_clob_installed", lambda: False)
    with pytest.raises(ExecutionRejected) as ei:
        live._assert_real_sdk()
    assert ei.value.outcome == "sdk_missing"


def test_invalid_order_version_is_stale_client_schema(in_memory_db, monkeypatch):
    # 6. 400 invalid order version -> categorized stale_client_schema, full venue text
    from py_clob_client_v2.exceptions import PolyApiException
    exc = PolyApiException(error_msg={"error": "invalid order version, please use the latest clob-client"})
    fake = _FakeClient(ask=0.50, post_exc=exc)
    with pytest.raises(ExecutionRejected) as ei:
        _place(monkeypatch, in_memory_db, fake)
    assert ei.value.outcome == "stale_client_schema"
    assert "invalid order version" in (ei.value.venue_error or "")
    assert live._categorize_rejection("", "stale_client_schema") == "stale_client_schema"


def test_geoblock_still_handled(in_memory_db, monkeypatch):
    # 7. 403 region restriction -> categorized geoblocked, full venue text captured
    from py_clob_client_v2.exceptions import PolyApiException
    exc = PolyApiException(error_msg={"error": "Trading restricted in your region, please refer to ..."})
    fake = _FakeClient(ask=0.50, post_exc=exc)
    with pytest.raises(ExecutionRejected) as ei:
        _place(monkeypatch, in_memory_db, fake)
    assert ei.value.outcome == "geoblocked"
    assert "restricted in your region" in (ei.value.venue_error or "").lower()
    assert live._categorize_rejection("", "geoblocked") == "geoblocked"


def test_paper_mode_works_without_real_submission(in_memory_db, monkeypatch):
    # 8. dry_run / paper mode never touches the v2 SDK and still opens a position
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("LIVE_EXECUTOR", "dry_run")
    monkeypatch.setenv("LIVE_STARTING_BANKROLL", "40")
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)   # no key needed for paper
    m = _market(in_memory_db, "mp")
    ex = live.process_signal(in_memory_db, strategy_key="s", wallet="0xw", signal_id=88,
                             market=m, outcome="Yes", price=0.5, entry_reason="paper")
    assert ex.status == "open" and ex.executor == "dry_run" and ex.fill_outcome == "simulated"


def test_sdk_info_reports_v2(monkeypatch):
    info = live.sdk_info()
    assert info["sdk_package"] == "py-clob-client-v2" and info["clob_api_mode"] == "v2"
    assert info["collateral"] == "USDC" and info["v2_sdk_installed"] is True


# --- venue-provided book metadata (tick_size / min_order_size) ---------------
def test_book_tick_size_overrides_get_tick_size(in_memory_db, monkeypatch):
    # 1. book.tick_size (0.001) is used for flooring; get_tick_size() NOT called
    fake = _FakeClient(ask=0.48, tick=0.01, book_tick=0.001,
                       get_order={"size_matched": 999, "status": "matched"})
    res = _place(monkeypatch, in_memory_db, fake, price=0.505)
    assert res.outcome == "filled"
    assert res.tick_size == 0.001                 # book tick recorded
    assert res.limit_price == 0.505               # floored on 0.001 grid (0.01 would give 0.50)
    assert fake.tick_size_calls == 0              # fallback never consulted


def test_book_min_order_size_enforced(in_memory_db, monkeypatch):
    # 2. book.min_order_size (5 shares) rejects a sub-minimum order, fail closed
    fake = _FakeClient(ask=0.48, book_tick=0.01, book_min=5,
                       get_order={"size_matched": 999, "status": "matched"})
    with pytest.raises(ExecutionRejected) as ei:
        _place(monkeypatch, in_memory_db, fake, price=0.50)   # 1.10/0.50 = 2.2 shares < 5
    assert "min_order_size" in str(ei.value) and "shares" in str(ei.value)
    assert not fake.posted                         # never submitted
    assert live._categorize_rejection(str(ei.value)) == "no_capital"


def test_missing_book_metadata_falls_back_safely(in_memory_db, monkeypatch):
    # 3. no tick_size/min_order_size in book -> get_tick_size() + config notional floor
    fake = _FakeClient(ask=0.48, tick=0.01,        # book_tick/book_min default None
                       get_order={"size_matched": 999, "status": "matched"})
    res = _place(monkeypatch, in_memory_db, fake, price=0.50)
    assert res.outcome == "filled"
    assert res.tick_size == 0.01 and fake.tick_size_calls == 1   # fallback used
    assert res.min_order_size is None              # fell back to config.min_stake


def test_used_tick_and_min_size_logged_on_execution(in_memory_db, monkeypatch):
    # 4. the tick/min used are recorded on the execution decision row
    fake = _FakeClient(ask=0.18, book_tick=0.01, book_min=5,
                       get_order={"size_matched": 999, "status": "matched"})
    monkeypatch.setattr(live.PolymarketExecutor, "_build_client", lambda self, key: fake)
    _poly_cfg(monkeypatch)
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    m = _market(in_memory_db, "mtick")
    ex = live.process_signal(in_memory_db, strategy_key="s", wallet="0xw", signal_id=77,
                             market=m, outcome="Yes", price=0.20, entry_reason="copy")
    # 1.10/0.20 = 5.5 shares >= 5 -> fills, and the venue metadata is persisted
    assert ex.status == "open" and ex.fill_outcome == "filled"
    assert ex.tick_size == 0.01 and ex.min_order_size == 5.0
