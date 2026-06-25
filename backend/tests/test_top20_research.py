"""Tests for the research platform (Phases 11-20) + quality fixes."""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select

from app import top20
from app.models import Market, PaperSignal, Top20FeatureVector, Top20Strategy, Wallet, WalletCandidate, WalletStat
from app.top20 import analytics, ensembles, market_intel, montecarlo, optimize, reputation, report, simulate
from app.top20.engine import _param_hash


def _pos(pnl, size, days_ago, cat="Crypto", now=None):
    now = now or datetime.utcnow()
    return SimpleNamespace(realized_pnl=pnl, size=size, timestamp=now - timedelta(days=days_ago),
                           market=SimpleNamespace(category=cat))


# --- Phase 11: reproducibility ---------------------------------------------
def test_param_hash_reproducible():
    p = {"a": 1, "b": [2, 3], "c": {"d": 4}}
    assert _param_hash(p) == _param_hash(dict(p))  # identical params -> identical hash
    assert _param_hash(p) != _param_hash({"a": 2})


def test_decide_is_deterministic():
    from app.top20.strategies import CONFIG_BY_KEY, Ctx, Shared
    ctx = Ctx(1, "good_candidate", 85, 0.1, 5000, 5, 0.5, "Yes", "m", "Crypto",
              0.7, 1.0, 0.2, 90, 0.2, 0.8, 50)
    sh = Shared({1: 0}, {1: 0}, {1: 0}, {1: 0}, 0.05, set())
    d = CONFIG_BY_KEY["top5"]
    assert top20.strategies.decide(d, ctx, sh) == top20.strategies.decide(d, ctx, sh)


# --- Phase 14: Monte Carlo --------------------------------------------------
def test_montecarlo_reproducible_and_bounded():
    pnls = [100, -50, 200, -30, 150, -80, 90, -40, 60, -20]
    a = montecarlo.simulate(pnls, 10_000, sims=500, seed=7)
    b = montecarlo.simulate(pnls, 10_000, sims=500, seed=7)
    assert a == b                                   # reproducible
    assert 0.0 <= a["probability_of_ruin"] <= 1.0
    assert a["drawdown_p95"] >= a["expected_drawdown"]
    c = montecarlo.simulate(pnls, 10_000, sims=500, seed=99)
    assert c["seed"] == 99


def test_montecarlo_empty():
    assert montecarlo.simulate([], 10_000)["sims"] == 0


# --- Phase 12: parameter optimization ---------------------------------------
def _samples(n=40):
    """High-confidence signals win; low-confidence lose -> optimizer should
    prefer a higher confidence threshold."""
    base = datetime(2026, 1, 1)
    out = []
    for i in range(n):
        hi = i % 2 == 0
        out.append(simulate.Sample(
            created_at=base + timedelta(hours=i), market_id=f"m{i}", outcome="Yes",
            resolved_outcome="Yes" if hi else "No", price=0.5,
            confidence=90 if hi else 60, edge=0.1 if hi else 0.0, liquidity=5000,
            category="Crypto", win_rate=0.7, sharpe=1.0, roi=0.2, copyability=80,
            classification="good_candidate", specialization=0.2, recency=0.8,
            num_settled=50, wallet_id=1))
    return out


def test_parameter_optimization():
    res = optimize.optimize(_samples(), "confidence")
    assert len(res["results"]) == len(optimize.GRIDS["confidence"])
    assert res["best"] is not None
    # higher-confidence threshold should not be worse than the lowest
    assert res["best"]["value"] >= 70


# --- Phase 13: walk-forward -------------------------------------------------
def test_walk_forward_stability():
    res = optimize.walk_forward(_samples(60), "confidence", windows=3)
    assert not res.get("insufficient_data")
    assert "avg_forward_sharpe" in res and "parameter_stability" in res
    assert "verdict" in res and 0 <= res["parameter_stability"] <= 1


# --- Phase 15: wallet reputation decay --------------------------------------
def test_reputation_decay_weights_recent_more():
    now = datetime.utcnow()
    # recent win, old loss of equal magnitude -> decayed ROI should be positive
    positions = [_pos(50, 100, 1, now=now), _pos(-50, 100, 200, now=now)]
    rep = reputation.compute(positions, now=now)
    assert rep["lifetime_roi"] == 0.0           # raw nets to zero
    assert rep["decayed_roi"] > 0.0             # decay favors the recent win
    assert rep["half_life_days"] == 30.0


def test_reputation_empty():
    assert reputation.compute([])["insufficient_data"]


# --- Phase 16: market intelligence ------------------------------------------
def test_market_intelligence():
    recs = [{"category": "Crypto", "edge": 0.1, "won": 1, "price": 0.4,
             "realized_return": 1.5, "ttr_hours": 10} for _ in range(6)]
    recs += [{"category": "Sports", "edge": 0.0, "won": 0, "price": 0.6,
              "realized_return": -1.0, "ttr_hours": 50} for _ in range(6)]
    mi = market_intel.compute(recs)
    assert mi["best_categories"][0]["category"] == "Crypto"
    assert mi["categories"][0]["edge_persistence"] >= 0


