"""Tests for wallet discovery + copyability scoring."""
from __future__ import annotations

from types import SimpleNamespace

from app import copyability, discovery
from app.models import Trade, Wallet, WalletCandidate, WalletStat
from tests.conftest import make_trade


# --- pure extraction / filtering / selection --------------------------------
def _t(addr, size):
    return SimpleNamespace(wallet_address=addr, size=size)


def test_extract_candidates_aggregates():
    trades = [_t("0xA", 100), _t("0xA", 200), _t("0xB", 50)]
    seeds = discovery.extract_candidates(trades)
    assert seeds["0xA"].batch_trades == 2
    assert seeds["0xA"].batch_notional == 300
    assert seeds["0xA"].avg_notional == 150
    assert seeds["0xB"].batch_trades == 1


def test_filter_candidates_drops_dust():
    seeds = discovery.extract_candidates([_t("0xbig", 500), _t("0xdust", 3)])
    kept = discovery.filter_candidates(seeds, min_notional=25)
    addrs = {s.address for s in kept}
    assert "0xbig" in addrs and "0xdust" not in addrs


def test_select_for_backfill_caps_and_prioritizes():
    seeds = {a: discovery.CandidateSeed(a, 1, n) for a, n in
             [("0x1", 10), ("0x2", 900), ("0x3", 500), ("0x4", 700)]}
    picked = discovery.select_for_backfill(list(seeds.values()), max_n=2)
    assert len(picked) == 2
    # biggest notional first
    assert [p.address for p in picked] == ["0x2", "0x4"]


# --- copyability scoring -----------------------------------------------------
def _stat(**kw):
    base = dict(num_trades=60, realized_roi=0.25, win_rate=0.68, avg_trade_size=250,
                recency_score=0.8, consistency=0.7, category_performance={"Crypto": 0.3})
    base.update(kw)
    return SimpleNamespace(**base)


def _trades(n, distinct=15, settled=True):
    out = []
    for i in range(n):
        out.append(make_trade(1, f"m{i % distinct}", "Yes", 0.4, 250,
                              realized_pnl=(50 if settled else 0.0)))
    return out


def test_elite_candidate_scores_high():
    r = copyability.score_copyability(_stat(), _trades(80, distinct=18))
    assert r.classification in ("elite_candidate", "good_candidate")
    assert r.copyability_score >= 60
    assert not r.suspected_noise


def test_tiny_sample_is_insufficient():
    r = copyability.score_copyability(_stat(num_trades=5), _trades(5), min_trade_count=15)
    assert r.classification == "insufficient_data"


def test_too_good_to_be_true_is_capped():
    # 95% win rate but only ~18 settled trades -> capped + flagged
    r = copyability.score_copyability(
        _stat(num_trades=18, win_rate=0.95), _trades(18, distinct=10)
    )
    assert r.suspected_noise
    assert r.classification == "ignore"
    assert r.copyability_score <= 47


def test_micro_notional_flagged_as_noise():
    r = copyability.score_copyability(
        _stat(num_trades=60, avg_trade_size=4.0), _trades(60, distinct=8)
    )
    assert r.suspected_noise
    assert r.classification == "ignore"


def test_few_markets_high_volume_flagged():
    r = copyability.score_copyability(_stat(num_trades=40), _trades(40, distinct=1))
    assert r.suspected_noise


def test_losing_wallet_ignored():
    r = copyability.score_copyability(
        _stat(realized_roi=-0.3, win_rate=0.32, category_performance={"Crypto": -0.3}),
        _trades(50, distinct=15),
    )
    assert r.classification in ("ignore", "watchlist")
    assert r.copyability_score < 60


def test_classify_bands():
    assert copyability.classify(85, False) == "elite_candidate"
    assert copyability.classify(65, False) == "good_candidate"
    assert copyability.classify(45, False) == "watchlist"
    assert copyability.classify(20, False) == "ignore"
    assert copyability.classify(90, True) == "ignore"  # noise overrides


# --- DB: discovery run + track/ignore state ---------------------------------
def _seed_wallet(db, addr, n, win_pnl=50, size=250, distinct=12):
    w = Wallet(address=addr, copy_enabled=False)
    db.add(w)
    db.flush()
    db.add(WalletStat(wallet_id=w.id, num_trades=n, realized_roi=0.25, win_rate=0.66,
                      avg_trade_size=size, recency_score=0.8, consistency=0.7,
                      score=70, classification="sharp",
                      category_performance={"Crypto": 0.3}))
    from app.models import Market
    for i in range(distinct):
        if db.get(Market, f"m{i}") is None:
            db.add(Market(id=f"m{i}", question="Q", outcomes=["Yes", "No"], prices=[0.5, 0.5]))
    db.flush()
    for i in range(n):
        db.add(Trade(wallet_id=w.id, market_id=f"m{i % distinct}", outcome="Yes",
                     side="buy", price=0.4, size=size, realized_pnl=win_pnl,
                     timestamp=make_trade(1, "m", "Yes", 0.4, 1, days_ago=i % 30).timestamp))
    db.commit()
    return w


def test_run_discovery_mock_creates_candidates(in_memory_db):
    db = in_memory_db
    from app.models import Setting
    for k, v in {"data_mode": "mock", "min_candidate_trade_count": "15",
                 "min_candidate_notional": "25", "max_wallets_to_backfill_per_cycle": "5"}.items():
        db.add(Setting(key=k, value=v))
    db.commit()
    _seed_wallet(db, "0xgood", 60)
    _seed_wallet(db, "0xtiny", 4)
    settings = {"data_mode": "mock", "min_candidate_trade_count": 15,
                "min_candidate_notional": 25, "max_wallets_to_backfill_per_cycle": 5}
    summary = discovery.run_discovery(db, settings)
    assert summary["evaluated"] == 2
    cands = db.query(WalletCandidate).all()
    assert len(cands) == 2
    by_addr = {db.get(Wallet, c.wallet_id).address: c for c in cands}
    assert by_addr["0xtiny"].classification == "insufficient_data"
    assert by_addr["0xgood"].classification in ("elite_candidate", "good_candidate", "watchlist")


def test_track_and_ignore_state(in_memory_db):
    db = in_memory_db
    _seed_wallet(db, "0xw", 40)
    discovery.run_discovery(db, {"data_mode": "mock", "min_candidate_trade_count": 15,
                                 "min_candidate_notional": 25,
                                 "max_wallets_to_backfill_per_cycle": 5})
    cand = discovery.set_candidate_state(db, "0xw", "tracked")
    assert cand.state == "tracked"
    assert db.scalar(__import__("sqlalchemy").select(Wallet).where(Wallet.address == "0xw")).copy_enabled is True
    cand = discovery.set_candidate_state(db, "0xw", "ignored")
    assert cand.state == "ignored"
    assert db.scalar(__import__("sqlalchemy").select(Wallet).where(Wallet.address == "0xw")).copy_enabled is False


def test_backfill_limit_respected():
    # select_for_backfill must never return more than max_n (worker discovery cap)
    seeds = [discovery.CandidateSeed(f"0x{i}", 1, 100 + i) for i in range(20)]
    assert len(discovery.select_for_backfill(seeds, max_n=3)) == 3
    assert len(discovery.select_for_backfill(seeds, max_n=0)) == 0
