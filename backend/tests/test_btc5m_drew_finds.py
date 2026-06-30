"""DREW FINDS tests: slug categorisation, wallet profiling + reverse-engineered
strategy, similar-wallet discovery from co-traders, our-indexed cross-reference, the
cached run, and read-only isolation (no LiveExecution / bankroll touch). All network
fetchers are injected so the tests run offline."""
from __future__ import annotations

from sqlalchemy import func, select

from app import btc5m_drew_finds as df
from app import btc5m_models as bm
from app import live
from app.models import LiveExecution


# --- categorisation ---------------------------------------------------------
def test_categorize():
    assert df.categorize("btc-updown-5m-1782777300") == "btc_5m_updown"
    assert df.categorize("xrp-updown-5m-1782607200") == "crypto_updown_other"
    assert df.categorize("fifwc-nld-mar-2026-06-29-nld") == "sports"
    assert df.categorize("some-election-market") == "other"


# --- synthetic public-API fixtures ------------------------------------------
def _btc5m_trades(addr, n=40, buy_pct=0.9, px=0.41, n_markets=8):
    out = []
    for i in range(n):
        out.append({"proxyWallet": addr, "side": "BUY" if i < n * buy_pct else "SELL",
                    "conditionId": f"cid{i % n_markets}", "slug": f"btc-updown-5m-{1782777300 + (i % n_markets)*300}",
                    "size": 30.0, "price": px, "timestamp": 1782700000 + i * 600,
                    "pseudonym": "", "name": ""})
    return out


def _sports_trades(addr, n=30):
    return [{"proxyWallet": addr, "side": "BUY", "conditionId": f"s{i}",
             "slug": f"fifwc-team-{i}", "size": 50000.0, "price": 0.6,
             "timestamp": 1782700000 + i * 600, "pseudonym": "Substantial-Service"} for i in range(n)]


def _fetch_factory():
    seed = df.TARGETS[0]["address"]
    whale = df.TARGETS[1]["address"]
    co = "0xc0trader00000000000000000000000000000001"
    market_trades = {f"cid{i}": (_btc5m_trades(seed, 4) + _btc5m_trades(co, 4, buy_pct=0.85, px=0.43)) for i in range(8)}

    def trades_fn(*, user=None, market=None, limit=1000):
        if user == seed:
            return _btc5m_trades(seed)
        if user == whale:
            return _sports_trades(whale)
        if market in market_trades:
            return market_trades[market]
        return []

    def pnl_fn(address):
        return {seed: 3818.0, whale: 71354.0, co: 250.0}.get(address, -10.0)
    return trades_fn, pnl_fn


# --- profiling --------------------------------------------------------------
def test_profile_btc5m_scalper():
    trades_fn, pnl_fn = _fetch_factory()
    prof = df.profile_wallet(df.TARGETS[0], trades_fn=trades_fn, pnl_fn=pnl_fn)
    assert prof["btc_5m_pct"] == 100.0 and prof["all_time_pnl"] == 3818.0
    assert "BTC 5-minute up/down specialist" in prof["strategy"]
    assert "CHEAP" in prof["strategy"]           # avg price 0.41 -> buys cheap side


def test_profile_sports_whale_excluded():
    trades_fn, pnl_fn = _fetch_factory()
    prof = df.profile_wallet(df.TARGETS[1], trades_fn=trades_fn, pnl_fn=pnl_fn)
    assert prof["btc_5m_pct"] == 0.0
    assert "NOT a BTC-5m trader" in prof["strategy"]


# --- similar wallets --------------------------------------------------------
def test_find_similar_from_cotraders():
    trades_fn, pnl_fn = _fetch_factory()
    seed_trades = trades_fn(user=df.TARGETS[0]["address"])
    seed_stats = df.profile_wallet(df.TARGETS[0], trades_fn=trades_fn, pnl_fn=pnl_fn)
    sim = df.find_similar(seed_trades, seed_stats, trades_fn=trades_fn, pnl_fn=pnl_fn, min_trades=2)
    assert sim and all("similarity" in s and "all_time_pnl" in s for s in sim)
    assert all(s["wallet"] != df.TARGETS[0]["address"].lower() for s in sim)   # seed excluded
    assert sim[0]["trades"] > 0 and 0 <= sim[0]["similarity"] <= 1


# --- our indexed cross-reference --------------------------------------------
def test_our_specialists(in_memory_db):
    db = in_memory_db
    db.add(bm.Btc5mWalletProfile(wallet_address="0xprof", profitable=True, realized_pnl=3042.0,
                                 roi=0.013, win_rate=0.715, trade_count=3400, profit_factor=1.2,
                                 avg_trade_size=20.0, cluster="Momentum"))
    db.add(bm.Btc5mWalletProfile(wallet_address="0xloss", profitable=False, realized_pnl=-50.0))
    db.commit()
    out = df.our_btc5m_specialists(db)
    assert len(out) == 1 and out[0]["wallet"] == "0xprof" and out[0]["cluster"] == "Momentum"


# --- full run + isolation ---------------------------------------------------
def test_run_builds_report_and_isolation(in_memory_db):
    db = in_memory_db
    trades_fn, pnl_fn = _fetch_factory()
    rep = df.run(db, trades_fn=trades_fn, pnl_fn=pnl_fn)
    assert len(rep["targets"]) == 2
    assert rep["seed_wallet"] == "@std0"
    assert rep["similar_btc5m_wallets"]           # found co-traders
    assert "BTC-5m scalper" in rep["summary"]
    # cached + readable
    st = df.status(db)
    assert st["report"]["seed_wallet"] == "@std0"
    # READ-ONLY: nothing trades
    assert db.scalar(select(func.count()).select_from(LiveExecution)) == 0


def test_status_safety(in_memory_db):
    db = in_memory_db
    bank0 = live.get_state(db).bankroll
    st = df.status(db)
    assert "read-only" in st["safety"] and "@std0" in st["targets_configured"]
    assert live.get_state(db).bankroll == bank0
