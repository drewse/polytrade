"""Shadow Portfolio — verifies it is 100% READ-ONLY simulation: never writes real
executions/positions, leaves production eligibility unchanged, and is deterministic."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from app import live_ranking, shadow
from app.models import (
    LiveExecution, LiveSignalDecision, LiveState, Market, PaperSignal, Wallet,
    WalletCandidate, WalletStat,
)


def _wallet(db, addr, *, roi, pf, settled, win=0.6, last_active_days=2):
    w = Wallet(address=addr, copy_enabled=True,
               last_active=datetime.utcnow() - timedelta(days=last_active_days))
    db.add(w); db.flush()
    db.add(WalletStat(wallet_id=w.id, num_trades=settled, num_settled=settled,
                      realized_roi=roi, win_rate=win, profit_factor=pf, expectancy=10.0,
                      sharpe=0.5, recency_score=0.9, partial_history=False, consistency=0.6,
                      avg_trade_size=50.0, max_drawdown=0.2))
    db.add(WalletCandidate(wallet_id=w.id, copyability_score=70.0, classification="good_candidate"))
    return w


def _market(db, mid, *, resolved, won_outcome=None, price_over=0.5):
    m = Market(id=mid, question=f"Q {mid}", outcomes=["Over", "Under"],
               prices=[price_over, round(1 - price_over, 4)], token_ids=["t1", "t2"],
               resolved=resolved, resolved_outcome=won_outcome if resolved else None,
               resolved_at=datetime.utcnow() if resolved else None, liquidity=5000, volume=20000)
    db.add(m); db.flush()
    return m


def _signal_and_decision(db, wallet, market, *, price, outcome, sig_id, edge=0.12, conf=78):
    db.add(PaperSignal(id=sig_id, wallet_id=wallet.id, market_id=market.id, outcome=outcome,
                       side="buy", observed_price=price, suggested_entry=price, confidence=conf,
                       edge_estimate=edge, reason="t", created_at=datetime.utcnow() - timedelta(days=1)))
    db.add(LiveSignalDecision(signal_id=sig_id, status="skipped", category="wallet_not_eligible",
                              reason="not in production top-N", wallet_address=wallet.address,
                              edge=edge, confidence=conf, production_score=0.0,
                              created_at=datetime.utcnow() - timedelta(days=1)))


def _seed(db):
    _wallet(db, "0xprod", roi=0.28, pf=2.2, settled=100)          # production eligible
    cand = _wallet(db, "0xcand", roi=0.05, pf=1.10, settled=15)   # candidate
    db.commit()
    mwin = _market(db, "mwin", resolved=True, won_outcome="Over")    # candidate won
    mlose = _market(db, "mlose", resolved=True, won_outcome="Under") # candidate lost
    mopen = _market(db, "mopen", resolved=False, price_over=0.60)    # still open
    db.commit()
    _signal_and_decision(db, cand, mwin, price=0.50, outcome="Over", sig_id=1)
    _signal_and_decision(db, cand, mlose, price=0.50, outcome="Over", sig_id=2)
    _signal_and_decision(db, cand, mopen, price=0.50, outcome="Over", sig_id=3)
    db.commit()
    return cand


def test_endpoint_is_read_only_no_real_writes(in_memory_db):
    db = in_memory_db
    _seed(db)

    def snapshot():
        return (
            db.scalar(select(func.count()).select_from(LiveExecution)),    # real executions
            db.scalar(select(func.count()).select_from(LiveSignalDecision)),
            db.scalar(select(func.count()).select_from(PaperSignal)),
            db.scalar(select(func.count()).select_from(Market)),
            db.scalar(select(func.count()).select_from(LiveState)),
            frozenset(live_ranking.eligible_addresses(db)),
        )

    before = snapshot()
    shadow.shadow_portfolio(db)
    shadow.shadow_portfolio(db, limit=5)
    after = snapshot()
    assert before == after                      # nothing written; eligibility identical
    # crucially: zero real executions exist (no live orders triggered)
    assert db.scalar(select(func.count()).select_from(LiveExecution)) == 0


def test_production_wallets_never_simulated(in_memory_db):
    db = in_memory_db
    _seed(db)
    res = shadow.shadow_portfolio(db)
    wallets = {w["wallet"] for w in res["wallets"]}
    assert "0xcand" in wallets
    assert "0xprod" not in wallets               # production never appears as a candidate
    assert "0xprod" in live_ranking.eligible_addresses(db)


def test_simulated_pl_is_correct_and_deterministic(in_memory_db):
    db = in_memory_db
    _seed(db)
    a = shadow.shadow_portfolio(db)
    b = shadow.shadow_portfolio(db)
    wa = next(w for w in a["wallets"] if w["wallet"] == "0xcand")
    wb = next(w for w in b["wallets"] if w["wallet"] == "0xcand")
    assert wa == wb                              # deterministic

    # 3 shadow trades: win@0.50 -> +1.0, lose@0.50 -> -1.0, open@0.60 (entry 0.50) -> +0.2 unreal
    assert wa["shadow_trades"] == 3
    assert wa["simulated_wins"] == 1 and wa["simulated_losses"] == 1 and wa["open_positions"] == 1
    assert wa["realized_pl"] == 0.0              # +1.0 + -1.0
    assert wa["unrealized_pl"] == 0.2            # (0.60/0.50 - 1) * 1.0
    assert wa["total_pl"] == 0.2
    assert wa["win_rate"] == 0.5
    assert wa["simulated"] is True
    assert wa["promotion_score"] is not None     # merged with promotion candidate


def test_aggregates_and_marking(in_memory_db):
    db = in_memory_db
    _seed(db)
    res = shadow.shadow_portfolio(db)
    assert res["simulated"] is True and "no real orders" in res["note"].lower()
    agg = res["aggregates"]
    for key in ("all_candidates", "strong", "near", "watch", "production_baseline"):
        assert key in agg and agg[key]["simulated"] is True
    assert agg["all_candidates"]["shadow_trades"] == 3
