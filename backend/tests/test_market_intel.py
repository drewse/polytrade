"""Market Intelligence & Regime Engine V1 tests: market profiles, regime
classification (discriminates trend vs range), wallet/strategy specialization,
decay, originality, counterfactual, recommendations, nightly review, idempotent
batch, and ISOLATION from live trading / production."""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app import btc5m, market_intel as mi, research
from app import market_intel_models as mim
from app.models import LiveExecution, LiveState, Market, Trade, Wallet, WalletCandidate


def _seed(db, *, n_wallets=12, seed=0):
    """Seed BTC5m markets with DISTINCT price paths: even markets trend UP
    (rising implied prob), odd markets are range-bound near 0.5. Then refresh the
    Lab so features/fingerprints exist."""
    rng = random.Random(seed)
    wallets = []
    for i in range(n_wallets):
        w = Wallet(address=f"0x{i:040x}", last_active=datetime.utcnow())
        db.add(w); db.flush(); wallets.append(w)
    for mk in range(20):
        created = datetime.utcnow() - timedelta(hours=mk)
        trending = (mk % 2 == 0)
        up = trending or (rng.random() < 0.5)
        m = Market(id=f"btc5m-{mk}", question=f"Bitcoin Up or Down 5m #{mk}", slug=f"s-{mk}",
                   outcomes=["Up", "Down"], token_ids=["t1", "t2"], resolved=True,
                   resolved_outcome="Up" if up else "Down", prices=[0.5, 0.5],
                   resolved_at=created + timedelta(minutes=5), created_at=created, volume=1000.0)
        db.add(m)
        for step, wi in enumerate(range(n_wallets)):
            w = wallets[wi]
            secs = 10 + step * 18
            if trending:
                price = round(0.30 + 0.03 * step, 3)        # rises 0.30 -> ~0.63
            else:
                price = round(0.50 + (0.01 if step % 2 else -0.01), 3)   # flat ~0.50
            db.add(Trade(external_id=f"tx-{mk}-{wi}", wallet_id=w.id, market_id=m.id,
                         outcome="Up", side="buy", price=price, size=round(rng.uniform(1, 8), 2),
                         timestamp=created + timedelta(seconds=secs)))
    db.commit()
    btc5m.refresh(db, limit_markets=None)


# --- Phase 1 + 2: profiles + regime classification --------------------------
def test_profiles_and_regime_discriminates(in_memory_db):
    db = in_memory_db
    _seed(db)
    out = mi.build_profiles(db)
    assert out["profiles"] == 20
    profs = db.scalars(select(mim.MiMarketProfile)).all()
    assert all(p.primary_regime in mi.REGIMES for p in profs)
    assert all("opening_prob" in p.price and "total_volume" in p.volume for p in profs)
    # trending markets carry a much larger net move than the range-bound ones
    trend_moves = [abs(p.price["net_move"]) for p in profs if p.market_id.endswith(tuple("02468"))]
    range_moves = [abs(p.price["net_move"]) for p in profs if not p.market_id.endswith(tuple("02468"))]
    assert _mean(trend_moves) > _mean(range_moves)
    # the classifier produces at least two DISTINCT regimes (not all Mixed)
    regimes = {p.primary_regime for p in profs}
    assert len(regimes) >= 2


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


# --- Phase 3 + 8: wallet specialization + position size ---------------------
def test_wallet_regime_specialization(in_memory_db):
    db = in_memory_db
    _seed(db)
    mi.build_profiles(db)
    out = mi.wallet_regime(db)
    assert out["wallets"] > 0
    w = db.scalars(select(mim.MiWalletRegime)).first()
    assert w.by_regime and isinstance(w.by_regime, dict)
    assert "avg_stake" in w.position_size and "stake_percentile" in w.position_size
    assert "7d" in w.decay and "trend" in w.decay


# --- Phase 4: strategy heatmap ----------------------------------------------
def test_strategy_regime_heatmap(in_memory_db):
    db = in_memory_db
    _seed(db)
    research.research_cycle(db, limit_markets=None)      # strategies + paper trades
    mi.build_profiles(db)
    out = mi.strategy_regime(db)
    assert out["strategies"] > 0
    s = db.scalars(select(mim.MiStrategyRegime)).first()
    assert s.by_regime and s.best_regime is not None
    # each regime cell carries the full metric set
    cell = next(iter(s.by_regime.values()))
    for k in ("win_rate", "roi", "profit_factor", "max_drawdown", "trades", "expected_value", "confidence"):
        assert k in cell


# --- Phase 6: decay ---------------------------------------------------------
def test_decay_classifies_trend(in_memory_db):
    db = in_memory_db
    _seed(db)
    mi.build_profiles(db); mi.wallet_regime(db)
    da = mi.decay_analysis(db)
    assert "wallets" in da and "strategies" in da
    for w in da["wallets"]:
        assert w["trend"] in ("improving", "stable", "decaying", "broken")


# --- Phase 7: originality (leaders rank above followers) --------------------
def test_originality_graph(in_memory_db):
    db = in_memory_db
    _seed(db)
    mi.build_profiles(db); mi.wallet_regime(db)
    og = mi.originality_graph(db)
    assert og["nodes"] > 0
    rows = mi.originality(db)["wallets"]
    if len(rows) >= 2:
        # sorted by originality score descending -> leaders/independent first
        assert rows[0]["originality_score"] >= rows[-1]["originality_score"]


# --- Phase 9: counterfactual ------------------------------------------------
def test_counterfactual_timing(in_memory_db):
    db = in_memory_db
    _seed(db)
    out = mi.counterfactual(db, sample=100)
    assert out["trades_tested"] > 0
    assert set(out["timing_sensitivity"].keys()) <= {str(s) for s in mi.SHIFTS}
    assert db.scalar(select(func.count()).select_from(mim.MiCounterfactual)) == 1


# --- Phase 10 + 11: recommendations + nightly review ------------------------
def test_recommendations_and_nightly_review(in_memory_db):
    db = in_memory_db
    _seed(db)
    research.research_cycle(db, limit_markets=None)
    mi.run_intel_batch(db, refresh_lab=False, limit_markets=None)
    assert db.scalar(select(func.count()).select_from(mim.MiRecommendation)) > 0
    review = db.scalar(select(mim.MiNightlyReview))
    assert review is not None and "regime_distribution" in review.report
    # nightly reviews are permanent / append-only
    mi.nightly_review(db)
    assert db.scalar(select(func.count()).select_from(mim.MiNightlyReview)) == 2


def test_batch_idempotent(in_memory_db):
    db = in_memory_db
    _seed(db)
    research.research_cycle(db, limit_markets=None)
    mi.run_intel_batch(db, refresh_lab=False, limit_markets=None)
    n_prof = db.scalar(select(func.count()).select_from(mim.MiMarketProfile))
    mi.run_intel_batch(db, refresh_lab=False, limit_markets=None)
    assert db.scalar(select(func.count()).select_from(mim.MiMarketProfile)) == n_prof   # upsert, no dupes


# --- SAFETY: isolation from live trading / production -----------------------
def test_market_intel_never_touches_live_or_production(in_memory_db):
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

    research.research_cycle(db, limit_markets=None)
    mi.run_intel_batch(db, refresh_lab=False, limit_markets=None)

    assert db.scalar(select(func.count()).select_from(LiveExecution)) == 0
    assert (db.get(LiveState, 1).bankroll, db.get(LiveState, 1).halted) == state_before
    assert db.get(WalletCandidate, w.id).classification == cand_before
    assert db.get(Wallet, w.id).copy_enabled == copy_before
