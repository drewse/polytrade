"""Tests for the historical replay engine (Phases 21-29):
no-look-ahead reputation, checkpoint/resume, labeled feature vectors,
probability benchmark, regimes, drift, wallet evolution."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app import top20
from app.models import Market, Top20FeatureVector, Top20Trade, Trade, Wallet, WalletCandidate, WalletStat
from app.top20 import benchmark, replay


# --- Phase 29: probability benchmark (pure) ---------------------------------
def test_brier_logloss_auc():
    assert benchmark.brier([1, 0], [1.0, 0.0]) == 0.0
    assert benchmark.brier([1, 0], [0.0, 1.0]) == 1.0
    assert benchmark.log_loss([1, 1], [0.99, 0.99]) < benchmark.log_loss([1, 1], [0.5, 0.5])
    assert benchmark.roc_auc([1, 1, 0, 0], [0.9, 0.8, 0.2, 0.1]) == 1.0   # separable
    assert benchmark.roc_auc([1, 1, 0, 0], [0.1, 0.2, 0.8, 0.9]) == 0.0   # reversed
    assert benchmark.roc_auc([1, 1], [0.9, 0.8]) is None                   # one class


def test_calibration_and_compute():
    ys = [1] * 30 + [0] * 30
    cur = [0.8] * 30 + [0.2] * 30          # well separated
    samples = [{"y": y, "current": c, "market": 0.5, "wallet": c, "edge": c}
               for y, c in zip(ys, cur)]
    out = benchmark.compute(samples)
    assert out["n"] == 60
    assert out["estimators"]["current"]["brier"] < out["estimators"]["random"]["brier"]
    assert out["best_by_brier"] in ("current", "wallet_only", "edge_only")
    assert len(out["reliability"]["diagram"]) == 10
    assert "ece" in out["reliability"]


# --- Phase 26: point-in-time reputation (no look-ahead) ---------------------
def _seed_resolved(db, wid, mid, res_day, won=True, price=0.4, size=40.0, liq=5000):
    base = datetime(2026, 1, 1)
    db.add(Market(id=mid, question="Will BTC moon?", outcomes=["Yes", "No"],
                  prices=[1.0, 0.0] if won else [0.0, 1.0], liquidity=liq, resolved=True,
                  resolved_outcome="Yes", resolved_at=base + timedelta(days=res_day)))
    db.flush()
    db.add(Trade(wallet_id=wid, market_id=mid, outcome="Yes", side="buy", price=price,
                 size=size, timestamp=base + timedelta(days=res_day - 0.5)))


def test_point_in_time_no_lookahead(in_memory_db):
    db = in_memory_db
    w = Wallet(address="0xpit", copy_enabled=True)
    db.add(w); db.flush()
    for i in range(1, 7):                 # 6 positions resolving on days 1..6
        _seed_resolved(db, w.id, f"m{i}", res_day=i)
    db.commit()
    tl = replay._build_wallet_timelines(db)[w.id]
    base = datetime(2026, 1, 1)
    # at day 3.5 only positions resolved on days 1,2,3 are visible (=3)
    assert replay._point_in_time(tl, base + timedelta(days=3.5))["n"] == 3
    assert replay._point_in_time(tl, base + timedelta(days=0.5))["n"] == 0   # nothing yet
    assert replay._point_in_time(tl, base + timedelta(days=10))["n"] == 6    # all visible


def test_replay_produces_labeled_feature_vectors(in_memory_db):
    db = in_memory_db
    top20.ensure_strategies(db)
    w = Wallet(address="0xrep", copy_enabled=True)
    db.add(w); db.flush()
    # 6 prior winning resolved positions (days 1..6) -> qualifies by day 10
    for i in range(1, 7):
        _seed_resolved(db, w.id, f"h{i}", res_day=i)
    # candidate trade at day 10 on a market resolving day 30
    base = datetime(2026, 1, 1)
    db.add(Market(id="cand", question="Will the Lakers win?", outcomes=["Yes", "No"],
                  prices=[1.0, 0.0], liquidity=8000, resolved=True, resolved_outcome="Yes",
                  resolved_at=base + timedelta(days=30)))
    db.flush()
    db.add(Trade(wallet_id=w.id, market_id="cand", outcome="Yes", side="buy", price=0.5,
                 size=60.0, timestamp=base + timedelta(days=10)))
    db.commit()

    res = replay.run(db, max_trades=100)
    assert res["feature_vectors_added"] >= 1
    fvs = db.scalars(select(Top20FeatureVector).where(Top20FeatureVector.source == "replay")).all()
    assert fvs and all(fv.settled and fv.label_realized_return is not None for fv in fvs)
    assert all("resolution_result" in fv.features and "holding_minutes" in fv.features for fv in fvs)
    rt = db.scalars(select(Top20Trade).where(Top20Trade.source == "replay")).all()
    assert rt and all(t.status == "closed" and t.holding_minutes is not None for t in rt)


def test_replay_checkpoint_resume(in_memory_db):
    db = in_memory_db
    top20.ensure_strategies(db)
    w = Wallet(address="0xck", copy_enabled=True)
    db.add(w); db.flush()
    for i in range(1, 7):
        _seed_resolved(db, w.id, f"c{i}", res_day=i)
    base = datetime(2026, 1, 1)
    db.add(Market(id="cand2", question="Sports match winner?", outcomes=["Yes", "No"],
                  prices=[1.0, 0.0], liquidity=8000, resolved=True, resolved_outcome="Yes",
                  resolved_at=base + timedelta(days=30)))
    db.flush()
    db.add(Trade(wallet_id=w.id, market_id="cand2", outcome="Yes", side="buy", price=0.5,
                 size=60.0, timestamp=base + timedelta(days=10)))
    db.commit()
    first = replay.run(db, max_trades=100)["feature_vectors_added"]
    # second run starts past the checkpoint -> no new vectors (no duplication)
    second = replay.run(db, max_trades=100)["feature_vectors_added"]
    assert first >= 1 and second == 0


# --- Phase 27/28: drift + regimes (DB) --------------------------------------
def test_drift_and_regimes_and_benchmark(in_memory_db):
    db = in_memory_db
    top20.ensure_strategies(db)
    w = Wallet(address="0xdr", copy_enabled=True)
    db.add(w); db.flush()
    for i in range(1, 7):
        _seed_resolved(db, w.id, f"d{i}", res_day=i)
    base = datetime(2026, 1, 1)
    db.add(Market(id="dcand", question="Bitcoin above 100k?", outcomes=["Yes", "No"],
                  prices=[1.0, 0.0], liquidity=8000, resolved=True, resolved_outcome="Yes",
                  resolved_at=base + timedelta(days=40)))
    db.flush()
    db.add(Trade(wallet_id=w.id, market_id="dcand", outcome="Yes", side="buy", price=0.5,
                 size=60.0, timestamp=base + timedelta(days=10)))
    db.commit()
    replay.run(db, max_trades=100)

    drift = top20.strategy_drift(db)
    assert "months" in drift and "decay" in drift
    regimes = top20.market_regimes(db)
    assert "monthly_regimes" in regimes and "regime_performance" in regimes
    bench = top20.probability_benchmark(db)
    assert "estimators" in bench and "current" in bench.get("estimators", {})
    ev = top20.wallet_evolution(db, "0xdr")
    # 6 prior + the candidate market (also resolved) = 7 settled positions
    assert ev and len(ev["points"]) == 7
    assert [p["n_settled"] for p in ev["points"]] == [1, 2, 3, 4, 5, 6, 7]  # monotonic


def test_replay_status_targets(in_memory_db):
    st = top20.replay_status(in_memory_db)
    assert st["paper_only"] is True
    assert st["targets"]["feature_vectors"] == 10000
    assert "resolved_markets" in st and "checkpoint_trade_id" in st
