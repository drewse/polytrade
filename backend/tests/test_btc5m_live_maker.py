"""BTC 5M live-maker SAFETY tests (all offline — book/markets monkeypatched, orders
via MockClobClient). Verifies: live path is unreachable without ENABLED+armed(live);
risk guard rejects over-cap / over-exposure / crossing(maker-only); kill cancels all +
latches; arm expiry; the full quote→submit→fill→markout→cancel lifecycle + metrics;
shadow mode sends nothing; and isolation (no LiveExecution, bankroll unchanged)."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from app import btc5m_live_maker as mk
from app import btc5m_live_maker_clob as clob
from app import btc5m_live_maker_models as lmm
from app import live
from app.models import LiveExecution


# --- fake market data (no network) ------------------------------------------
def _patch_book(monkeypatch, *, bid=0.40, ask=0.45, mid=0.425):
    monkeypatch.setattr(clob, "open_btc5m_markets",
                        lambda limit=30: [{"market_id": "M1", "slug": "btc-updown-5m-1", "question": "BTC?",
                                           "token_ids": ["TOK_YES", "TOK_NO"], "end_date": None}])
    monkeypatch.setattr(clob, "get_book",
                        lambda token_id: {"ok": True, "best_bid": bid, "best_ask": ask, "mid": mid,
                                          "bid_size": 100.0, "ts": datetime.utcnow(), "mono_ns": 0, "error": None})


# --- the live gate ----------------------------------------------------------
def test_live_arming_refused_without_enabled(in_memory_db, monkeypatch):
    db = in_memory_db
    monkeypatch.delenv("BTC5M_LIVE_MAKER_ENABLED", raising=False)
    r = mk.arm(db, mode="live")
    assert r["ok"] is False and "ENABLED" in r["error"]
    assert mk.status(db)["live_path_reachable"] is False


def test_make_client_none_when_not_enabled(in_memory_db, monkeypatch):
    db = in_memory_db
    monkeypatch.delenv("BTC5M_LIVE_MAKER_ENABLED", raising=False)
    mk.arm(db, mode="shadow")                      # shadow always allowed
    # shadow client is fine; but a 'live' mode without ENABLED must yield no client
    st = mk._state(db); st.mode = "live"; db.commit()
    assert mk._make_client(db) is None             # HARD gate


def test_status_default_is_safe(in_memory_db, monkeypatch):
    db = in_memory_db
    monkeypatch.delenv("BTC5M_LIVE_MAKER_ENABLED", raising=False)
    s = mk.status(db)
    assert s["enabled"] is False and s["armed"] is False and s["live_path_reachable"] is False
    assert "DATA COLLECTION only" in s["safety"]


# --- risk guard -------------------------------------------------------------
def test_risk_rejects_crossing_maker_only(in_memory_db, monkeypatch):
    db = in_memory_db
    mk.arm(db, mode="shadow")
    ok, reason = mk.risk_check(db, notional=1.0, price=0.45, best_ask=0.45)   # price == ask => crosses
    assert ok is False and "maker-only" in reason
    ok2, _ = mk.risk_check(db, notional=1.0, price=0.44, best_ask=0.45)       # rests inside
    assert ok2 is True


def test_risk_rejects_over_caps(in_memory_db, monkeypatch):
    db = in_memory_db
    monkeypatch.setenv("BTC5M_LIVE_MAKER_PER_ORDER_USD", "1.5")
    monkeypatch.setenv("BTC5M_LIVE_MAKER_MAX_EXPOSURE_USD", "3")
    mk.arm(db, mode="shadow")
    big, r = mk.risk_check(db, notional=5.0, price=0.4, best_ask=0.45)        # over per-order
    assert big is False and "per-order" in r
    st = mk._state(db); st.open_exposure_usd = 2.5; db.commit()
    ok, r2 = mk.risk_check(db, notional=1.0, price=0.4, best_ask=0.45)        # 2.5+1.0 > 3
    assert ok is False and "exposure" in r2


def test_risk_rejects_when_disarmed_or_expired(in_memory_db):
    db = in_memory_db
    ok, r = mk.risk_check(db, notional=1.0, price=0.4, best_ask=0.45)
    assert ok is False and "not armed" in r
    mk.arm(db, mode="shadow", ttl_min=20)
    st = mk._state(db); st.arm_expires_at = datetime.utcnow() - timedelta(minutes=1); db.commit()
    ok2, r2 = mk.risk_check(db, notional=1.0, price=0.4, best_ask=0.45)
    assert ok2 is False and "expired" in r2


# --- kill switch ------------------------------------------------------------
def test_kill_cancels_all_and_latches(in_memory_db, monkeypatch):
    db = in_memory_db
    monkeypatch.setenv("BTC5M_LIVE_MAKER_ENABLED", "true")
    monkeypatch.setenv("BTC5M_LIVE_MAKER_PRIVATE_KEY", "0xdummy")
    _patch_book(monkeypatch)
    mk.arm(db, mode="live")
    client = clob.MockClobClient()
    mk.run_cycle(db, client=client)                # posts a resting order
    assert db.scalar(select(func.count()).select_from(lmm.Btc5mLiveMakerOrder).where(
        lmm.Btc5mLiveMakerOrder.status.in_(("acked", "resting", "partial")))) >= 1
    mk.kill(db, client=client)
    st = mk.status(db)
    assert st["kill"] is True and st["armed"] is False and st["open_orders"] == 0
    # cannot re-arm while killed
    assert mk.arm(db, mode="shadow")["ok"] is False
    assert mk.reset_kill(db)["kill"] is False


# --- full mock lifecycle ----------------------------------------------------
def test_full_lifecycle_quote_fill_markout(in_memory_db, monkeypatch):
    db = in_memory_db
    monkeypatch.setenv("BTC5M_LIVE_MAKER_ENABLED", "true")
    monkeypatch.setenv("BTC5M_LIVE_MAKER_PRIVATE_KEY", "0xdummy")
    monkeypatch.setenv("BTC5M_LIVE_MAKER_MIN_SHARES", "5")
    _patch_book(monkeypatch, bid=0.40, ask=0.45, mid=0.425)
    bank0 = live.get_state(db).bankroll
    mk.arm(db, mode="live")
    client = clob.MockClobClient(fill_after_polls=1, fill_price=0.40)
    # cycle 1: post; exposure counts
    mk.run_cycle(db, client=client)
    o = db.scalars(select(lmm.Btc5mLiveMakerOrder)).first()
    assert o.status == "acked" and o.exchange_order_id and o.mid_at_quote == 0.425
    assert mk._state(db).open_exposure_usd > 0
    # cycle 2: reconcile -> fill recorded with mid_at_fill + realized spread
    mk.run_cycle(db, client=client)
    db.refresh(o)
    assert o.filled_shares > 0 and o.first_fill_at is not None and o.realized_spread is not None
    # backdate the fill and reconcile -> 5s mark-out (adverse selection) captured
    o.first_fill_at = datetime.utcnow() - timedelta(seconds=6); db.commit()
    mk.run_cycle(db, client=client)
    db.refresh(o)
    assert o.mid_5s is not None and o.adverse_5s is not None
    # metrics computed
    m = mk.metrics(db)
    assert m["real_orders"] >= 1 and m["fills"] >= 1 and m["fill_probability"] is not None
    assert m["avg_submit_latency_ms"] is not None
    # ISOLATION
    assert db.scalar(select(func.count()).select_from(LiveExecution)) == 0
    assert live.get_state(db).bankroll == bank0


def test_cancel_after_queue_lifetime(in_memory_db, monkeypatch):
    db = in_memory_db
    monkeypatch.setenv("BTC5M_LIVE_MAKER_ENABLED", "true")
    monkeypatch.setenv("BTC5M_LIVE_MAKER_PRIVATE_KEY", "0xdummy")
    monkeypatch.setenv("BTC5M_LIVE_MAKER_QUEUE_LIFETIME_S", "12")
    _patch_book(monkeypatch)
    mk.arm(db, mode="live")
    client = clob.MockClobClient(fill_after_polls=None)        # never fills
    mk.run_cycle(db, client=client)
    o = db.scalars(select(lmm.Btc5mLiveMakerOrder)).first()
    o.quote_at = datetime.utcnow() - timedelta(seconds=20); db.commit()   # past lifetime
    mk.run_cycle(db, client=client)
    db.refresh(o)
    assert o.status == "cancelled" and o.cancel_success is True and o.queue_lifetime_ms is not None


# --- shadow mode sends nothing ----------------------------------------------
def test_shadow_mode_no_real_orders(in_memory_db, monkeypatch):
    db = in_memory_db
    monkeypatch.delenv("BTC5M_LIVE_MAKER_ENABLED", raising=False)
    _patch_book(monkeypatch)
    mk.arm(db, mode="shadow")
    mk.run_cycle(db)                               # uses ShadowClient internally
    o = db.scalars(select(lmm.Btc5mLiveMakerOrder)).first()
    assert o is not None and o.status == "shadow" and o.exchange_order_id is None
    # shadow never books exposure or real money
    assert mk._state(db).open_exposure_usd == 0.0 and mk._state(db).deployed_usd == 0.0
    assert db.scalar(select(func.count()).select_from(LiveExecution)) == 0


def test_run_cycle_noop_when_disarmed(in_memory_db, monkeypatch):
    db = in_memory_db
    monkeypatch.delenv("BTC5M_LIVE_MAKER_ENABLED", raising=False)
    r = mk.run_cycle(db)
    assert r["ran"] is False and r["skipped"] == "disarmed"


# --- $100 experiment budget (software-enforced; ignores wallet balance) ------
def _add_order(db, **kw):
    base = dict(session_id=1, client_id="x", market_id="M", token_id="T", outcome="YES", side="BUY",
                price=0.4, size_shares=5, notional_usd=2.0, mode="live", status="resting")
    base.update(kw)
    o = lmm.Btc5mLiveMakerOrder(**base); db.add(o); db.commit(); return o


def test_experiment_budget_caps_committed_capital(in_memory_db, monkeypatch):
    db = in_memory_db
    monkeypatch.setenv("BTC5M_LIVE_MAKER_MAX_EXPERIMENT_CAPITAL", "100")
    monkeypatch.setenv("BTC5M_LIVE_MAKER_MAX_CONCURRENT", "9")
    mk.arm(db, mode="shadow")
    _add_order(db, client_id="big", notional_usd=99.0)      # $99 already committed at risk
    assert mk.committed_capital(db) == 99.0
    ok, reason = mk.risk_check(db, notional=3.0, price=0.4, best_ask=0.45)   # 99+3 > 100
    assert ok is False and "experiment budget" in reason
    ok2, _ = mk.risk_check(db, notional=1.0, price=0.4, best_ask=0.45)       # 99+1 <= 100
    assert ok2 is True


def test_committed_capital_ignores_shadow_and_settled(in_memory_db):
    db = in_memory_db
    _add_order(db, mode="shadow", status="shadow", notional_usd=50.0)        # shadow: no capital
    _add_order(db, status="filled", position_settled=True, notional_usd=50.0)  # settled: freed
    assert mk.committed_capital(db) == 0.0


# --- permanent cumulative-loss lock -----------------------------------------
def test_cumulative_loss_lock_latches_and_blocks(in_memory_db, monkeypatch):
    db = in_memory_db
    monkeypatch.setenv("BTC5M_LIVE_MAKER_ENABLED", "true")
    monkeypatch.setenv("BTC5M_LIVE_MAKER_PRIVATE_KEY", "0xdummy")
    monkeypatch.setenv("BTC5M_LIVE_MAKER_CUMULATIVE_LOSS_STOP", "100")
    mk.arm(db, mode="live")
    # two settled positions totalling -$100 realized
    _add_order(db, client_id="L1", status="filled", position_settled=True, realized_pnl=-50.0)
    _add_order(db, client_id="L2", status="filled", position_settled=True, realized_pnl=-50.0)
    assert mk.cumulative_realized_pnl(db) == -100.0
    r = mk.run_cycle(db, client=clob.MockClobClient())
    assert r.get("stopped") == "cumulative_loss_lock"
    st = mk.status(db)
    assert st["locked"] is True and st["live_path_reachable"] is False
    # locked: cannot arm, cannot post, no client
    assert mk.arm(db, mode="shadow")["ok"] is False
    assert mk._make_client(db) is None
    ok, reason = mk.risk_check(db, notional=1.0, price=0.4, best_ask=0.45)
    assert ok is False and "LOCKED" in reason
    # manual reset clears the lock flag
    assert mk.reset_lock(db)["locked"] is False


# --- position settlement at resolution --------------------------------------
def test_settlement_computes_realized_pnl(in_memory_db, monkeypatch):
    db = in_memory_db
    monkeypatch.setenv("BTC5M_LIVE_MAKER_ENABLED", "true")
    monkeypatch.setenv("BTC5M_LIVE_MAKER_PRIVATE_KEY", "0xdummy")
    mk.arm(db, mode="live")
    import time
    past = int(time.time()) - 1000                  # resolved long ago
    _add_order(db, client_id="P1", status="filled", filled_shares=6, fill_price=0.40,
               notional_usd=2.4, market_window_ts=past, position_settled=False)
    monkeypatch.setattr(clob, "get_resolution", lambda ts: {"resolved": True, "won_yes": True})
    mk.run_cycle(db, client=clob.MockClobClient())
    o = db.scalars(select(lmm.Btc5mLiveMakerOrder).where(lmm.Btc5mLiveMakerOrder.client_id == "P1")).first()
    assert o.position_settled is True and o.won is True
    assert abs(o.realized_pnl - (6 * 1.0 - 6 * 0.40)) < 1e-6      # +3.60 (won)


# --- startup reconciliation -------------------------------------------------
def test_reconcile_cancels_orphans(in_memory_db, monkeypatch):
    db = in_memory_db
    client = clob.MockClobClient()
    client.post_limit(token_id="T", side="BUY", price=0.4, size=5)   # an orphan open order on the exchange
    _add_order(db, client_id="orphan", exchange_order_id="mock-1", status="resting")
    r = mk.reconcile_open_orders(db, client=client)
    assert r["ok"] and r["exchange_cancelled"] >= 1 and r["db_cancelled"] >= 1
    o = db.scalars(select(lmm.Btc5mLiveMakerOrder).where(lmm.Btc5mLiveMakerOrder.client_id == "orphan")).first()
    assert o.status == "cancelled"


def test_status_surfaces_budget_and_lock(in_memory_db, monkeypatch):
    db = in_memory_db
    monkeypatch.setenv("BTC5M_LIVE_MAKER_MAX_EXPERIMENT_CAPITAL", "100")
    s = mk.status(db)
    eb = s["experiment_budget"]
    assert eb["max_experiment_capital_usd"] == 100 and eb["remaining_usd"] == 100
    assert eb["cumulative_loss_stop_usd"] == 100
    assert s["locked"] is False and "$100 experiment budget" in s["safety"]
