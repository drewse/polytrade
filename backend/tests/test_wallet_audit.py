"""Top-20 wallet audit tests: READ-ONLY (no orders, eligible set unchanged),
public-fetch failure is fail-soft, cached stats are used, rolling windows compute
correctly, warning flags fire, and the score breakdown is deterministic and
matches the production ranking."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from app import live_ranking, public_profile, wallet_audit as wa
from app import wallet_audit_models as wm
from app.models import LiveExecution, Market, Trade, Wallet, WalletCandidate, WalletStat

ADDR = "0x4f29e103339919c4baaea2a60195cf1c8bb27a7e"


def _eligible_wallet(db, addr=ADDR, *, roi=0.25, pf=1.8, settled=30, partial=True, mdd=0.5):
    w = Wallet(address=addr, copy_enabled=True, last_active=datetime.utcnow() - timedelta(days=2))
    db.add(w); db.flush()
    db.add(WalletStat(wallet_id=w.id, num_trades=settled * 2, num_settled=settled, realized_roi=roi,
                      win_rate=0.6, profit_factor=pf, recency_score=0.9, partial_history=partial,
                      max_drawdown=mdd, avg_trade_size=50, realized_pnl=120))
    db.add(WalletCandidate(wallet_id=w.id, copyability_score=70, classification="good_candidate"))
    for i in range(settled):
        m = Market(id=f"m{i}", question=f"Q{i}", outcomes=["Yes", "No"], token_ids=["t1", "t2"],
                   prices=[1.0, 0.0], resolved=True, resolved_outcome="Yes",
                   resolved_at=datetime.utcnow() - timedelta(days=i % 40))
        db.add(m)
        db.add(Trade(external_id=f"t{i}", wallet_id=w.id, market_id=f"m{i}", outcome="Yes", side="buy",
                     price=0.5, size=50, timestamp=datetime.utcnow() - timedelta(days=i % 40)))
    db.commit()
    return w


_WHALE_PUBLIC = lambda addr: {
    "address": addr, "fetch_status": "ok", "fetch_error": None, "fetched_at": datetime.utcnow(),
    "display_name": "0x4f2", "pnl_all": -22088.48, "pnl_1d": -131181.0, "pnl_7d": -487010.0, "pnl_30d": 58186.0,
    "volume_all": 162_141_683.0, "position_value": 186181.0, "predictions": 40, "biggest_win": 5000.0,
    "biggest_loss": -72887.0, "largest_position_size": 166757.0,
    "top_positions": [{"title": "Belgium vs Iran draw", "size": 166757, "cashPnl": -72887}],
}


def test_audit_is_read_only(in_memory_db):
    db = in_memory_db
    _eligible_wallet(db)
    before = frozenset(live_ranking.eligible_addresses(db))
    n_exec = db.scalar(select(func.count()).select_from(LiveExecution))
    wa.top_wallets_audit(db, refresh_public=False)
    # eligible set + executions unchanged by running the audit
    assert frozenset(live_ranking.eligible_addresses(db)) == before
    assert db.scalar(select(func.count()).select_from(LiveExecution)) == n_exec == 0


def test_audit_surfaces_internal_public_and_breakdown(in_memory_db):
    db = in_memory_db
    _eligible_wallet(db)
    public_profile.refresh_profiles(db, [ADDR], force=True, fetch_fn=_WHALE_PUBLIC)
    out = wa.top_wallets_audit(db, refresh_public=False)
    assert out["audited"] == 1
    a = out["wallets"][0]
    assert a["copied"] is True and a["rank"] == 1
    assert a["public"]["pnl_all"] == -22088.48 and a["public"]["volume_all"] > 1e8
    # score breakdown is deterministic and equals the production rank score
    assert a["score_breakdown"]["total"] == a["production_rank_score"]
    comps = a["score_breakdown"]["components"]
    assert set(comps) == {"reputation", "profit_factor", "roi", "recency"}


def test_warning_flags_fire(in_memory_db):
    db = in_memory_db
    _eligible_wallet(db)
    public_profile.refresh_profiles(db, [ADDR], force=True, fetch_fn=_WHALE_PUBLIC)
    a = wa.top_wallets_audit(db)["wallets"][0]
    codes = {w["code"] for w in a["warnings"]}
    for expected in ("public_lifetime_loss", "low_coverage", "internal_public_conflict",
                     "likely_market_maker_whale", "high_drawdown", "recent_good_lifetime_bad"):
        assert expected in codes, f"missing warning {expected}"


def test_rolling_windows_compute_correctly(in_memory_db):
    db = in_memory_db
    w = Wallet(address="0xroll", copy_enabled=True, last_active=datetime.utcnow())
    db.add(w); db.flush()
    db.add(WalletStat(wallet_id=w.id, num_trades=4, num_settled=4, realized_roi=0.1, win_rate=0.75,
                      profit_factor=2.0, recency_score=0.9))
    # 2 wins resolved 2 days ago (inside 7d), 1 loss resolved 40 days ago (outside 30d)
    now = datetime.utcnow()
    specs = [(2, "Yes", 2), (3, "Yes", 2), (40, "No", 1)]
    for i, (age, out, _w) in enumerate(specs):
        db.add(Market(id=f"r{i}", question="Q", outcomes=["Yes", "No"], token_ids=["t1", "t2"], prices=[1.0, 0.0],
                      resolved=True, resolved_outcome="Yes", resolved_at=now - timedelta(days=age)))
        db.add(Trade(external_id=f"rt{i}", wallet_id=w.id, market_id=f"r{i}", outcome=out, side="buy",
                     price=0.5, size=10, timestamp=now - timedelta(days=age)))
    db.commit()
    _, positions = wa._settled_positions(db, w)
    roll = wa._rolling(positions, now)
    assert roll["7d"]["trades"] == 2 and roll["7d"]["pnl"] > 0        # the 2 recent wins
    assert roll["90d"]["trades"] == 3                                # all three within 90d
    assert roll["1d"]["trades"] == 0                                 # nothing in the last day


def test_public_fetch_failure_is_failsoft(in_memory_db):
    db = in_memory_db
    _eligible_wallet(db)

    def _boom(addr):
        raise RuntimeError("data-api down")

    info = public_profile.refresh_profiles(db, [ADDR], force=True, fetch_fn=_boom)
    assert info["errors"] == 1
    # the audit still works; the wallet's public block carries the error status
    out = wa.top_wallets_audit(db, refresh_public=False)
    a = out["wallets"][0]
    assert a["public"]["fetch_status"] == "error" and "data-api down" in (a["public"]["fetch_error"] or "")


def test_cached_profiles_are_reused_within_ttl(in_memory_db):
    db = in_memory_db
    _eligible_wallet(db)
    calls = []

    def _count(addr):
        calls.append(addr)
        return _WHALE_PUBLIC(addr)

    public_profile.refresh_profiles(db, [ADDR], fetch_fn=_count)
    assert len(calls) == 1
    public_profile.refresh_profiles(db, [ADDR], fetch_fn=_count)   # fresh -> skipped, no refetch
    assert len(calls) == 1
    assert db.scalar(select(func.count()).select_from(wm.PublicWalletProfile)) == 1


def test_score_breakdown_is_deterministic(in_memory_db):
    db = in_memory_db
    _eligible_wallet(db)
    a1 = wa.top_wallets_audit(db)["wallets"][0]["score_breakdown"]
    a2 = wa.top_wallets_audit(db)["wallets"][0]["score_breakdown"]
    assert a1 == a2
