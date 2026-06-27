"""Discovery Backfill Queue Worker — priority order, idempotency, fail-closed,
completion clears the queue, eligibility unchanged for non-qualifying stats, and
no live executions/orders/positions are created."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from app import discovery_backfill, live_ranking
from app.models import DiscoverySource, LiveExecution, Wallet, WalletCandidate, WalletStat


def _drow(db, addr, *, pri, score, source="profit_leaderboard", detail="profit_30d", first_days=0):
    db.add(DiscoverySource(wallet_address=addr, discovery_source=source, source_detail=detail,
                           source_rank=1, discovery_score=score, backfill_priority=pri,
                           needs_backfill=True, backfill_status="pending",
                           first_seen=datetime.utcnow() - timedelta(days=first_days),
                           last_seen=datetime.utcnow()))


def _mock_backfill(*, pf=1.10, roi=0.05, settled=12, trades=12):
    """Stand-in for services.backfill_wallet — creates Wallet+WalletStat (below
    production threshold by default, so it does NOT become eligible)."""
    def fn(db, address, **_):
        w = db.scalar(select(Wallet).where(func.lower(Wallet.address) == address.lower()))
        if not w:
            w = Wallet(address=address, copy_enabled=False, last_active=datetime.utcnow())
            db.add(w); db.flush()
        if not db.get(WalletStat, w.id):
            db.add(WalletStat(wallet_id=w.id, num_trades=trades, num_settled=settled, realized_roi=roi,
                              win_rate=0.5, profit_factor=pf, expectancy=10.0, sharpe=0.5, recency_score=0.9,
                              partial_history=True, consistency=0.6, avg_trade_size=50.0, max_drawdown=0.2))
        db.commit()
        return {"ok": True, "trades_inserted": trades}
    return fn


def test_processes_highest_priority_first(in_memory_db):
    db = in_memory_db
    _drow(db, "0xlow", pri=30, score=30)
    _drow(db, "0xhigh", pri=100, score=100)
    _drow(db, "0xmid", pri=70, score=70)
    db.commit()
    order = []

    def rec(db, address, **_):
        order.append(address); return {"ok": True, "trades_inserted": 1}

    discovery_backfill.run_backfill_batch(db, batch=5, backfill_fn=rec, rate_limit_s=0)
    assert order == ["0xhigh", "0xmid", "0xlow"]   # priority desc


def test_idempotent_rerun(in_memory_db):
    db = in_memory_db
    _drow(db, "0xa", pri=100, score=90); db.commit()
    calls = []

    def rec(db, address, **_):
        calls.append(address)
        return _mock_backfill()(db, address)

    discovery_backfill.run_backfill_batch(db, batch=5, backfill_fn=rec, rate_limit_s=0)
    discovery_backfill.run_backfill_batch(db, batch=5, backfill_fn=rec, rate_limit_s=0)
    assert calls == ["0xa"]                         # processed exactly once across reruns


def test_failure_records_error_without_crashing(in_memory_db):
    db = in_memory_db
    _drow(db, "0xok", pri=100, score=90)
    _drow(db, "0xbad", pri=90, score=80)
    db.commit()

    def fn(db, address, **_):
        if address == "0xbad":
            raise RuntimeError("data-api 500")
        return _mock_backfill()(db, address)

    out = discovery_backfill.run_backfill_batch(db, batch=5, backfill_fn=fn, rate_limit_s=0)
    assert out["completed"] == 1 and out["failed"] == 1          # batch did not crash
    bad = db.scalar(select(DiscoverySource).where(DiscoverySource.wallet_address == "0xbad"))
    assert bad.backfill_status == "failed" and "data-api 500" in (bad.backfill_error or "")
    assert bad.needs_backfill is True                            # stays queued for retry


def test_completed_clears_needs_backfill(in_memory_db):
    db = in_memory_db
    _drow(db, "0xa", pri=100, score=90); db.commit()
    discovery_backfill.run_backfill_batch(db, batch=5, backfill_fn=_mock_backfill(), rate_limit_s=0)
    row = db.scalar(select(DiscoverySource).where(DiscoverySource.wallet_address == "0xa"))
    assert row.backfill_status == "completed" and row.needs_backfill is False
    assert row.stats_updated is True and row.trades_imported == 12
    assert discovery_backfill.backfill_status(db)["completed"] == 1


def test_eligibility_unchanged_for_non_qualifying_stats(in_memory_db):
    db = in_memory_db
    # an existing production-eligible wallet
    w = Wallet(address="0xprod", copy_enabled=True, last_active=datetime.utcnow()); db.add(w); db.flush()
    db.add(WalletStat(wallet_id=w.id, num_trades=100, num_settled=100, realized_roi=0.28, win_rate=0.6,
                      profit_factor=2.2, expectancy=10, sharpe=0.5, recency_score=0.9, partial_history=False,
                      consistency=0.6, avg_trade_size=50, max_drawdown=0.2))
    db.add(WalletCandidate(wallet_id=w.id, copyability_score=70.0, classification="good_candidate"))
    _drow(db, "0xweak", pri=100, score=90); db.commit()
    before = frozenset(live_ranking.eligible_addresses(db))

    # backfill a below-threshold wallet (PF 1.10, settled 12) -> must NOT become eligible
    discovery_backfill.run_backfill_batch(db, batch=5,
                                          backfill_fn=_mock_backfill(pf=1.10, settled=12), rate_limit_s=0)
    after = frozenset(live_ranking.eligible_addresses(db))
    assert before == after
    assert "0xweak" not in after


def test_no_live_executions_created(in_memory_db):
    db = in_memory_db
    _drow(db, "0xa", pri=100, score=90); db.commit()
    before = db.scalar(select(func.count()).select_from(LiveExecution))
    discovery_backfill.run_backfill_batch(db, batch=5, backfill_fn=_mock_backfill(), rate_limit_s=0)
    assert db.scalar(select(func.count()).select_from(LiveExecution)) == before == 0
