"""Research Platform V1 tests: strategy library, independent paper trading,
causal replay, mutation lineage, tournament/champion, ensembles, hypotheses,
nightly review, full cycle, and ISOLATION from live trading/production."""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app import btc5m, research
from app import research_models as rm
from app.models import LiveExecution, LiveState, Market, Trade, Wallet, WalletCandidate


def _seed_lab(db, *, n_markets=30, n_wallets=12, seed=0):
    """Seed BTC5m markets+trades and run the Lab refresh so the research layer has
    fingerprints/clusters/consensus/champion to build strategies on."""
    rng = random.Random(seed)
    wallets = []
    for i in range(n_wallets):
        w = Wallet(address=f"0x{i:040x}", last_active=datetime.utcnow())
        db.add(w); db.flush(); wallets.append(w)
    for mi in range(n_markets):
        created = datetime.utcnow() - timedelta(hours=mi)
        yes_wins = (mi % 2 == 0)
        db.add(Market(id=f"btc5m-{mi}", question=f"Bitcoin Up or Down 5m #{mi}", slug=f"s-{mi}",
                      outcomes=["Up", "Down"], token_ids=["t1", "t2"], prices=[0.5, 0.5], resolved=True,
                      resolved_outcome="Up" if yes_wins else "Down",
                      resolved_at=created + timedelta(minutes=5), created_at=created, volume=1000.0))
        for wi, w in enumerate(wallets):
            for k in range(rng.randint(1, 3)):
                t = created + timedelta(seconds=(rng.randint(5, 80) if wi < n_wallets // 2 else rng.randint(120, 250)))
                up = yes_wins if (wi < n_wallets // 2 and rng.random() < 0.8) else (rng.random() < 0.5)
                db.add(Trade(external_id=f"tx-{mi}-{wi}-{k}", wallet_id=w.id, market_id=f"btc5m-{mi}",
                             outcome="Up" if up else "Down", side="buy",
                             price=round(rng.uniform(0.35, 0.65), 3), size=round(rng.uniform(1, 8), 2), timestamp=t))
    db.commit()
    btc5m.refresh(db, limit_markets=None)


# --- Phase 1: strategy library ----------------------------------------------
def test_seed_strategies_builds_library_idempotently(in_memory_db):
    db = in_memory_db
    _seed_lab(db)
    out = research.seed_strategies(db)
    assert out["seeded"] > 0
    n = db.scalar(select(func.count()).select_from(rm.ResearchStrategy))
    assert n > 0
    # idempotent: a second seed does nothing
    out2 = research.seed_strategies(db)
    assert out2["seeded"] == 0
    assert db.scalar(select(func.count()).select_from(rm.ResearchStrategy)) == n
    # statuses are valid
    for s in db.scalars(select(rm.ResearchStrategy)).all():
        assert s.status in rm.STATUSES


# --- Phase 2 + 3: independent paper trading + causal replay ------------------
def test_replay_paper_trades_are_independent(in_memory_db):
    db = in_memory_db
    _seed_lab(db)
    research.seed_strategies(db)
    research.replay_all(db)
    strats = db.scalars(select(rm.ResearchStrategy)).all()
    assert any(s.trades > 0 for s in strats)
    # each strategy keeps its OWN paper trades + bankroll (no cross-contamination)
    for s in strats:
        owned = db.scalars(select(rm.StrategyPaperTrade).where(
            rm.StrategyPaperTrade.strategy_id == s.id)).all()
        assert all(t.strategy_id == s.id for t in owned)
        assert len(owned) == s.trades


def test_replay_is_deterministic_and_reproducible(in_memory_db):
    db = in_memory_db
    _seed_lab(db)
    research.seed_strategies(db)
    research.replay_all(db)
    scores1 = {s.id: s.robust_score for s in db.scalars(select(rm.ResearchStrategy)).all()}
    n1 = db.scalar(select(func.count()).select_from(rm.StrategyPaperTrade))
    research.replay_all(db)                              # rerun
    scores2 = {s.id: s.robust_score for s in db.scalars(select(rm.ResearchStrategy)).all()}
    assert scores1 == scores2                            # deterministic
    assert db.scalar(select(func.count()).select_from(rm.StrategyPaperTrade)) == n1   # no growth


def test_every_paper_trade_explains_itself(in_memory_db):
    db = in_memory_db
    _seed_lab(db)
    research.seed_strategies(db)
    research.replay_all(db)
    t = db.scalars(select(rm.StrategyPaperTrade)).first()
    assert t is not None
    assert "reasons" in t.explanation and t.explanation["reasons"]   # Phase 9 explainability


# --- Phase 4: mutation lineage ----------------------------------------------
def test_mutation_creates_children_without_overwriting(in_memory_db):
    db = in_memory_db
    _seed_lab(db)
    research.seed_strategies(db)
    research.replay_all(db)
    before = {s.id: dict(s.params) for s in db.scalars(select(rm.ResearchStrategy)).all()}
    out = research.mutate_top(db)
    assert out["mutations_created"] > 0
    # parents are unchanged (never overwritten); children reference parent_id
    after = db.scalars(select(rm.ResearchStrategy)).all()
    for s in after:
        if s.id in before:
            assert s.params == before[s.id]              # parent params untouched
    children = [s for s in after if s.parent_id]
    assert children and all(c.version > 1 for c in children)


# --- Phase 5 + 6: tournament + ensembles ------------------------------------
def test_tournament_promotes_champion_by_robust_score(in_memory_db):
    db = in_memory_db
    _seed_lab(db)
    research.seed_strategies(db)
    research.build_ensembles(db)
    research.replay_all(db)
    out = research.tournament(db)
    champs = [s for s in db.scalars(select(rm.ResearchStrategy)).all() if s.is_champion]
    assert len(champs) == 1                              # exactly one champion
    assert champs[0].status == "Champion"
    # champion has the top robust score among eligible strategies
    eligible = [s for s in db.scalars(select(rm.ResearchStrategy)).all()
                if s.trades >= research.MIN_TRADES_FOR_CHAMPION]
    assert champs[0].robust_score == max(s.robust_score for s in eligible)


def test_ensembles_are_built_and_paper_traded(in_memory_db):
    db = in_memory_db
    _seed_lab(db)
    research.seed_strategies(db)
    research.build_ensembles(db)
    research.replay_all(db)
    ens = db.scalars(select(rm.ResearchStrategy).where(rm.ResearchStrategy.is_ensemble.is_(True))).all()
    assert len(ens) > 0
    assert any(e.trades > 0 for e in ens)               # ensembles trade independently


# --- Phase 7 + 8: nightly review + hypotheses -------------------------------
def test_hypotheses_generated_with_status(in_memory_db):
    db = in_memory_db
    _seed_lab(db)
    research.research_cycle(db, limit_markets=None)      # seeds + replays + generates hypotheses
    hyps = db.scalars(select(rm.ResearchHypothesis)).all()
    assert len(hyps) > 0
    assert all(h.status in ("Pending", "Testing", "Confirmed", "Rejected", "Inconclusive") for h in hyps)


def test_nightly_review_is_permanent_with_18_sections(in_memory_db):
    db = in_memory_db
    _seed_lab(db)
    research.seed_strategies(db)
    research.replay_all(db)
    research.tournament(db)
    rev = research.nightly_review(db)
    sections = [k for k in rev["report"] if not k.startswith("_")]
    assert len(sections) == 18                           # all 18 sections present
    # a second review does not overwrite the first (append-only / permanent)
    research.nightly_review(db)
    assert db.scalar(select(func.count()).select_from(rm.NightlyReview)) == 2


# --- Phase 10: full cycle ---------------------------------------------------
def test_research_cycle_runs_end_to_end(in_memory_db):
    db = in_memory_db
    _seed_lab(db)
    out = research.research_cycle(db, limit_markets=None)
    assert out["champion"] is not None
    assert out["replay"]["strategies_replayed"] > 0
    assert out["nightly_review_id"] is not None
    d = research.dashboard(db)
    assert d["total_strategies"] > 0 and d["paper_trades"] > 0
    det = research.strategy_detail(db, d["top_strategies"][0]["id"])
    assert det and "paper_trades" in det                 # drill-down works


# --- SAFETY: total isolation from live trading / production -----------------
def test_research_never_touches_live_or_production(in_memory_db):
    db = in_memory_db
    db.add(LiveState(id=1, starting_bankroll=40.0, bankroll=40.0, halted=False))
    w = Wallet(address="0xprod", copy_enabled=True, last_active=datetime.utcnow())
    db.add(w); db.flush()
    db.add(WalletCandidate(wallet_id=w.id, copyability_score=70.0, classification="good_candidate"))
    db.commit()
    _seed_lab(db)
    state_before = (db.get(LiveState, 1).bankroll, db.get(LiveState, 1).halted)
    cand_before = db.get(WalletCandidate, w.id).classification
    copy_before = db.get(Wallet, w.id).copy_enabled

    research.research_cycle(db, limit_markets=None)

    assert db.scalar(select(func.count()).select_from(LiveExecution)) == 0       # no orders
    assert (db.get(LiveState, 1).bankroll, db.get(LiveState, 1).halted) == state_before
    assert db.get(WalletCandidate, w.id).classification == cand_before           # eligibility unchanged
    assert db.get(Wallet, w.id).copy_enabled == copy_before                      # copy flag unchanged
