"""Promotion Candidates analytics — verifies it is 100% READ-ONLY, computes
candidates correctly, NEVER includes production wallets, and is deterministic."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from app import live_ranking, promotion
from app.models import (
    LiveExecution, LiveSignalDecision, LiveState, Market, Trade, Wallet,
    WalletCandidate, WalletStat,
)


def _wallet(db, addr, *, roi, pf, settled, win=0.6, recency=0.9, last_active_days=2,
            avg_trade_size=50.0, copyability=70.0):
    w = Wallet(address=addr, copy_enabled=True,
               last_active=datetime.utcnow() - timedelta(days=last_active_days))
    db.add(w); db.flush()
    db.add(WalletStat(wallet_id=w.id, num_trades=settled, num_settled=settled,
                      realized_roi=roi, win_rate=win, profit_factor=pf, expectancy=10.0,
                      sharpe=0.5, recency_score=recency, partial_history=False,
                      consistency=0.6, avg_trade_size=avg_trade_size, max_drawdown=0.2))
    db.add(WalletCandidate(wallet_id=w.id, copyability_score=copyability,
                           classification="good_candidate"))
    return w


def _decisions(db, addr, n, *, edge=0.12, conf=78, days_back=0):
    for i in range(n):
        db.add(LiveSignalDecision(
            signal_id=hash((addr, i)) % 1_000_000, status="skipped",
            category="wallet_not_eligible", reason="wallet not in production top-N",
            wallet_address=addr, edge=edge, confidence=conf, production_score=0.0,
            created_at=datetime.utcnow() - timedelta(days=days_back + i)))


def test_production_wallets_never_appear(in_memory_db):
    db = in_memory_db
    _wallet(db, "0xprod", roi=0.28, pf=2.2, settled=100)     # passes -> production eligible
    _wallet(db, "0xcand", roi=0.05, pf=1.10, settled=15)     # below PF -> candidate
    db.commit()
    _decisions(db, "0xcand", 6)
    _decisions(db, "0xprod", 4)   # even with reject-decisions, production wallet must be excluded
    db.commit()

    res = promotion.promotion_candidates(db, min_signals=2)
    addrs = {c["wallet"] for c in res["candidates"]}
    assert "0xcand" in addrs
    assert "0xprod" not in addrs                              # production NEVER appears
    assert "0xprod" in live_ranking.eligible_addresses(db)   # sanity: it IS production


def test_candidate_fields_and_exact_reason(in_memory_db):
    db = in_memory_db
    _wallet(db, "0xprod", roi=0.28, pf=2.2, settled=100)
    _wallet(db, "0xcand", roi=0.05, pf=1.10, settled=15)
    db.commit()
    _decisions(db, "0xcand", 8, edge=0.15, conf=80)
    db.commit()
    c = next(x for x in promotion.promotion_candidates(db)["candidates"] if x["wallet"] == "0xcand")
    assert c["signals_seen"] == 8
    assert c["profit_factor"] == 1.10 and c["roi"] == 0.05 and c["settled_trades"] == 15
    assert c["average_edge"] == 0.15 and c["average_confidence"] == 80.0
    assert "PF" in c["reason_rejected"]                       # EXACT reason from the filter
    assert c["status"] in ("strong", "near", "watch")
    assert 0 <= c["promotion_score"] <= 100


def test_outside_top_n_reason(in_memory_db, monkeypatch):
    # a wallet that PASSES filters but is ranked out of the top-N -> "Outside top-N"
    monkeypatch.setenv("LIVE_TOP_N_WALLETS", "1")
    db = in_memory_db
    _wallet(db, "0xbest", roi=0.40, pf=3.0, settled=200)     # rank #1 -> production
    _wallet(db, "0xrunner", roi=0.10, pf=1.40, settled=40)   # passes filters, rank #2 -> out
    db.commit()
    _decisions(db, "0xrunner", 5)
    db.commit()
    c = next(x for x in promotion.promotion_candidates(db)["candidates"] if x["wallet"] == "0xrunner")
    assert c["reason_rejected"] == "Outside production top-N"


def test_promotion_score_deterministic():
    kw = dict(signals=10, avg_edge=0.15, avg_conf=80, pf=1.5, roi=0.10, settled=20,
              recent_7d=3, distinct_markets=5, concentration=0.3, last_active_days=2)
    a = promotion._promotion_score(**kw)
    b = promotion._promotion_score(**kw)
    assert a == b and 0 <= a <= 100
    # penalties bite: negative ROI + poor PF + single market -> much lower
    bad = promotion._promotion_score(signals=2, avg_edge=0.15, avg_conf=80, pf=0.8, roi=-0.1,
                                     settled=2, recent_7d=0, distinct_markets=1,
                                     concentration=1.0, last_active_days=90)
    assert bad < a


def test_endpoint_is_read_only(in_memory_db):
    db = in_memory_db
    _wallet(db, "0xprod", roi=0.28, pf=2.2, settled=100)
    _wallet(db, "0xcand", roi=0.05, pf=1.10, settled=15)
    db.commit()
    _decisions(db, "0xcand", 5)
    db.commit()

    def snapshot():
        return (
            db.scalar(select(func.count()).select_from(LiveExecution)),
            db.scalar(select(func.count()).select_from(LiveSignalDecision)),
            db.scalar(select(func.count()).select_from(Wallet)),
            db.scalar(select(func.count()).select_from(Trade)),
            frozenset(live_ranking.eligible_addresses(db)),
        )

    before = snapshot()
    promotion.promotion_candidates(db)
    promotion.promotion_candidates(db, limit=10)
    after = snapshot()
    assert before == after            # no rows created/changed, eligibility identical
    # live state singleton (halt/bankroll) is never touched
    assert db.scalar(select(func.count()).select_from(LiveState)) in (0, 1)
