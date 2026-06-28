"""Production ranking hardening gates: each gate excludes when enabled, audit-only
mode leaves eligibility UNCHANGED, enforcement mode changes it, unknown public
stats are handled safely, and passing wallets keep a deterministic score."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import live_ranking, public_profile, ranking_hardening
from app.models import Market, Trade, Wallet, WalletCandidate, WalletStat


def _pub(*, pnl_all=5000.0, volume=5000.0, predictions=40, status="ok", largest=None):
    return {"fetch_status": status, "pnl_all": pnl_all, "volume_all": volume,
            "predictions": predictions, "largest_position_size": largest}


# --- pure gate logic --------------------------------------------------------
def test_public_pnl_gate(monkeypatch):
    monkeypatch.setenv("LIVE_MIN_PUBLIC_ALL_TIME_PNL", "0")
    cfg = ranking_hardening.config()
    bad = ranking_hardening.evaluate(partial_history=False, public=_pub(pnl_all=-22000),
                                     internal_volume=1000, internal_settled=30, cfg=cfg)
    assert not bad["pass"] and any(e["code"] == "public_pnl_below_min" for e in bad["exclusions"])
    ok = ranking_hardening.evaluate(partial_history=False, public=_pub(pnl_all=5000),
                                    internal_volume=1000, internal_settled=30, cfg=cfg)
    assert ok["pass"]


def test_partial_history_gate(monkeypatch):
    monkeypatch.setenv("LIVE_ALLOW_PARTIAL_HISTORY", "false")
    cfg = ranking_hardening.config()
    v = ranking_hardening.evaluate(partial_history=True, public=_pub(), internal_volume=1000,
                                   internal_settled=30, cfg=cfg)
    assert not v["pass"] and any(e["code"] == "partial_history" for e in v["exclusions"])
    monkeypatch.setenv("LIVE_ALLOW_PARTIAL_HISTORY", "true")
    v2 = ranking_hardening.evaluate(partial_history=True, public=_pub(), internal_volume=1000,
                                    internal_settled=30, cfg=ranking_hardening.config())
    assert v2["pass"]


def test_coverage_gate(monkeypatch):
    monkeypatch.setenv("LIVE_MIN_COVERAGE_RATIO", "0.05")
    cfg = ranking_hardening.config()
    # internal volume 100 vs public 1,000,000 -> coverage 0.0001 << 0.05
    v = ranking_hardening.evaluate(partial_history=False, public=_pub(volume=1_000_000, predictions=10_000),
                                   internal_volume=100, internal_settled=30, cfg=cfg)
    assert not v["pass"] and any(e["code"] == "low_coverage" for e in v["exclusions"])
    assert v["coverage"] is not None


def test_whale_gate(monkeypatch):
    monkeypatch.setenv("LIVE_MAX_PUBLIC_VOLUME", "1000000")
    cfg = ranking_hardening.config()
    v = ranking_hardening.evaluate(partial_history=False, public=_pub(volume=162_000_000),
                                   internal_volume=1000, internal_settled=30, cfg=cfg)
    assert not v["pass"] and any(e["code"] == "whale_volume" for e in v["exclusions"])


def test_unknown_public_handled_safely(monkeypatch):
    cfg = ranking_hardening.config()
    # no public stats -> NOT excluded by default (only flagged unknown)
    v = ranking_hardening.evaluate(partial_history=False, public=None, internal_volume=1000,
                                   internal_settled=30, cfg=cfg)
    assert v["pass"] and "public_stats" in v["unknowns"]
    # ...unless explicitly required
    monkeypatch.setenv("LIVE_REQUIRE_PUBLIC_STATS", "true")
    v2 = ranking_hardening.evaluate(partial_history=False, public=None, internal_volume=1000,
                                    internal_settled=30, cfg=ranking_hardening.config())
    assert not v2["pass"] and any(e["code"] == "missing_public_stats" for e in v2["exclusions"])


# --- integration: audit-only vs enforcement ---------------------------------
def _mk(db, addr, *, partial, pnl_all, vol):
    w = Wallet(address=addr, copy_enabled=True, last_active=datetime.utcnow() - timedelta(days=2))
    db.add(w); db.flush()
    db.add(WalletStat(wallet_id=w.id, num_trades=60, num_settled=30, realized_roi=0.25, win_rate=0.6,
                      profit_factor=1.8, recency_score=0.9, partial_history=partial, max_drawdown=0.2,
                      avg_trade_size=50, realized_pnl=120))
    db.add(WalletCandidate(wallet_id=w.id, copyability_score=70, classification="good"))
    for i in range(30):
        mid = f"{addr}-m{i}"
        db.add(Market(id=mid, question="Q", outcomes=["Yes", "No"], token_ids=["t1", "t2"], prices=[1.0, 0.0],
                      resolved=True, resolved_outcome="Yes", resolved_at=datetime.utcnow() - timedelta(days=i)))
        db.add(Trade(external_id=f"{addr}-t{i}", wallet_id=w.id, market_id=mid, outcome="Yes", side="buy",
                     price=0.5, size=10, timestamp=datetime.utcnow() - timedelta(days=i)))
    db.commit()
    public_profile.refresh_profiles(db, [addr], force=True,
                                    fetch_fn=lambda a: {"address": a, "fetch_status": "ok", "fetch_error": None,
                                                        "fetched_at": datetime.utcnow(), "pnl_all": pnl_all,
                                                        "volume_all": vol, "predictions": 40})


def test_audit_only_does_not_change_eligible_set(in_memory_db, monkeypatch):
    db = in_memory_db
    _mk(db, "0xclean", partial=False, pnl_all=5000.0, vol=5000.0)
    _mk(db, "0xwhale", partial=True, pnl_all=-22000.0, vol=162_000_000.0)
    monkeypatch.setenv("LIVE_RANKING_AUDIT_ONLY", "true")
    assert frozenset(live_ranking.eligible_addresses(db)) == {"0xclean", "0xwhale"}   # UNCHANGED


def test_enforcement_changes_eligible_set(in_memory_db, monkeypatch):
    db = in_memory_db
    _mk(db, "0xclean", partial=False, pnl_all=5000.0, vol=5000.0)
    _mk(db, "0xwhale", partial=True, pnl_all=-22000.0, vol=162_000_000.0)
    monkeypatch.setenv("LIVE_RANKING_AUDIT_ONLY", "false")
    assert frozenset(live_ranking.eligible_addresses(db)) == {"0xclean"}              # whale removed


def test_passing_wallet_score_deterministic_and_unchanged(in_memory_db, monkeypatch):
    db = in_memory_db
    _mk(db, "0xclean", partial=False, pnl_all=5000.0, vol=5000.0)
    monkeypatch.setenv("LIVE_RANKING_AUDIT_ONLY", "false")
    r1 = next(r for r in live_ranking.rank_wallets(db) if r["address"] == "0xclean")
    r2 = next(r for r in live_ranking.rank_wallets(db) if r["address"] == "0xclean")
    assert r1["production_rank_score"] == r2["production_rank_score"]
    assert r1["eligible"] and r1["hardened_pass"]
