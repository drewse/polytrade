"""Discovery 2.0 — verifies discovery NEVER alters production eligibility, that
dedup + backfill priority are deterministic, and the read endpoint is read-only."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from app import discovery2, live_ranking
from app.models import (
    DiscoverySource, Market, Wallet, WalletCandidate, WalletStat,
)


def _wallet(db, addr, *, roi, pf, settled):
    w = Wallet(address=addr, copy_enabled=True, last_active=datetime.utcnow() - timedelta(days=1))
    db.add(w); db.flush()
    db.add(WalletStat(wallet_id=w.id, num_trades=settled, num_settled=settled, realized_roi=roi,
                      win_rate=0.6, profit_factor=pf, expectancy=10.0, sharpe=0.5, recency_score=0.9,
                      partial_history=False, consistency=0.6, avg_trade_size=50.0, max_drawdown=0.2))
    db.add(WalletCandidate(wallet_id=w.id, copyability_score=70.0, classification="good_candidate"))
    return w


# injectable fetchers (no network)
def _profit(window=None, limit=None):
    return [("0xLEADER", 1, 100000.0), ("0xLEADER", 1, 100000.0)]   # same wallet twice -> dedup
def _volume(window=None, limit=None):
    return [("0xVOLWALLET", 2, 5000000.0)]
def _holders(cid, limit=None):
    return [("0xHOLDER", 1, 9000.0, 0)]


def _refresh(db):
    return discovery2.refresh_discovery(db, profit_fn=_profit, volume_fn=_volume, holders_fn=_holders)


def test_discovery_does_not_change_eligibility(in_memory_db):
    db = in_memory_db
    _wallet(db, "0xprod", roi=0.28, pf=2.2, settled=100); db.commit()
    before = frozenset(live_ranking.eligible_addresses(db))
    before_wallets = db.scalar(select(func.count()).select_from(Wallet))
    before_stats = db.scalar(select(func.count()).select_from(WalletStat))

    _refresh(db)

    after = frozenset(live_ranking.eligible_addresses(db))
    assert before == after                                   # eligibility UNCHANGED
    assert db.scalar(select(func.count()).select_from(Wallet)) == before_wallets   # no Wallet rows created
    assert db.scalar(select(func.count()).select_from(WalletStat)) == before_stats # no stats created

    cands = {c["wallet"]: c for c in discovery2.discovery_candidates(db)["candidates"]}
    assert "0xleader" in cands and not cands["0xleader"]["production_eligible"]
    assert cands["0xleader"]["needs_backfill"] is True       # no stats -> queued for backfill


def test_dedup_and_upsert(in_memory_db):
    db = in_memory_db
    _refresh(db)
    # exactly ONE row for the leader in profit_30d despite being returned twice
    n = db.scalar(select(func.count()).select_from(DiscoverySource).where(
        DiscoverySource.wallet_address == "0xleader",
        DiscoverySource.discovery_source == "profit_leaderboard",
        DiscoverySource.source_detail == "profit_30d"))
    assert n == 1
    # the leader is found across all 4 profit windows -> 4 rows, deduped per detail
    total_leader = db.scalar(select(func.count()).select_from(DiscoverySource).where(
        DiscoverySource.wallet_address == "0xleader"))
    assert total_leader == 4

    # re-run upserts (no duplicate rows), updates rank
    discovery2.refresh_discovery(db, profit_fn=lambda window=None, limit=None: [("0xLEADER", 7, 1.0)],
                                 volume_fn=lambda **k: [], holders_fn=lambda *a, **k: [])
    n2 = db.scalar(select(func.count()).select_from(DiscoverySource).where(
        DiscoverySource.wallet_address == "0xleader", DiscoverySource.source_detail == "profit_30d"))
    assert n2 == 1
    row = db.scalar(select(DiscoverySource).where(
        DiscoverySource.wallet_address == "0xleader", DiscoverySource.source_detail == "profit_30d"))
    assert row.source_rank == 7


def test_backfill_priority_deterministic():
    p = discovery2._priority
    assert p("profit_leaderboard", "profit_30d") == 100
    assert p("profit_leaderboard", "profit_7d") == 90
    assert p("profit_leaderboard", "profit_1d") == 80
    assert p("top_holders", "holders:x") == 70
    assert p("volume_leaderboard", "volume_30d") == 60
    assert p("recent_trades", "recent") == 30
    # ordering: monthly > weekly > daily > holders > volume > recent
    assert (p("profit_leaderboard", "profit_30d") > p("profit_leaderboard", "profit_7d")
            > p("profit_leaderboard", "profit_1d") > p("top_holders", "x")
            > p("volume_leaderboard", "x") > p("recent_trades", "x"))
    assert discovery2._score(100, 1) == discovery2._score(100, 1)        # deterministic
    assert discovery2._score(100, 1) > discovery2._score(100, 10)        # rank-1 scores higher


def test_read_endpoint_is_read_only(in_memory_db):
    db = in_memory_db
    _wallet(db, "0xprod", roi=0.28, pf=2.2, settled=100); db.commit()
    _refresh(db)

    def snap():
        return (db.scalar(select(func.count()).select_from(DiscoverySource)),
                db.scalar(select(func.count()).select_from(Wallet)),
                db.scalar(select(func.count()).select_from(WalletStat)),
                frozenset(live_ranking.eligible_addresses(db)))

    before = snap()
    discovery2.discovery_candidates(db)
    discovery2.discovery_candidates(db, limit=5)
    assert snap() == before                                   # read changes nothing


def test_sources_and_summary(in_memory_db):
    db = in_memory_db
    db.add(Market(id="0xmkt1", question="Q", outcomes=["Yes", "No"], prices=[0.5, 0.5],
                  token_ids=["t1", "t2"], resolved=False, volume=999999))   # high-volume open mkt
    db.commit()
    _refresh(db)
    res = discovery2.discovery_candidates(db)
    assert res["read_only"] is True
    srcs = {s for c in res["candidates"] for s in c["discovery_sources"]}
    assert {"profit_leaderboard", "volume_leaderboard", "top_holders"} <= srcs
    # the volume wallet is present and carries the volume source priority (60)
    vol = next(c for c in res["candidates"] if c["wallet"] == "0xvolwallet")
    assert "volume_leaderboard" in vol["discovery_sources"] and vol["backfill_priority"] == 60