# --- Phase 17: ensembles ----------------------------------------------------
def test_ensemble_weighting():
    strats = [{"key": f"s{i}", "name": f"S{i}", "returns": [0.1, -0.05, 0.2],
               "metrics": {"sharpe": i * 0.5, "total_pnl": i * 10, "max_drawdown": 0.1}}
              for i in range(6)]
    out = ensembles.compute(strats)
    methods = {e["method"] for e in out["ensembles"]}
    assert "equal_weight" in methods and "top5_sharpe" in methods
    top5 = next(e for e in out["ensembles"] if e["method"] == "top5_sharpe")
    assert top5["n_strategies"] == 5
    eq = next(e for e in out["ensembles"] if e["method"] == "equal_weight")
    assert abs(sum(eq["weights"].values()) - 1.0) < 0.01  # rounded weights


# --- Phase 19: report -------------------------------------------------------
def test_report_generation():
    md = report.generate({"date": "2026-06-25", "best_strategy": {"name": "Alpha", "score": 80},
                          "open_risk": {"open_exposure": 100, "capital_utilization": 0.1, "open_positions": 3}})
    assert "Research Report" in md and "Open risk" in md and "Alpha" in md


# --- Quality fixes ----------------------------------------------------------
def test_sharpe_confidence_interval():
    ci = analytics.sharpe_ci([0.1, -0.05, 0.2, 0.0, 0.15])
    assert len(ci) == 2 and ci[0] <= ci[1]
    assert analytics.sharpe_ci([0.1]) == [0.0, 0.0]


def test_cagr_not_extrapolated_for_short_spans():
    now = datetime(2026, 1, 1, 12, 0)
    closed = [SimpleNamespace(realized_pnl=100, unrealized_pnl=0, stake=100, kelly_fraction=0.2,
                              entry_time=now, closed_at=now, status="closed") for _ in range(3)]
    m = analytics.compute_metrics(closed, [], [10000, 10300], 10000, 3, 3,
                                  first_ts=now, last_ts=now + timedelta(hours=2))
    assert m["annualized_valid"] is False and m["annualized_return"] == 0.0
    assert m["insufficient_history"] is True


def test_drawdown_no_artificial_spike():
    # steadily rising equity -> 0 drawdown (no false reset spike)
    assert analytics.max_drawdown([10000, 10100, 10200, 10300]) == 0.0


# --- Phase 18 + 20: DB integration ------------------------------------------
def _seed_signal(db, conf=95, edge=0.12, price=0.5):
    w = Wallet(address="0xresw", copy_enabled=True)
    db.add(w); db.flush()
    db.add(WalletStat(wallet_id=w.id, num_trades=300, score=80, win_rate=0.8, realized_roi=0.2,
                      consistency=0.7, num_settled=200, classification="sharp"))
    db.add(WalletCandidate(wallet_id=w.id, copyability_score=90, classification="good_candidate"))
    db.add(Market(id="0xmktR", question="Will BTC top $100k?", outcomes=["Yes", "No"],
                  prices=[price, 1 - price], liquidity=20000, resolved=False))
    db.flush()
    s = PaperSignal(wallet_id=w.id, market_id="0xmktR", outcome="Yes", side="buy",
                    observed_price=price, suggested_entry=price, confidence=conf,
                    edge_estimate=edge, reason="t", created_at=datetime.utcnow())
    db.add(s); db.commit()
    return s


def test_feature_vectors_captured_and_labeled(in_memory_db):
    db = in_memory_db
    _seed_signal(db)
    top20.run_cycle(db)
    fvs = db.scalars(select(Top20FeatureVector)).all()
    assert fvs and all(fv.decision == "take" for fv in fvs)
    assert all("estimated_probability" in fv.features for fv in fvs)
    assert all(not fv.settled for fv in fvs)  # not labeled until resolution
    # resolve and settle
    m = db.get(Market, "0xmktR"); m.resolved = True; m.resolved_outcome = "Yes"; db.commit()
    top20.run_cycle(db)
    labeled = db.scalars(select(Top20FeatureVector).where(Top20FeatureVector.settled == True)).all()
    assert labeled and all(fv.label_realized_return is not None for fv in labeled)
    ds = top20.feature_vectors(db, settled_only=True)
    assert ds["labeled"] >= 1


def test_strategy_retirement_and_status(in_memory_db):
    db = in_memory_db
    top20.ensure_strategies(db)
    rec = top20.recommend_retirements(db)
    assert "recommendations" in rec and rec["min_sample"] == 20
    out = top20.set_status(db, "top5", "retired")
    assert out["status"] == "retired" and out["active"] is False
    assert top20.set_status(db, "top5", "bogus") is None


def test_research_endpoints_run(in_memory_db):
    db = in_memory_db
    _seed_signal(db)
    top20.run_cycle(db)
    assert top20.market_intelligence(db) is not None
    assert "ensembles" in top20.ensemble_view(db)
    assert "markdown" in top20.research_report(db, "2026-06-25")
    mc = top20.monte_carlo(db, db.scalar(select(Top20Strategy.id)))
    assert "probability_of_ruin" in mc or "insufficient_data" in mc
