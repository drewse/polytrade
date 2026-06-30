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
