"""Production wallet-ranking (live-only) tests: score weighting, hard filters,
ranking order, and that the executor copies only eligible wallets."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app import live, live_ranking
from app.models import LiveExecution, Market, PaperSignal, Trade, Wallet, WalletCandidate, WalletStat


def _wallet(db, addr, *, roi, pf, settled, win=0.6, recency=0.9, partial=False,
            last_active_days=2, copyability=70.0):
    w = Wallet(address=addr, copy_enabled=True,
               last_active=datetime.utcnow() - timedelta(days=last_active_days))
    db.add(w); db.flush()
    db.add(WalletStat(wallet_id=w.id, num_trades=settled, num_settled=settled,
                      realized_roi=roi, win_rate=win, profit_factor=pf, expectancy=10.0,
                      sharpe=0.5, recency_score=recency, partial_history=partial,
                      consistency=0.6))
    db.add(WalletCandidate(wallet_id=w.id, copyability_score=copyability,
                           classification="good_candidate"))
    return w


# --- score weighting (pure) -------------------------------------------------
def test_production_score_weighting():
    s = live_ranking.production_score(reputation_score=100, profit_factor=3.0, roi=0.5, recency=1.0)
    assert s == 100.0                       # all components maxed
    z = live_ranking.production_score(reputation_score=0, profit_factor=1.0, roi=0.0, recency=0.0)
    assert z == 0.0
    # reputation dominates (40%): rep only -> 40
    assert live_ranking.production_score(reputation_score=100, profit_factor=1.0, roi=0.0, recency=0.0) == 40.0


# --- hard filters -----------------------------------------------------------
def test_filters(in_memory_db):
    db = in_memory_db
    now = datetime.utcnow()
    cfg = live_ranking._cfg()
    good = _wallet(db, "0xgood", roi=0.2, pf=1.8, settled=50); db.commit()
    gs = db.scalar(select(WalletStat).where(WalletStat.wallet_id == good.id))
    assert live_ranking.passes_filters(gs, good, now, cfg)[0]
    # negative ROI fails
    neg = _wallet(db, "0xneg", roi=-0.09, pf=1.8, settled=50); db.commit()
    ns = db.scalar(select(WalletStat).where(WalletStat.wallet_id == neg.id))
    ok, why = live_ranking.passes_filters(ns, neg, now, cfg)
    assert not ok and "ROI" in why
    # PF <= 1.20 fails
    lowpf = _wallet(db, "0xpf", roi=0.2, pf=1.1, settled=50); db.commit()
    ps = db.scalar(select(WalletStat).where(WalletStat.wallet_id == lowpf.id))
    assert not live_ranking.passes_filters(ps, lowpf, now, cfg)[0]
    # too few settled fails
    few = _wallet(db, "0xfew", roi=0.2, pf=1.8, settled=5); db.commit()
    fs = db.scalar(select(WalletStat).where(WalletStat.wallet_id == few.id))
    assert not live_ranking.passes_filters(fs, few, now, cfg)[0]
    # inactive fails
    old = _wallet(db, "0xold", roi=0.2, pf=1.8, settled=50, last_active_days=120); db.commit()
    os_ = db.scalar(select(WalletStat).where(WalletStat.wallet_id == old.id))
    assert not live_ranking.passes_filters(os_, old, now, cfg)[0]


# --- ranking: losers excluded, profitable ranked, both metrics present ------
def test_ranking_excludes_losers_keeps_both_metrics(in_memory_db):
    db = in_memory_db
    # the audit example: high copyability but negative ROI -> must be ineligible
    _wallet(db, "0xloser", roi=-0.09, pf=0.47, settled=100, win=0.91, copyability=76.5)
    _wallet(db, "0xwinner", roi=0.28, pf=2.2, settled=100, win=0.57, copyability=70.0)
    db.commit()
    ranked = live_ranking.rank_wallets(db, include_failed=True)
    by = {r["address"]: r for r in ranked}
    assert by["0xloser"]["eligible"] is False           # losing wallet rejected
    assert by["0xwinner"]["eligible"] is True
    assert by["0xwinner"]["production_rank_score"] > 0
    # both metrics exposed
    assert by["0xloser"]["copyability"] == 76.5 and "production_rank_score" in by["0xloser"]
    # eligible set excludes the loser
    elig = live_ranking.eligible_addresses(db)
    assert "0xwinner" in elig and "0xloser" not in elig


# --- executor copies only production-eligible wallets -----------------------
def test_executor_gates_on_production_eligibility(in_memory_db, monkeypatch):
    db = in_memory_db
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("LIVE_EXECUTOR", "dry_run")
    monkeypatch.setenv("LIVE_STARTING_BANKROLL", "40")
    monkeypatch.setenv("LIVE_MAX_ORDERS", "0")
    monkeypatch.setenv("LIVE_MIN_EDGE", "0.0")
    monkeypatch.setenv("LIVE_MIN_CONFIDENCE", "0")
    win = _wallet(db, "0xwin", roi=0.28, pf=2.2, settled=100)
    los = _wallet(db, "0xlos", roi=-0.05, pf=0.8, settled=100)
    for wal, mid in ((win, "mw"), (los, "ml")):
        db.add(Market(id=mid, question="Q", outcomes=["Yes", "No"], token_ids=["t1", "t2"],
                      prices=[0.5, 0.5], resolved=False, liquidity=5000, volume=20000))
        db.flush()
        db.add(PaperSignal(wallet_id=wal.id, market_id=mid, outcome="Yes", side="buy",
                           observed_price=0.5, suggested_entry=0.5, confidence=80,
                           edge_estimate=0.1, reason="t", created_at=datetime.utcnow()))
    db.commit()
    live.process_new_signals(db)
    placed = db.scalars(select(LiveExecution).where(LiveExecution.status == "open")).all()
    addrs = {e.wallet_address for e in placed}
    assert "0xwin" in addrs and "0xlos" not in addrs     # only the profitable wallet copied
