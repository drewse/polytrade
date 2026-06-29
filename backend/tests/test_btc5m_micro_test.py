"""BTC 5M Micro-Test Mode tests: opt-in + disarmed by default; only BTC5M markets
/ watched wallets / price ≤ ceiling / ≥ min seconds remaining qualify; fixed
5-share sizing; one concurrent position; daily/total/trade-count stops; execution
errors stop the test; trades tagged + stored separately; and general live copy
trading (LiveExecution / LiveState bankroll) is never touched."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from app import btc5m_micro_test as umt
from app import live
from app import btc5m_micro_test_models as mt
from app import btc5m_models as bm
from app.models import LiveExecution, LiveState, Market, Trade, Wallet

PRIMARY = "0x4c9497941333332d29f1c235dd23200f3623ffad"
BACKUP = "0xd9013df863c1ba932780857b020dfdeacedf8e14"


def _enable(monkeypatch, **over):
    monkeypatch.setenv("BTC5M_MICRO_TEST_ENABLED", over.get("enabled", "true"))
    monkeypatch.setenv("BTC5M_MICRO_TEST_PRIMARY_WALLET", PRIMARY)
    monkeypatch.setenv("BTC5M_MICRO_TEST_BACKUP_WALLETS", BACKUP)
    monkeypatch.setenv("BTC5M_MICRO_TEST_FIXED_SHARES", "5")
    monkeypatch.setenv("BTC5M_MICRO_TEST_MAX_ENTRY_PRICE", "0.60")
    monkeypatch.setenv("BTC5M_MICRO_TEST_MAX_CONCURRENT", "1")
    monkeypatch.setenv("BTC5M_MICRO_TEST_DAILY_LOSS_STOP", "10")
    monkeypatch.setenv("BTC5M_MICRO_TEST_TOTAL_LOSS_STOP", "15")
    monkeypatch.setenv("BTC5M_MICRO_TEST_MIN_SECONDS_REMAINING", "30")
    monkeypatch.setenv("BTC5M_MICRO_TEST_ALLOWED_REGIMES", "Hybrid,Liquidity Spike")
    monkeypatch.setenv("BTC5M_MICRO_TEST_REQUIRE_CONFIDENCE", over.get("require_conf", "false"))
    # default: exercise the deterministic research-index path (no network). V2
    # poll-path tests inject fetch_fn explicitly instead.
    monkeypatch.setenv("BTC5M_MICRO_TEST_WALLET_POLL", over.get("wallet_poll", "false"))


def _seed_signal(db, *, wallet=PRIMARY, mid="0xbtcm1", price=0.50, direction="YES",
                 question="Bitcoin Up or Down — 5 minute", secs_remaining=200,
                 age_min=1, outcomes=("Up", "Down")):
    """Seed a production Market + Btc5mMarket + a source Trade + a Btc5mTrade so the
    micro-test signal source can find a qualifying open entry."""
    now = datetime.utcnow()
    w = db.scalar(select(Wallet).where(Wallet.address == wallet))
    if not w:
        w = Wallet(address=wallet); db.add(w); db.flush()
    m = db.get(Market, mid) or Market(id=mid)
    m.question = question
    m.outcomes = list(outcomes)
    m.token_ids = ["tokUp", "tokDown"]
    m.resolved = False
    m.created_at = now - timedelta(seconds=max(0, 300 - secs_remaining))
    db.add(m)
    out = outcomes[0] if direction == "YES" else outcomes[1]
    tr = Trade(external_id=f"src-{mid}", wallet_id=w.id, market_id=mid, outcome=out,
               side="buy", price=price, size=price * 5, timestamp=now - timedelta(minutes=age_min))
    db.add(tr); db.flush()
    db.add(bm.Btc5mMarket(market_id=mid, question=question, expiry=now + timedelta(seconds=secs_remaining),
                          resolved=False, outcomes=list(outcomes)))
    db.add(bm.Btc5mTrade(source_trade_id=tr.id, external_id=f"bt-{mid}", market_id=mid,
                         wallet_address=wallet, side="buy", direction=direction, price=price,
                         shares=5, usd_value=price * 5, timestamp=now - timedelta(minutes=age_min),
                         seconds_until_expiry=secs_remaining, opened_position=True))
    db.commit()
    return m


class FakeFilled:
    """Minimal executor whose place() returns a clean filled OrderResult."""
    def place(self, *, db, market, outcome, price, size_usd, cfg):
        return live.OrderResult(outcome="filled", fill_price=price, limit_price=price,
                                filled_usd=size_usd, filled_shares=round(size_usd / price, 2),
                                fees=0.0, order_id="oid-1", order_latency_ms=1.0,
                                confirm_latency_ms=1.0, tick_size=0.01, min_order_size=5.0)


class FakeError:
    def place(self, *, db, market, outcome, price, size_usd, cfg):
        raise live.ExecutionRejected("submit: boom", outcome="submit_error", venue_error="boom")


# --- opt-in / disarmed ------------------------------------------------------
def test_disabled_never_trades(in_memory_db, monkeypatch):
    db = in_memory_db
    _enable(monkeypatch, enabled="false")
    _seed_signal(db)
    out = umt.run_once(db, place=False)
    assert out["ran"] is False and "disabled" in out["reason"]
    assert db.scalar(select(func.count()).select_from(mt.Btc5mMicroTestTrade)) == 0


def test_paper_runs_without_arm(in_memory_db, monkeypatch):
    """PAPER path requires only enabled — the arm latch protects live execution
    only, and paper can never place a real order."""
    db = in_memory_db
    _enable(monkeypatch)
    _seed_signal(db)
    out = umt.run_once(db, place=False)           # enabled, NOT armed -> paper still runs
    assert out["ran"] is True and out["mode"] == "paper"
    t = db.scalar(select(mt.Btc5mMicroTestTrade))
    assert t.executor == "paper"                  # recorded, but no real order


def test_live_requires_arm(in_memory_db, monkeypatch):
    """LIVE path (place=True) still requires the explicit arm — unchanged."""
    db = in_memory_db
    _enable(monkeypatch)
    monkeypatch.setenv("LIVE_EXECUTOR", "dry_run")
    _seed_signal(db)
    out = umt.run_once(db, place=True, executor=FakeFilled())   # enabled but never armed
    assert out["ran"] is False and "not armed" in out["reason"]
    assert db.scalar(select(func.count()).select_from(mt.Btc5mMicroTestTrade)) == 0


def test_arm_required_enabled_and_primary(in_memory_db, monkeypatch):
    db = in_memory_db
    monkeypatch.setenv("BTC5M_MICRO_TEST_ENABLED", "false")
    assert umt.arm(db)["ok"] is False             # cannot arm while disabled
    _enable(monkeypatch)
    assert umt.arm(db, by="op")["ok"] is True


# --- signal gates -----------------------------------------------------------
def test_paper_trade_fixed_5_share_sizing(in_memory_db, monkeypatch):
    db = in_memory_db
    _enable(monkeypatch); umt.arm(db)
    _seed_signal(db, price=0.50)
    out = umt.run_once(db, place=False)
    assert out["ran"] is True and out["mode"] == "paper"
    t = db.scalar(select(mt.Btc5mMicroTestTrade))
    assert t.shares == 5 and t.size_usd == 2.5      # 5 × $0.50
    assert t.strategy_mode == "btc5m_micro_test" and t.executor == "paper"


def test_non_btc5m_market_ignored(in_memory_db, monkeypatch):
    db = in_memory_db
    _enable(monkeypatch); umt.arm(db)
    _seed_signal(db, question="Will the Lakers win tonight?")   # not a BTC 5M market
    out = umt.run_once(db, place=False)
    assert out["ran"] is False
    assert db.scalar(select(func.count()).select_from(mt.Btc5mMicroTestTrade)) == 0


def test_wrong_wallet_ignored(in_memory_db, monkeypatch):
    db = in_memory_db
    _enable(monkeypatch); umt.arm(db)
    _seed_signal(db, wallet="0xdeadbeef00000000000000000000000000000000")
    out = umt.run_once(db, place=False)
    assert out["ran"] is False
    assert db.scalar(select(func.count()).select_from(mt.Btc5mMicroTestTrade)) == 0


def test_price_above_ceiling_ignored(in_memory_db, monkeypatch):
    db = in_memory_db
    _enable(monkeypatch); umt.arm(db)
    _seed_signal(db, price=0.75)                    # > 0.60 ceiling
    out = umt.run_once(db, place=False)
    assert out["ran"] is False and "max" in out["reason"]
    assert db.scalar(select(func.count()).select_from(mt.Btc5mMicroTestTrade)) == 0


def test_too_little_time_remaining_ignored(in_memory_db, monkeypatch):
    db = in_memory_db
    _enable(monkeypatch); umt.arm(db)
    _seed_signal(db, secs_remaining=10)             # < 30s remaining
    out = umt.run_once(db, place=False)
    assert out["ran"] is False and "remaining" in out["reason"]


def test_backup_wallet_used_and_logged(in_memory_db, monkeypatch):
    db = in_memory_db
    _enable(monkeypatch); umt.arm(db)
    _seed_signal(db, wallet=BACKUP, mid="0xbtcmB")
    out = umt.run_once(db, place=False)
    assert out["ran"] is True and out["role"] == "backup"
    t = db.scalar(select(mt.Btc5mMicroTestTrade))
    assert t.wallet_role == "backup" and t.wallet_triggered == BACKUP


# --- concurrency / stops (LIVE-path gates: exercised with place=True) --------
def test_one_concurrent_position_enforced(in_memory_db, monkeypatch):
    db = in_memory_db
    _enable(monkeypatch); umt.arm(db)
    monkeypatch.setenv("LIVE_EXECUTOR", "dry_run")
    _seed_signal(db, mid="0xbtcm1")
    _seed_signal(db, mid="0xbtcm2")
    assert umt.run_once(db, place=True, executor=FakeFilled())["placed"] is True   # opens 1
    out = umt.run_once(db, place=True, executor=FakeFilled())                       # second blocked
    assert out["ran"] is False and "concurrent" in out["reason"]
    assert db.scalar(select(func.count()).select_from(mt.Btc5mMicroTestTrade)
                     .where(mt.Btc5mMicroTestTrade.status == "open")) == 1


def _closed_loss(db, mid, pnl, day=True):
    now = datetime.utcnow()
    db.add(mt.Btc5mMicroTestTrade(idempotency_key=f"k-{mid}", market_id=mid, outcome="Up",
                                  direction="YES", shares=5, size_usd=2.5, status="closed",
                                  realized_pnl=pnl, won=pnl > 0,
                                  closed_at=now if day else now - timedelta(days=2)))
    db.commit()


def test_total_loss_stop_enforced(in_memory_db, monkeypatch):
    db = in_memory_db
    _enable(monkeypatch); umt.arm(db)
    for i in range(6):
        _closed_loss(db, f"loss{i}", -3.0, day=False)         # -$18 total <= -$15 stop
    _seed_signal(db)
    out = umt.run_once(db, place=True)                         # live-path stop
    assert out.get("stopped") is True
    st = umt.get_mt_state(db)
    assert st.stopped is True and st.armed is False and "total" in st.stop_reason


def test_daily_loss_stop_enforced(in_memory_db, monkeypatch):
    db = in_memory_db
    _enable(monkeypatch); umt.arm(db)
    for i in range(4):
        _closed_loss(db, f"d{i}", -3.0, day=True)             # -$12 today <= -$10 daily stop
    _seed_signal(db)
    out = umt.run_once(db, place=True)                         # live-path stop
    assert out.get("stopped") is True and "daily" in umt.get_mt_state(db).stop_reason


def test_max_trades_stop_enforced(in_memory_db, monkeypatch):
    db = in_memory_db
    _enable(monkeypatch); monkeypatch.setenv("BTC5M_MICRO_TEST_MAX_TRADES", "3"); umt.arm(db)
    for i in range(3):
        _closed_loss(db, f"t{i}", 1.0)                        # 3 settled (wins) hits the cap
    _seed_signal(db)
    out = umt.run_once(db, place=True)                         # live-path stop
    assert out.get("stopped") is True and "settled test trades" in umt.get_mt_state(db).stop_reason


# --- live path / errors / isolation ----------------------------------------
def test_live_fill_records_open_tagged_trade(in_memory_db, monkeypatch):
    db = in_memory_db
    _enable(monkeypatch); umt.arm(db)
    monkeypatch.setenv("LIVE_EXECUTOR", "dry_run")
    _seed_signal(db, price=0.40)
    out = umt.run_once(db, place=True, executor=FakeFilled())
    assert out["placed"] is True
    t = db.scalar(select(mt.Btc5mMicroTestTrade))
    assert t.status == "open" and t.fill_price == 0.40 and t.shares == 5
    assert t.strategy_mode == "btc5m_micro_test"


def test_execution_error_stops_test(in_memory_db, monkeypatch):
    db = in_memory_db
    _enable(monkeypatch); umt.arm(db)
    monkeypatch.setenv("LIVE_EXECUTOR", "dry_run")
    _seed_signal(db, price=0.40)
    out = umt.run_once(db, place=True, executor=FakeError())
    assert out.get("stopped") is True
    st = umt.get_mt_state(db)
    assert st.stopped is True and st.armed is False
    t = db.scalar(select(mt.Btc5mMicroTestTrade))
    assert t.status == "rejected" and t.venue_error == "boom"


def test_global_halt_blocks_live_micro_test(in_memory_db, monkeypatch):
    """Global halt blocks the LIVE path (place=True). Paper is unaffected (it can
    place no order)."""
    db = in_memory_db
    _enable(monkeypatch); umt.arm(db)
    monkeypatch.setenv("LIVE_EXECUTOR", "dry_run")
    st = live.get_state(db)
    st.halted = True; st.halt_reason = "paused (manual)"; db.commit()
    _seed_signal(db)
    out = umt.run_once(db, place=True, executor=FakeFilled())
    assert out["ran"] is False and "halt" in out["reason"]
    # paper still runs while global trading is halted (no real order possible)
    assert umt.run_once(db, place=False)["ran"] is True


def test_isolation_no_liveexecution_or_bankroll_change(in_memory_db, monkeypatch):
    db = in_memory_db
    _enable(monkeypatch); umt.arm(db)
    monkeypatch.setenv("LIVE_EXECUTOR", "dry_run")
    bank0 = live.get_state(db).bankroll
    _seed_signal(db, mid="0xiso1", price=0.40)
    umt.run_once(db, place=True, executor=FakeFilled())
    umt.run_once(db, place=False)                    # paper too
    # production execution + accounting are completely untouched
    assert db.scalar(select(func.count()).select_from(LiveExecution)) == 0
    assert live.get_state(db).bankroll == bank0


def test_settlement_isolated_books_pnl(in_memory_db, monkeypatch):
    db = in_memory_db
    _enable(monkeypatch); umt.arm(db)
    _seed_signal(db, mid="0xset1", price=0.40, direction="YES")
    umt.run_once(db, place=False)
    m = db.get(Market, "0xset1")
    m.resolved = True; m.resolved_outcome = "Up"; m.resolved_at = datetime.utcnow(); db.commit()
    bank0 = live.get_state(db).bankroll
    res = umt.settle(db)
    assert res["closed"] == 1
    t = db.scalar(select(mt.Btc5mMicroTestTrade))
    assert t.status == "closed" and t.won is True and t.realized_pnl == round(5 - 2.0, 2)
    assert live.get_state(db).bankroll == bank0      # production bankroll untouched


def test_status_payload_shape(in_memory_db, monkeypatch):
    db = in_memory_db
    _enable(monkeypatch)
    s = umt.status(db)
    assert s["enabled"] is True and s["armed"] is False
    assert s["config"]["primary_wallet"] == PRIMARY
    assert s["config"]["expected_max_loss_per_trade"] == 3.0   # 5 × $0.60
    assert s["config"]["fixed_shares"] == 5
    assert "latency" in s and "worker" in s                    # V2 blocks present


# ===========================================================================
# V2 — low-latency wallet-poll source, latency instrumentation, price drift
# ===========================================================================
from app.polymarket_client import TradeDTO   # noqa: E402


def _open_btc5m_market(db, mid="0xpollm1", question="Bitcoin Up or Down — 5 minute",
                       secs_remaining=200, outcomes=("Up", "Down")):
    now = datetime.utcnow()
    m = db.get(Market, mid) or Market(id=mid)
    m.question = question; m.outcomes = list(outcomes); m.token_ids = ["tokUp", "tokDown"]
    m.resolved = False; m.created_at = now - timedelta(seconds=max(0, 300 - secs_remaining))
    db.add(m)
    db.add(bm.Btc5mMarket(market_id=mid, question=question,
                          expiry=now + timedelta(seconds=secs_remaining), resolved=False,
                          outcomes=list(outcomes)))
    db.commit()
    return m


def _dto(mid, *, price=0.50, outcome="Up", side="buy", wallet=PRIMARY, age_s=3):
    return TradeDTO(external_id=f"x-{mid}", wallet_address=wallet, market_id=mid, outcome=outcome,
                    side=side, price=price, size=price * 5, shares=5,
                    timestamp=datetime.utcnow() - timedelta(seconds=age_s))


def test_wallet_poll_source_detects_with_latency(in_memory_db, monkeypatch):
    db = in_memory_db
    _enable(monkeypatch); umt.arm(db)
    _open_btc5m_market(db, mid="0xpollm1")
    fetch = {PRIMARY: [_dto("0xpollm1", price=0.50, age_s=3)], BACKUP: []}
    out = umt.run_once(db, place=False, fetch_fn=lambda a: fetch.get(a, []))
    assert out["ran"] is True and out["source"] == "wallet_poll"
    t = db.scalar(select(mt.Btc5mMicroTestTrade))
    assert t.signal_source == "wallet_poll"
    assert t.wallet_trade_at is not None and t.detected_at is not None
    assert 2.0 <= t.detection_latency_s <= 6.0          # ~3s wallet-trade age


def test_price_drift_and_missed_edge_recorded(in_memory_db, monkeypatch):
    db = in_memory_db
    _enable(monkeypatch); umt.arm(db)
    _open_btc5m_market(db, mid="0xpollm2")
    fetch = lambda a: [_dto("0xpollm2", price=0.50)] if a == PRIMARY else []
    # market moved 0.50 -> 0.55 by the time we detected (price_fn = current mid)
    out = umt.run_once(db, place=False, fetch_fn=fetch, price_fn=lambda tok: 0.55)
    assert out["ran"] is True
    t = db.scalar(select(mt.Btc5mMicroTestTrade))
    assert t.wallet_entry_price == 0.5 and t.detected_price == 0.55
    assert t.latency_cost == 0.05                       # detected - wallet entry
    assert t.missed_edge == 0.05                        # paper fill (detected) - perfect copy


def test_poll_falls_back_to_research_when_empty(in_memory_db, monkeypatch):
    db = in_memory_db
    _enable(monkeypatch); umt.arm(db)
    _seed_signal(db, mid="0xresearch1", price=0.40)     # research-index row exists
    out = umt.run_once(db, place=False, fetch_fn=lambda a: [])   # poll yields nothing
    assert out["ran"] is True and out["source"] == "research_index"


def test_latency_stats_aggregate(in_memory_db, monkeypatch):
    db = in_memory_db
    _enable(monkeypatch)
    monkeypatch.setenv("BTC5M_MICRO_TEST_MAX_CONCURRENT", "5")   # measure 3 signals at once
    umt.arm(db)
    for i, age in enumerate((2, 4, 8)):
        _open_btc5m_market(db, mid=f"0xlat{i}")
        umt.run_once(db, place=False,
                     fetch_fn=(lambda mid: (lambda a: [_dto(mid, age_s={2: 2, 4: 4, 8: 8}[age])] if a == PRIMARY else []))(f"0xlat{i}"),
                     price_fn=lambda tok: 0.50)
    ls = umt.latency_stats(db)
    assert ls["n_signals"] == 3
    assert ls["by_source"] == {"wallet_poll": 3}
    assert ls["median_detection_latency_s"] is not None
    buckets = {b["bucket"]: b["count"] for b in ls["detection_histogram"]}
    assert buckets["2-5s"] >= 1 and buckets["5-10s"] >= 1


def test_worker_inert_by_default_and_status(monkeypatch):
    from app import btc5m_micro_test_worker as w
    monkeypatch.delenv("BTC5M_MICRO_TEST_ENABLED", raising=False)
    assert w.get_config()["enabled"] is False
    assert w.start() is False                            # disabled -> never starts
    s = w.status()
    assert s["worker_running"] is False and s["place_live"] is False   # paper by default


def test_worker_defaults_to_paper(monkeypatch):
    from app import btc5m_micro_test_worker as w
    monkeypatch.setenv("BTC5M_MICRO_TEST_ENABLED", "true")
    assert w.get_config()["place_live"] is False         # never live unless explicit opt-in
    monkeypatch.setenv("BTC5M_MICRO_TEST_WORKER_PLACE_LIVE", "true")
    assert w.get_config()["place_live"] is True
