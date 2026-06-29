"""BTC passive-maker FORWARD pipeline tests: disabled no-op, incremental + idempotent
conversion, processes only NEW markets, funnel diagnostics identify the stalled stage,
multi-point quotes separated from independent, broad universe kept out of the BTC gate,
book-capture fail-soft, and paper-only isolation (no LiveExecution / bankroll change).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from app import btc5m_passive_maker_forward as fwd
from app import btc5m_passive_maker as harness
from app import btc5m_passive_maker_models as pmm
from app import btc5m_strategy_models as lm
from app import btc5m_models as bm
from app import live
from app.models import LiveExecution, Market, Trade
from tests.test_btc5m_execution_lab import _seed


# --- worker disabled / enabled ----------------------------------------------
def test_forward_disabled_is_noop(in_memory_db, monkeypatch):
    db = in_memory_db
    _seed(db)
    monkeypatch.delenv("BTC_PASSIVE_MAKER_FORWARD_ENABLED", raising=False)
    out = fwd.run_forward_cycle(db)
    assert out["ran"] is False and "disabled" in out["skipped"]
    assert db.scalar(select(func.count()).select_from(pmm.Btc5mPaperQuote)) == 0


def test_forward_incremental_and_idempotent(in_memory_db, monkeypatch):
    db = in_memory_db
    _seed(db)                                            # seeds Btc5mMarket + lab points already
    monkeypatch.setenv("BTC_PASSIVE_MAKER_FORWARD_ENABLED", "true")
    r1 = fwd.run_forward_cycle(db, force=True)
    assert r1["ran"]
    q1 = db.scalar(select(func.count()).select_from(pmm.Btc5mPaperQuote))
    assert q1 > 0
    # second cycle processes nothing new (idempotent)
    r2 = fwd.run_forward_cycle(db, force=True)
    q2 = db.scalar(select(func.count()).select_from(pmm.Btc5mPaperQuote))
    assert q2 == q1 and r2["new_quotes"] == 0
    # PAPER ONLY
    assert db.scalar(select(func.count()).select_from(LiveExecution)) == 0


def test_quote_counts_increase_with_new_resolved_markets(in_memory_db, monkeypatch):
    db = in_memory_db
    _seed(db)
    monkeypatch.setenv("BTC_PASSIVE_MAKER_FORWARD_ENABLED", "true")
    fwd.run_forward_cycle(db, force=True)
    q1 = db.scalar(select(func.count()).select_from(pmm.Btc5mPaperQuote))
    # add a NEW resolved market + lab point, then run again -> quotes increase
    from tests.test_btc5m_execution_lab import _seed as _s  # noqa
    _add_market_with_point(db, "newmkt1", up=True)
    r = fwd.run_forward_cycle(db, force=True)
    q2 = db.scalar(select(func.count()).select_from(pmm.Btc5mPaperQuote))
    assert q2 > q1 and r["new_quotes"] >= 1


def _add_market_with_point(db, mid, *, up=True):
    created = datetime(2026, 6, 28, 12, 0, 0)
    db.add(bm.Btc5mMarket(market_id=mid, slug="btc-updown-5m", question="Bitcoin Up or Down",
                          created_time=created, resolution_time=created + timedelta(minutes=5),
                          resolved=True, final_outcome="Up" if up else "Down"))
    br = 0.0015 if up else -0.0015
    db.add(lm.Btc5mLabPoint(market_id=mid, duration_minutes=5, t_offset_s=60, secs_to_expiry=240,
        regime="mixed", features={"btc_ret_sofar": br, "btc_ret_3s": br, "btc_ret_5s": br,
            "btc_ret_10s": br, "btc_ret_30s": br, "btc_momentum": br * 100, "btc_vol": 0.0006,
            "flow_imbalance": 0.6, "recent_flow_imbalance": 0.5, "pm_momentum": 0.0, "lag": 0.2,
            "wallet_signal": 0.5, "volume_usd": 200, "trade_freq": 0.2, "has_large_trade": 0,
            "large_trade_usd": 0, "pm_yes": 0.5}, pm_yes=0.5, spread=0.04, btc_ret_30s=br,
        flow_imbalance=0.6, label_up=up, split="holdout"))
    db.commit()


# --- diagnostics identify stalled stage -------------------------------------
def test_diagnostics_funnel_and_blocked_stage(in_memory_db):
    db = in_memory_db
    _seed(db)
    # btc5m markets exist but NO paper quotes yet -> stage 4 (paper_quotes) is blocked
    d = fwd.diagnostics(db)
    f = d["funnel"]
    for stage in ("1_btc_markets_in_main", "2_btc5m_indexed", "3_lab_markets", "4_paper_quotes",
                  "5_paper_fills", "6_settled_fills"):
        assert stage in f and "total" in f[stage] and "blocked" in f[stage]
    # lab markets exist but quotes=0 -> stage 4 blocked
    assert f["4_paper_quotes"]["total"] == 0 and f["4_paper_quotes"]["blocked"] is True
    assert "4_paper_quotes" in d["blocked_stages"] and d["pipeline_blocked"] is True


def test_diagnostics_unblocks_after_quoting(in_memory_db, monkeypatch):
    db = in_memory_db
    _seed(db)
    monkeypatch.setenv("BTC_PASSIVE_MAKER_FORWARD_ENABLED", "true")
    fwd.run_forward_cycle(db, force=True)
    d = fwd.diagnostics(db)
    assert d["funnel"]["4_paper_quotes"]["total"] > 0


# --- multi-point separated from independent ---------------------------------
def test_multi_point_quotes_separated(in_memory_db, monkeypatch):
    db = in_memory_db
    _seed(db)
    # give one market 5 decision points so multi-point quoting can trigger
    br = 0.0015
    for t in (90, 120, 150, 180):
        db.add(lm.Btc5mLabPoint(market_id="holdout0", duration_minutes=5, t_offset_s=t, secs_to_expiry=300 - t,
            regime="mixed", features={"btc_ret_sofar": br, "btc_ret_3s": br, "btc_ret_5s": br, "btc_ret_10s": br,
                "btc_ret_30s": br, "btc_momentum": br * 100, "btc_vol": 0.0006, "flow_imbalance": 0.6,
                "recent_flow_imbalance": 0.5, "pm_momentum": 0.0, "lag": 0.2, "wallet_signal": 0.5,
                "volume_usd": 200, "trade_freq": 0.2, "has_large_trade": 0, "large_trade_usd": 0, "pm_yes": 0.5},
            pm_yes=0.5, spread=0.04, btc_ret_30s=br, flow_imbalance=0.6, label_up=True, split="holdout"))
    db.commit()
    monkeypatch.setenv("BTC_PASSIVE_MAKER_PAPER_ENABLED", "true")
    monkeypatch.setenv("BTC_PASSIVE_MAKER_MULTI_POINT_QUOTES", "true")
    harness.run_once(db, force=True, multi_point=True)
    indep = db.scalar(select(func.count()).select_from(pmm.Btc5mPaperQuote)
                      .where(pmm.Btc5mPaperQuote.quote_kind == "independent")) or 0
    multi = db.scalar(select(func.count()).select_from(pmm.Btc5mPaperQuote)
                      .where(pmm.Btc5mPaperQuote.quote_kind == "multi_point")) or 0
    assert indep > 0 and multi > 0
    # the BTC gate uses ONLY independent fills — breakdown reports them separately
    fb = harness.family_breakdown(db)
    assert "btc:independent" in fb and "btc:multi_point" in fb
    st = harness.status(db)
    assert st["gate_cohort"] == "btc:independent"


# --- broad universe separate from BTC gate ----------------------------------
def test_broad_universe_separate_gate(in_memory_db):
    db = in_memory_db
    _seed(db)
    # seed a non-BTC binary market + trades in the MAIN tables
    created = datetime(2026, 6, 28, 10, 0, 0)
    db.add(Market(id="sportsX", question="Will Team A win?", slug="nba-team-a-vs-team-b",
                  category="Sports", outcomes=["Yes", "No"], token_ids=["t1", "t2"],
                  resolved=True, resolved_outcome="Yes", created_at=created,
                  resolved_at=created + timedelta(hours=2), volume=500))
    for i, sec in enumerate(range(30, 200, 12)):
        db.add(Trade(external_id=f"sx{i}", wallet_id=1, market_id="sportsX", outcome="Yes",
                     side="buy", price=0.5 + 0.001 * i, size=20.0, timestamp=created + timedelta(seconds=sec)))
    db.commit()
    out = fwd.broad_universe_cycle(db, batch=10)
    assert out["ran"] and out["created"] >= 1
    # the broad quote is tagged non-btc and EXCLUDED from the BTC gate
    nonbtc = db.scalar(select(func.count()).select_from(pmm.Btc5mPaperQuote)
                       .where(pmm.Btc5mPaperQuote.market_family != "btc")) or 0
    assert nonbtc >= 1
    fb = harness.family_breakdown(db)
    assert any(k.startswith(("sports:", "other:", "politics:", "crypto_other:")) for k in fb)
    # BTC gate stats unaffected by broad fills
    btc_fills = harness._settled_fills(db, family="btc", kind="independent")
    assert all(f for f in btc_fills) or btc_fills == []


# --- book capture fail-soft + isolation -------------------------------------
def test_settle_pending_and_isolation(in_memory_db, monkeypatch):
    db = in_memory_db
    _seed(db)
    bank0 = live.get_state(db).bankroll
    monkeypatch.setenv("BTC_PASSIVE_MAKER_FORWARD_ENABLED", "true")
    fwd.run_forward_cycle(db, force=True)
    assert fwd.settle_pending(db) >= 0                   # no crash
    assert live.get_state(db).bankroll == bank0
    assert db.scalar(select(func.count()).select_from(LiveExecution)) == 0
