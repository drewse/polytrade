"""BTC 5M Reversal Lab pipeline tests: dataset identification + indexing, feature
reconstruction, fingerprinting + Wallet IQ, model training/champion, consensus,
shadow strategy, idempotent refresh, and ISOLATION from live trading/production."""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app import btc5m
from app import btc5m_models as bm
from app.models import LiveExecution, LiveState, Market, Trade, Wallet, WalletCandidate


def _seed(db, *, n_markets=22, n_wallets=10, seed=0):
    """Seed synthetic BTC 5m markets + trades. Skilled wallets (first half) pick the
    winning side early ~80% of the time; the rest are random."""
    rng = random.Random(seed)
    wallets = []
    for i in range(n_wallets):
        w = Wallet(address=f"0x{i:040x}", last_active=datetime.utcnow())
        db.add(w); db.flush(); wallets.append(w)
    for mi in range(n_markets):
        created = datetime.utcnow() - timedelta(hours=mi)
        yes_wins = (mi % 2 == 0)
        m = Market(id=f"btc5m-{mi}", question=f"Bitcoin Up or Down 5m #{mi}",
                   slug=f"bitcoin-up-or-down-5m-{mi}", outcomes=["Up", "Down"],
                   token_ids=["t1", "t2"], prices=[0.5, 0.5], resolved=True,
                   resolved_outcome="Up" if yes_wins else "Down",
                   resolved_at=created + timedelta(minutes=5), created_at=created, volume=1000.0)
        db.add(m)
        for wi, w in enumerate(wallets):
            skilled = wi < n_wallets // 2
            for k in range(rng.randint(1, 3)):
                t = created + timedelta(seconds=(rng.randint(5, 80) if skilled else rng.randint(120, 250)))
                up = yes_wins if (skilled and rng.random() < 0.8) else (rng.random() < 0.5)
                db.add(Trade(external_id=f"tx-{mi}-{wi}-{k}", wallet_id=w.id, market_id=m.id,
                             outcome="Up" if up else "Down", side="buy",
                             price=round(rng.uniform(0.35, 0.65), 3),
                             size=round(rng.uniform(1, 8), 2), timestamp=t))
    db.commit()
    return wallets


# --- Phase 1: identification + indexing -------------------------------------
def test_identifies_btc5m_markets():
    assert btc5m.is_btc5m_market("Bitcoin Up or Down 5m", "bitcoin-up-or-down-5m")
    assert btc5m.is_btc5m_market("BTC 5 Minute price", None)
    assert btc5m.is_btc5m_market("BTC 5m", None)
    # NOT btc 5m:
    assert not btc5m.is_btc5m_market("Will Bitcoin hit $100k in 2026?", "bitcoin-100k")
    assert not btc5m.is_btc5m_market("Lakers vs Celtics", "nba")
    assert not btc5m.is_btc5m_market("Ethereum 5m", None)   # not bitcoin


def test_index_dataset_builds_records_with_derived_fields(in_memory_db):
    db = in_memory_db
    _seed(db, n_markets=6, n_wallets=6)
    out = btc5m.index_dataset(db, limit_markets=None)
    assert out["markets_indexed"] == 6 and out["trades_indexed"] > 0
    rows = db.scalars(select(bm.Btc5mTrade)).all()
    assert rows
    t0 = rows[0]
    assert t0.direction in ("YES", "NO")
    assert t0.seconds_from_creation is not None and t0.seconds_until_expiry is not None
    assert t0.label_direction in (0, 1)
    assert set(btc5m.FEATURE_NAMES) <= set(t0.features.keys())   # full feature vector stored
    assert t0.won is not None                                    # resolved -> settled


def test_index_dataset_is_idempotent(in_memory_db):
    db = in_memory_db
    _seed(db, n_markets=5, n_wallets=5)
    btc5m.index_dataset(db, limit_markets=None)
    n1 = db.scalar(select(func.count()).select_from(bm.Btc5mTrade))
    btc5m.index_dataset(db, limit_markets=None)                  # rerun
    n2 = db.scalar(select(func.count()).select_from(bm.Btc5mTrade))
    assert n1 == n2 and n1 > 0                                   # no duplicates


def test_features_have_no_label_leakage(in_memory_db):
    """The reconstructed feature vector must NOT contain the trade's own direction
    or price (those are the decision being predicted)."""
    db = in_memory_db
    _seed(db, n_markets=4, n_wallets=4)
    btc5m.index_dataset(db, limit_markets=None)
    t = db.scalars(select(bm.Btc5mTrade)).first()
    # market_yes_price is the LAST PRIOR state, not this trade's price/direction
    assert "direction" not in t.features and "side" not in t.features


# --- Phase 2 + 5: fingerprint + Wallet IQ + cluster -------------------------
def test_fingerprint_generates_iq_and_cluster(in_memory_db):
    db = in_memory_db
    _seed(db)
    btc5m.index_dataset(db, limit_markets=None)
    fp = btc5m.fingerprint_wallets(db)
    assert fp["profiles"] > 0
    prof = db.scalars(select(bm.Btc5mWalletProfile)).first()
    assert prof.cluster in btc5m.CLUSTERS
    assert 0.0 <= prof.cluster_confidence <= 1.0
    iq = prof.wallet_iq
    for k in ("strategy", "average_entry", "average_hold", "copy_confidence",
              "strength", "weakness", "average_confidence"):
        assert k in iq
    assert 0 <= iq["copy_confidence"] <= 99
    # skilled wallets (good ROI) should be flagged profitable
    assert any(p.profitable for p in db.scalars(select(bm.Btc5mWalletProfile)).all())


