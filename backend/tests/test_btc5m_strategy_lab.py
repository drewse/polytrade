"""BTC 5M Independent Strategy Lab tests: BTC/PM/flow feature engine, strategy
families + backtest metrics, dataset build (injected BTC price), train/val/holdout
search with overfit rejection + robust ranking, analyses, report classifier, and
paper-only isolation (no LiveExecution / bankroll / orders)."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from app import btc5m_strategy_lab as lab
from app import btc5m_models as bm
from app import btc5m_strategy_models as lm
from app import live
from app.models import LiveExecution
from types import SimpleNamespace as NS


# --- feature engine ---------------------------------------------------------
def test_btc_features_rising_series():
    series = [(t, 60000 + t * 0.6) for t in range(0, 151)]   # +0.6 $/s
    f = lab.btc_features(series, 60)
    assert f["btc_ret_sofar"] > 0 and f["btc_candle"] == 1 and f["btc_momentum"] > 0


def test_pm_flow_features_and_lag():
    f_btc = {"btc_ret_sofar": 0.0006}                          # BTC up 0.06%
    trades = [NS(seconds_from_creation=s, side="buy", direction="YES" if s % 2 else "NO",
                 usd_value=60 if s % 2 else 20, price=0.55) for s in range(5, 60, 5)]
    pf = lab.pm_flow_features(trades, 60, f_btc["btc_ret_sofar"])
    assert pf["pm_yes"] == 0.55 and pf["flow_imbalance"] > 0 and pf["has_large_trade"] == 1
    assert pf["lag"] > 0                                       # BTC up more than YES priced


def _point(**over):
    base = dict(pm_yes=0.55, spread=0.0, t_offset_s=60, secs_to_expiry=240, duration_minutes=5,
                regime="mixed", btc_ret_sofar=0.0008, btc_momentum=0.5, lag=0.18,
                recent_flow_imbalance=0.6, has_large_trade=1, flow_imbalance=0.6,
                pm_momentum=0.02, label_up=True)
    base.update(over)
    return base


# --- strategy families + backtest -------------------------------------------
def test_evaluate_btc_lead():
    prm = dict(max_spread=0.1, entry_min=20, entry_max=150, min_secs_left=30,
               min_lag=0.1, min_btc_move=0.0003, require_momentum=False)
    assert lab.evaluate_strategy(_point(), "btc_lead", prm) == "YES"          # BTC up + lag
    assert lab.evaluate_strategy(_point(btc_ret_sofar=-0.001, lag=-0.2), "btc_lead", prm) == "NO"
    assert lab.evaluate_strategy(_point(lag=0.02), "btc_lead", prm) is None    # no lag
    assert lab.evaluate_strategy(_point(spread=0.2), "btc_lead", prm) is None  # spread filter


def test_evaluate_fade_and_flow():
    prm_f = dict(max_spread=0.1, entry_min=20, entry_max=150, min_secs_left=30,
                 min_yes_dev=0.2, max_btc_move=0.0003)
    # YES far from 0.5 (0.8) but BTC flat -> fade -> NO
    assert lab.evaluate_strategy(_point(pm_yes=0.8, btc_ret_sofar=0.0001), "fade_overreaction", prm_f) == "NO"
    prm_fl = dict(max_spread=0.1, entry_min=20, entry_max=150, min_secs_left=30,
                  min_imbalance=0.3, require_btc_confirm=True, require_large=False)
    assert lab.evaluate_strategy(_point(recent_flow_imbalance=0.6, btc_ret_sofar=0.001), "flow_confirm", prm_fl) == "YES"


def test_backtest_metrics():
    prm = dict(max_spread=0.1, entry_min=20, entry_max=150, min_secs_left=30,
               min_lag=0.1, min_btc_move=0.0003, require_momentum=False)
    wins = [_point() for _ in range(8)]                       # YES @0.55, up -> win
    losses = [_point(label_up=False) for _ in range(2)]       # YES @0.55, down -> lose
    m = lab.backtest(wins + losses, "btc_lead", prm, slippage=0.01)
    assert m["trades"] == 10 and m["win_rate"] == 0.8 and m["roi"] > 0
    assert m["profit_factor"] > 1 and "5" in m["by_duration"]


# --- dataset build (injected BTC price) -------------------------------------
def _seed_market(db, mid, *, up=True, dur=5, created=None, yes=0.5):
    created = created or datetime(2026, 6, 28, 12, 0, 0)
    db.add(bm.Btc5mMarket(market_id=mid, slug=f"btc-updown-{dur}m-1", question="Bitcoin Up or Down",
                          created_time=created, resolution_time=created + timedelta(minutes=dur),
                          resolved=True, final_outcome="Up" if up else "Down"))
    for s in range(5, dur * 60 - 30, 15):
        db.add(bm.Btc5mTrade(market_id=mid, wallet_address="0xw", side="buy", direction="YES",
                             price=yes, shares=10, usd_value=60, timestamp=created + timedelta(seconds=s),
                             seconds_from_creation=s, seconds_until_expiry=dur * 60 - s, features={}))
    db.commit()


def _btc_fetch(up=True):
    # rising 0.1% (or falling) over the window
    def fn(start, end):
        secs = int((end - start).total_seconds())
        sign = 1 if up else -1
        return [(t, 60000 * (1 + sign * 0.001 * t / secs)) for t in range(0, secs + 1, 5)]
    return fn


def test_build_dataset_creates_points_and_splits(in_memory_db):
    db = in_memory_db
    for i in range(12):
        _seed_market(db, f"m{i}", up=(i % 2 == 0), created=datetime(2026, 6, 28, 12, i, 0))
    out = lab.build_dataset(db, limit_markets=12, fetch_fn=_btc_fetch(up=True))
    assert out["points_built"] > 0 and out["btc_source"] == "injected"
    pts = db.scalars(select(lm.Btc5mLabPoint)).all()
    assert all(p.features and p.label_up is not None for p in pts)
    splits = {s for (s,) in db.execute(select(lm.Btc5mLabPoint.split).distinct()).all()}
    assert "train" in splits and "holdout" in splits
    # paper-only isolation
    assert db.scalar(select(func.count()).select_from(LiveExecution)) == 0


# --- search: real edge accepted, overfit rejected ---------------------------
def _insert_point(db, *, split, label_up, **f):
    feats = _point(**f)
    db.add(lm.Btc5mLabPoint(market_id=f.get("market_id", "mx"), duration_minutes=5,
                            t_offset_s=feats["t_offset_s"], secs_to_expiry=feats["secs_to_expiry"],
                            regime="mixed", features=feats, pm_yes=feats["pm_yes"], spread=feats["spread"],
                            btc_ret_30s=0.0, flow_imbalance=feats["flow_imbalance"],
                            label_up=label_up, split=split))


def test_search_accepts_real_edge_and_rejects_overfit(in_memory_db):
    db = in_memory_db
    # design a true btc_lead edge: BTC up + lag>0 -> resolves Up (YES@0.55 wins) in ALL splits
    import random
    random.seed(1)
    for split in ("train", "val", "holdout"):
        for i in range(20):
            up = True
            _insert_point(db, split=split, label_up=up, market_id=f"{split}{i}",
                          btc_ret_sofar=0.001, lag=0.2, pm_yes=0.55)
        # a few noise points (BTC down)
        for i in range(6):
            _insert_point(db, split=split, label_up=False, market_id=f"{split}n{i}",
                          btc_ret_sofar=-0.001, lag=-0.2, pm_yes=0.55)
    db.commit()
    res = lab.run_search(db, min_train_trades=5)
    assert res["ok"] and res["tested"] > 100
    lb = lab.leaderboard(db)
    assert len(lb["accepted"]) >= 1
    top = lb["accepted"][0]
    assert top["family"] == "btc_lead" and top["roi"] > 0 and not top["overfit"]


def test_search_no_edge_finds_nothing(in_memory_db):
    db = in_memory_db
    # random labels -> no durable edge
    import random
    random.seed(2)
    for split in ("train", "val", "holdout"):
        for i in range(25):
            _insert_point(db, split=split, label_up=bool(random.getrandbits(1)),
                          market_id=f"{split}{i}", btc_ret_sofar=random.uniform(-0.002, 0.002),
                          lag=random.uniform(-0.3, 0.3), pm_yes=0.5)
    db.commit()
    lab.run_search(db, min_train_trades=5)
    rep = lab.build_report(db)
    assert rep["verdict_code"] in (1, 2, 3, 4, 5)
    # with random labels the best strategy should not have a strong durable edge
    assert rep["n_accepted"] == 0 or rep["best_strategy"]["holdout_roi"] < 0.5


# --- analyses + report ------------------------------------------------------
def test_lag_and_flow_analyses(in_memory_db):
    db = in_memory_db
    # lag>0 consistently resolves Up -> positive corr
    for i in range(30):
        up = i % 3 != 0
        _insert_point(db, split="train", label_up=up, market_id=f"a{i}",
                      lag=0.2 if up else -0.2, flow_imbalance=0.5 if up else -0.5,
                      btc_ret_sofar=0.001 if up else -0.001)
    db.commit()
    la = lab.lag_analysis(db)
    fl = lab.flow_imbalance_analysis(db)
    assert la["lag_vs_resolution_corr"] > 0.3 and fl["flow_vs_resolution_corr"] > 0.3


def test_report_classifies_btc_lead(in_memory_db):
    db = in_memory_db
    for split in ("train", "val", "holdout"):
        for i in range(20):
            _insert_point(db, split=split, label_up=True, market_id=f"{split}{i}",
                          btc_ret_sofar=0.001, lag=0.2)
        for i in range(6):
            _insert_point(db, split=split, label_up=False, market_id=f"{split}n{i}",
                          btc_ret_sofar=-0.001, lag=-0.2)
    db.commit()
    lab.run_search(db, min_train_trades=5)
    rep = lab.build_report(db)
    assert rep["verdict_code"] == 1 and "BTC" in rep["headline"]
    assert db.scalar(select(func.count()).select_from(LiveExecution)) == 0   # paper-only


def test_status_paper_only(in_memory_db, monkeypatch):
    db = in_memory_db
    bank0 = live.get_state(db).bankroll
    s = lab.status(db)
    assert "research/paper only" in s["safety"]
    assert live.get_state(db).bankroll == bank0
