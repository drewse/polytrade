"""Paper Challenger Framework V1 tests: challenger set, immutable per-market
experiments, independent paper portfolios, paired statistical significance vs
production, champion selection, recommendations, nightly review, idempotency, and
ISOLATION from live trading / production."""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app import btc5m, challenger as ch, market_intel as mi, research
from app import challenger_models as cm
from app.models import LiveExecution, LiveState, Market, Trade, Wallet, WalletCandidate


def _seed(db, *, n_markets=40, n_wallets=12, seed=0):
    rng = random.Random(seed)
    wallets = []
    for i in range(n_wallets):
        w = Wallet(address=f"0x{i:040x}", last_active=datetime.utcnow())
        db.add(w); db.flush(); wallets.append(w)
    for mk in range(n_markets):
        created = datetime.utcnow() - timedelta(hours=mk)
        yw = (mk % 2 == 0)
        db.add(Market(id=f"btc5m-{mk}", question=f"Bitcoin Up or Down 5m #{mk}", slug=f"s-{mk}",
                      outcomes=["Up", "Down"], token_ids=["t1", "t2"], resolved=True,
                      resolved_outcome="Up" if yw else "Down", prices=[0.5, 0.5],
                      resolved_at=created + timedelta(minutes=5), created_at=created, volume=1000.0))
        for wi, w in enumerate(wallets):
            for k in range(rng.randint(1, 3)):
                t = created + timedelta(seconds=(rng.randint(5, 90) if wi < n_wallets // 2 else rng.randint(120, 250)))
                up = yw if (wi < n_wallets // 2 and rng.random() < 0.8) else (rng.random() < 0.5)
                db.add(Trade(external_id=f"tx-{mk}-{wi}-{k}", wallet_id=w.id, market_id=f"btc5m-{mk}",
                             outcome="Up" if up else "Down", side="buy",
                             price=round(rng.uniform(0.2, 0.8), 3), size=round(rng.uniform(1, 8), 2), timestamp=t))
    db.commit()
    research.research_cycle(db, limit_markets=None)
    mi.build_profiles(db); mi.wallet_regime(db); mi.originality_graph(db)


def test_seed_creates_full_challenger_set(in_memory_db):
    db = in_memory_db
    n = ch.seed_challengers(db)
    keys = {c.key for c in db.scalars(select(cm.PcChallenger)).all()}
    assert "production" in keys
    assert {"timing_+5", "timing_+10", "timing_+20", "timing_-5"} <= keys
    assert {"size_half", "size_150", "size_double", "size_kelly", "size_sharecap"} <= keys
    assert {f"conf_{c}" for c in ch.CONF_THRESHOLDS} <= keys
    assert {"cons_none", "cons_2", "cons_3", "cons_leader"} <= keys
    assert {"strat_momentum", "strat_meta_ensemble", "strat_counterfactual"} <= keys
    # production is flagged as the baseline
    prod = db.scalar(select(cm.PcChallenger).where(cm.PcChallenger.key == "production"))
    assert prod.is_production is True


def test_experiments_created_and_immutable(in_memory_db):
    db = in_memory_db
    _seed(db)
    out = ch.run_experiments(db)
    assert out["new_experiments"] > 0
    n1 = db.scalar(select(func.count()).select_from(cm.PcExperiment))
    # one experiment per market (unique); rerun creates none
    ch.run_experiments(db)
    assert db.scalar(select(func.count()).select_from(cm.PcExperiment)) == n1
    # each experiment stores production + every challenger decision + a winner
    e = db.scalars(select(cm.PcExperiment)).first()
    assert e.production_decision and e.winner and e.challenger_decisions
    assert "production" in e.challenger_decisions


def test_independent_paper_portfolios(in_memory_db):
    db = in_memory_db
    _seed(db)
    ch.run_experiments(db); ch.rebuild_portfolios(db)
    challengers = db.scalars(select(cm.PcChallenger)).all()
    assert any(c.trades > 0 for c in challengers)
    # every paper trade belongs to exactly its own challenger (no mixing)
    for c in challengers:
        owned = db.scalars(select(cm.PcTrade).where(cm.PcTrade.challenger_id == c.id)).all()
        assert all(t.challenger_id == c.id for t in owned)
        assert len(owned) == c.trades
    # metrics + vs_production present
    nonprod = next(c for c in challengers if not c.is_production)
    assert "roi" in nonprod.metrics and "significance" in nonprod.vs_production


def test_statistical_significance_buckets():
    assert ch._significance([0.1] * 5)["significance"] == "Insufficient Data"   # too few
    strong = ch._significance([1.0] * 40)                                       # consistent positive
    assert strong["significance"] == "Significant" and strong["mean_improvement"] == 1.0
    reg = ch._significance([-1.0] * 40)
    assert reg["significance"] == "Regressing"


def test_champion_and_recommendations(in_memory_db):
    db = in_memory_db
    _seed(db)
    out = ch.run_challengers(db, refresh_lab=False)
    assert out["champion"] is not None
    champs = [c for c in db.scalars(select(cm.PcChallenger)).all() if c.is_champion]
    assert len(champs) == 1
    # recommendations cite improvement + sample (only for significant/promising winners)
    recs = ch.recommendations(db)
    for r in recs:
        assert r["significance"] in ("Significant", "Promising")
        assert "outperformed production" in r["text"]


def test_nightly_review_permanent(in_memory_db):
    db = in_memory_db
    _seed(db)
    ch.run_challengers(db, refresh_lab=False)
    review = db.scalar(select(cm.PcNightlyReview))
    assert review is not None
    for k in ("experiments", "timing_improvement", "sizing_improvement", "overall_champion", "ready_for_manual_review"):
        assert k in review.report
    ch.nightly_review(db)
    assert db.scalar(select(func.count()).select_from(cm.PcNightlyReview)) == 2   # append-only


def test_regime_aware_by_regime(in_memory_db):
    db = in_memory_db
    _seed(db)
    ch.run_challengers(db, refresh_lab=False)
    c = db.scalar(select(cm.PcChallenger).where(cm.PcChallenger.is_production.is_(False),
                                                cm.PcChallenger.trades > 0))
    assert c.by_regime and all("improvement_pct" in v for v in c.by_regime.values())


# --- SAFETY: isolation -------------------------------------------------------
def test_challenger_never_touches_live_or_production(in_memory_db):
    db = in_memory_db
    db.add(LiveState(id=1, starting_bankroll=40.0, bankroll=40.0, halted=False))
    w = Wallet(address="0xprod", copy_enabled=True, last_active=datetime.utcnow())
    db.add(w); db.flush()
    db.add(WalletCandidate(wallet_id=w.id, copyability_score=70.0, classification="good_candidate"))
    db.commit()
    _seed(db)
    state_before = (db.get(LiveState, 1).bankroll, db.get(LiveState, 1).halted)
    cand_before = db.get(WalletCandidate, w.id).classification
    copy_before = db.get(Wallet, w.id).copy_enabled

    ch.run_challengers(db, refresh_lab=False)

    assert db.scalar(select(func.count()).select_from(LiveExecution)) == 0
    assert (db.get(LiveState, 1).bankroll, db.get(LiveState, 1).halted) == state_before
    assert db.get(WalletCandidate, w.id).classification == cand_before
    assert db.get(Wallet, w.id).copy_enabled == copy_before