# --- Phase 4 + 8: strategy lab + champion -----------------------------------
def test_train_models_persists_leaderboard_and_champion(in_memory_db):
    db = in_memory_db
    _seed(db)
    btc5m.index_dataset(db, limit_markets=None)
    btc5m.fingerprint_wallets(db)
    out = btc5m.train_models(db)
    lb = btc5m.leaderboard(db, scope="global")
    assert len(lb) == 5                                         # all families on the leaderboard
    champs = [r for r in lb if r["is_champion"]]
    assert len(champs) == 1                                     # exactly one champion
    assert champs[0]["name"] != "baseline_majority"            # champion is a LEARNED model, not the floor
    assert out["global"]["champion"] == champs[0]["name"]


# --- Phase 6: consensus -----------------------------------------------------
def test_consensus_builds_groups_and_leaders(in_memory_db):
    db = in_memory_db
    _seed(db)
    btc5m.refresh(db, limit_markets=None)
    cons = btc5m.consensus(db)
    assert "edges" in cons and "consensus_groups" in cons and "leaders" in cons
    # skilled wallets entering the same winning side early -> some agreement edges
    assert len(cons["edges"]) > 0


# --- Phase 7: shadow strategy ----------------------------------------------
def test_shadow_signals_generated_and_scored(in_memory_db):
    db = in_memory_db
    _seed(db)
    btc5m.refresh(db, limit_markets=None)                       # generates + scores shadow signals
    sigs = db.scalars(select(bm.Btc5mShadowSignal)).all()
    assert len(sigs) > 0
    perf = btc5m.shadow_performance(db)
    assert "hit_rate" in perf and "paper_pnl" in perf
    # every signal is paper — none reference a real order/execution
    assert db.scalar(select(func.count()).select_from(LiveExecution)) == 0


# --- Phase 8: idempotent refresh -------------------------------------------
def test_refresh_is_idempotent(in_memory_db):
    db = in_memory_db
    _seed(db, n_markets=8, n_wallets=6)
    a = btc5m.refresh(db, limit_markets=None)
    n_trades = db.scalar(select(func.count()).select_from(bm.Btc5mTrade))
    b = btc5m.refresh(db, limit_markets=None)
    assert db.scalar(select(func.count()).select_from(bm.Btc5mTrade)) == n_trades   # no dupes
    assert a["dataset"]["markets_indexed"] == b["dataset"]["markets_indexed"]


# --- SAFETY: total isolation from live trading / production -----------------
def test_refresh_never_touches_live_trading_or_production(in_memory_db):
    db = in_memory_db
    # establish a live + production baseline
    db.add(LiveState(id=1, starting_bankroll=40.0, bankroll=40.0, halted=False))
    w = Wallet(address="0xprod", copy_enabled=True, last_active=datetime.utcnow())
    db.add(w); db.flush()
    db.add(WalletCandidate(wallet_id=w.id, copyability_score=70.0, classification="good_candidate"))
    db.commit()
    _seed(db)
    live_before = db.scalar(select(func.count()).select_from(LiveExecution))
    state_before = (db.get(LiveState, 1).bankroll, db.get(LiveState, 1).halted)
    cand_before = db.get(WalletCandidate, w.id).classification
    copy_before = db.get(Wallet, w.id).copy_enabled

    btc5m.refresh(db, limit_markets=None)

    assert db.scalar(select(func.count()).select_from(LiveExecution)) == live_before == 0
    assert (db.get(LiveState, 1).bankroll, db.get(LiveState, 1).halted) == state_before
    assert db.get(WalletCandidate, w.id).classification == cand_before   # eligibility unchanged
    assert db.get(Wallet, w.id).copy_enabled == copy_before              # copy flag unchanged


def test_fingerprint_handles_unresolved_markets(in_memory_db):
    """A wallet with more buys than settled (open/unresolved BTC5m markets) must
    fingerprint without error (regression: sizing detection index alignment)."""
    db = in_memory_db
    w = Wallet(address="0xmix", last_active=datetime.utcnow())
    db.add(w); db.flush()
    for mi in range(12):
        created = datetime.utcnow() - timedelta(hours=mi)
        resolved = (mi % 2 == 0)                       # half open, half resolved
        db.add(Market(id=f"btc5m-{mi}", question=f"Bitcoin Up or Down 5m #{mi}", slug=f"s-{mi}",
                      outcomes=["Up", "Down"], token_ids=["t1", "t2"], prices=[0.5, 0.5],
                      resolved=resolved, resolved_outcome="Up" if resolved else None,
                      resolved_at=(created + timedelta(minutes=5)) if resolved else None,
                      created_at=created, volume=500.0))
        db.add(Trade(external_id=f"tx-{mi}", wallet_id=w.id, market_id=f"btc5m-{mi}", outcome="Up",
                     side="buy", price=0.5, size=3.0 + mi, timestamp=created + timedelta(seconds=60)))
    db.commit()
    btc5m.index_dataset(db, limit_markets=None)
    fp = btc5m.fingerprint_wallets(db)                 # must not raise
    assert fp["profiles"] == 1
    prof = db.scalars(select(bm.Btc5mWalletProfile)).first()
    assert prof.metrics["buy_count"] == 12 and prof.settled_count == 6


def test_dashboard_shape(in_memory_db):
    db = in_memory_db
    _seed(db, n_markets=8, n_wallets=6)
    btc5m.refresh(db, limit_markets=None)
    d = btc5m.dashboard(db)
    for k in ("wallet_count", "trade_count", "markets_indexed", "models_trained",
              "best_model", "top_features", "largest_cluster", "consensus_opportunities",
              "leader_wallets", "latest_signals", "shadow_performance"):
        assert k in d
    assert "read-only" in d["safety"]
