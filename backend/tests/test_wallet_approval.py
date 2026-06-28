"""Deep backfill + manual wallet approval tests: manual disable/reject/watchlist
hard-exclude; approval still requires gates; approval queue never auto-promotes;
deep backfill updates coverage + is idempotent/resumable; coverage grades; the
production eligible set changes only via explicit manual status + gates; and no
live executions are ever created."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from app import deep_backfill as dbf, live_ranking, public_profile, wallet_approval as wap
from app import wallet_approval_models as am
from app.models import (LiveExecution, Market, Trade, Wallet, WalletCandidate, WalletStat)
from app.polymarket_client import TradeDTO


def _mk(db, addr, *, roi=0.25, pf=1.8, settled=30, partial=True):
    w = Wallet(address=addr, copy_enabled=True, last_active=datetime.utcnow() - timedelta(days=2))
    db.add(w); db.flush()
    db.add(WalletStat(wallet_id=w.id, num_trades=settled * 2, num_settled=settled, realized_roi=roi,
                      win_rate=0.6, profit_factor=pf, recency_score=0.9, partial_history=partial,
                      max_drawdown=0.2, avg_trade_size=50))
    db.add(WalletCandidate(wallet_id=w.id, copyability_score=70, classification="good_candidate"))
    for i in range(settled):
        mid = f"{addr}-m{i}"
        db.add(Market(id=mid, question="Q", outcomes=["Yes", "No"], token_ids=["t1", "t2"], prices=[1.0, 0.0],
                      resolved=True, resolved_outcome="Yes", resolved_at=datetime.utcnow() - timedelta(days=i)))
        db.add(Trade(external_id=f"{addr}-t{i}", wallet_id=w.id, market_id=mid, outcome="Yes", side="buy",
                     price=0.5, size=10, timestamp=datetime.utcnow() - timedelta(days=i)))
    db.commit()
    return w


# --- manual controls --------------------------------------------------------
def test_disabled_wallet_never_eligible(in_memory_db):
    db = in_memory_db
    _mk(db, "0xa"); _mk(db, "0xb")
    assert frozenset(live_ranking.eligible_addresses(db)) == {"0xa", "0xb"}
    wap.set_status(db, "0xa", "disable", by="op", note="looks like a market maker")
    assert "0xa" not in live_ranking.eligible_addresses(db)             # hard override
    # disable does not delete stats/history
    assert db.scalar(select(func.count()).select_from(WalletStat)) == 2
    wap.set_status(db, "0xa", "enable")
    assert "0xa" in live_ranking.eligible_addresses(db)                 # re-enabled


def test_rejected_and_watchlist_excluded(in_memory_db):
    db = in_memory_db
    _mk(db, "0xa"); _mk(db, "0xb")
    wap.set_status(db, "0xa", "reject")
    wap.set_status(db, "0xb", "watchlist")
    assert frozenset(live_ranking.eligible_addresses(db)) == set()      # both excluded
    wap.set_status(db, "0xa", "reset")
    assert frozenset(live_ranking.eligible_addresses(db)) == {"0xa"}    # reset restores


def test_approval_still_requires_gates(in_memory_db, monkeypatch):
    db = in_memory_db
    _mk(db, "0xgood", roi=0.25, pf=1.8, settled=30)
    _mk(db, "0xbad", roi=-0.1, pf=0.8, settled=30)                      # fails ROI/PF gates
    monkeypatch.setenv("LIVE_REQUIRE_MANUAL_APPROVAL", "true")
    wap.set_status(db, "0xgood", "approve", by="op")
    wap.set_status(db, "0xbad", "approve", by="op")
    elig = frozenset(live_ranking.eligible_addresses(db))
    assert "0xgood" in elig                                             # approved + passes gates
    assert "0xbad" not in elig                                         # approved but FAILS gates


def test_require_approval_blocks_unapproved(in_memory_db, monkeypatch):
    db = in_memory_db
    _mk(db, "0xa")
    monkeypatch.setenv("LIVE_REQUIRE_MANUAL_APPROVAL", "true")
    assert frozenset(live_ranking.eligible_addresses(db)) == set()     # not approved -> not eligible
    wap.set_status(db, "0xa", "approve", by="op")
    assert frozenset(live_ranking.eligible_addresses(db)) == {"0xa"}


def test_default_does_not_change_eligible_set(in_memory_db):
    db = in_memory_db
    _mk(db, "0xa"); _mk(db, "0xb")
    # no manual status, default config -> eligibility unchanged
    assert frozenset(live_ranking.eligible_addresses(db)) == {"0xa", "0xb"}


def test_approval_queue_never_auto_promotes(in_memory_db):
    db = in_memory_db
    _mk(db, "0xa")
    public_profile.refresh_profiles(db, ["0xa"], force=True,
                                    fetch_fn=lambda x: {"address": x, "fetch_status": "ok", "fetched_at": datetime.utcnow(),
                                                        "pnl_all": 5000.0, "volume_all": 300.0, "predictions": 30})
    q = wap.approval_queue(db)
    assert q["count"] >= 1
    cand = q["candidates"][0]
    assert "manual approval required" in cand["why_not_auto_approved"]
    # being in the queue does NOT make it approved
    assert wap.get_status(db, "0xa") is None or not wap.get_status(db, "0xa").manually_approved


# --- deep backfill ----------------------------------------------------------
def _fake_history(total):
    def fetch(addr, offset, limit):
        out = []
        for i in range(offset, min(offset + limit, total)):
            out.append(TradeDTO(external_id=f"{addr}-deep{i}", wallet_address=addr,
                                market_id=f"{addr}-dm{i}", outcome="Yes", side="buy", price=0.4,
                                size=5, timestamp=datetime.utcnow() - timedelta(days=100 + i), shares=12.5))
        return out
    return fetch


# huge public totals so coverage target is never the stop condition -> the
# worker pages until the history is exhausted (empty page).
_BIG_PUBLIC = lambda x: {"address": x, "fetch_status": "ok", "fetched_at": datetime.utcnow(),
                         "pnl_all": 5000.0, "volume_all": 1_000_000.0, "predictions": 1_000_000}


def test_deep_backfill_updates_coverage_and_grades(in_memory_db):
    db = in_memory_db
    _mk(db, "0xa")
    public_profile.refresh_profiles(db, ["0xa"], force=True, fetch_fn=_BIG_PUBLIC)
    before = db.scalar(select(func.count()).select_from(Trade))
    out = dbf.run_deep_backfill(db, batch=5, max_pages=5, page_size=4, fetch_fn=_fake_history(12))
    assert out["trades_inserted"] == 12                                # paged through all 12
    assert db.scalar(select(func.count()).select_from(Trade)) == before + 12
    prog = db.get(am.WalletBackfillProgress, "0xa")
    assert prog.exhausted is True and prog.status == "completed"        # ran to end of history
    assert prog.coverage_grade == "complete"                           # exhausted overrides grade
    assert prog.coverage_ratio is not None


def test_deep_backfill_idempotent_resumable(in_memory_db):
    db = in_memory_db
    _mk(db, "0xa")
    public_profile.refresh_profiles(db, ["0xa"], force=True, fetch_fn=_BIG_PUBLIC)
    dbf.run_deep_backfill(db, batch=5, max_pages=5, page_size=4, fetch_fn=_fake_history(12))
    n1 = db.scalar(select(func.count()).select_from(Trade))
    # rerun: exhausted wallet is skipped from the priority queue, no dup trades
    dbf.run_deep_backfill(db, batch=5, max_pages=5, page_size=4, fetch_fn=_fake_history(12))
    assert db.scalar(select(func.count()).select_from(Trade)) == n1     # idempotent — no duplicates


def test_coverage_grade_calculation():
    assert dbf._grade(0.95, False) == "complete"
    assert dbf._grade(0.75, False) == "high"
    assert dbf._grade(0.5, False) == "medium"
    assert dbf._grade(0.1, False) == "low"
    assert dbf._grade(None, False) == "unknown"
    assert dbf._grade(0.2, True) == "complete"                          # exhausted overrides


# --- safety -----------------------------------------------------------------
def test_no_live_executions_created(in_memory_db):
    db = in_memory_db
    _mk(db, "0xa")
    public_profile.refresh_profiles(db, ["0xa"], force=True,
                                    fetch_fn=lambda x: {"address": x, "fetch_status": "ok", "fetched_at": datetime.utcnow(),
                                                        "pnl_all": 5000.0, "volume_all": 400.0, "predictions": 40})
    dbf.run_deep_backfill(db, batch=5, max_pages=5, page_size=4, fetch_fn=_fake_history(8))
    wap.set_status(db, "0xa", "approve", by="op")
    wap.set_status(db, "0xa", "disable", by="op")
    assert db.scalar(select(func.count()).select_from(LiveExecution)) == 0
