"""BTC 5M Alpha Research Platform tests: calibration metrics, EV-after-cost
significance gate, fair-value probability model, feature discovery (promote/prune),
ensemble of perspective models, microstructure + cross-market analytics,
evolutionary search, decay detection, the nightly pipeline + verdict, the
wallet-as-feature, and paper-only isolation (no LiveExecution / bankroll / orders).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace as NS

from sqlalchemy import func, select

from app import btc5m_alpha_research as research
from app import btc5m_strategy_lab as lab
from app import btc5m_models as bm
from app import btc5m_strategy_models as lm
from app import live
from app.models import LiveExecution


# --- point helpers (mirror the strategy-lab dataset row) --------------------
def _feats(**over):
    base = dict(pm_yes=0.5, spread=0.0, t_offset_s=60, secs_to_expiry=240, duration_minutes=5,
                regime="mixed", btc_ret_sofar=0.001, btc_momentum=0.5, lag=0.18,
                btc_ret_1s=0.0003, btc_ret_2s=0.0006, btc_ret_3s=0.0008, btc_ret_5s=0.001,
                btc_ret_10s=0.0012, btc_ret_20s=0.0014, btc_ret_30s=0.0015, btc_ret_60s=0.0015,
                btc_acceleration=0.1, btc_vol=0.0005, btc_breakout=1, btc_candle=1, pm_momentum=0.0,
                recent_flow_imbalance=0.6, has_large_trade=1, large_trade_usd=60.0,
                flow_imbalance=0.6, volume_usd=200.0, trade_freq=0.2,
                wallet_signal=0.5, wallet_recent_signal=0.5, wallet_trade_count=3, wallet_present=1)
    base.update(over)
    return base


def _insert(db, *, split, label_up, mid, **f):
    if "btc_ret_sofar" in f:
        for k in ("btc_ret_1s", "btc_ret_2s", "btc_ret_3s", "btc_ret_5s",
                  "btc_ret_10s", "btc_ret_20s", "btc_ret_30s", "btc_ret_60s"):
            f.setdefault(k, f["btc_ret_sofar"])
    feats = _feats(**f)
    db.add(lm.Btc5mLabPoint(market_id=mid, duration_minutes=feats["duration_minutes"],
                            t_offset_s=feats["t_offset_s"], secs_to_expiry=feats["secs_to_expiry"],
                            regime=feats["regime"], features=feats, pm_yes=feats["pm_yes"],
                            spread=feats["spread"], btc_ret_30s=feats["btc_ret_30s"],
                            flow_imbalance=feats["flow_imbalance"], label_up=label_up, split=split))


def _seed_predictive(db, n=22):
    """A learnable dataset: BTC direction predicts the outcome, market priced at 0.5
    so the model has a gap to exploit. Filled across train/val/holdout."""
    for split in ("train", "val", "holdout"):
        for i in range(n):
            up = i % 2 == 0
            _insert(db, split=split, label_up=up, mid=f"{split}{i}",
                    btc_ret_sofar=0.0015 if up else -0.0015, lag=0.2 if up else -0.2,
                    flow_imbalance=0.6 if up else -0.6, wallet_signal=0.6 if up else -0.6,
                    pm_yes=0.5)
    db.commit()
    st = lab._state(db)
    st.btc_resolution_s = 1
    st.btc_coverage_pct = 99.0
    st.points_built = db.scalar(select(func.count()).select_from(lm.Btc5mLabPoint)) or 0
    st.lag_profile = {"0": 0.0, "1": 0.03, "2": 0.06, "3": 0.07, "4": 0.05}
    db.commit()


# --- calibration metrics ----------------------------------------------------
def test_calibration_metrics():
    probs = [0.9, 0.8, 0.7, 0.2, 0.1, 0.3]
    ys = [1, 1, 1, 0, 0, 0]
    b = research.brier_score(probs, ys)
    assert 0.0 <= b < 0.25                      # better than the 0.5-forecaster floor
    assert research.calibration_score(b) > 0    # positive skill
    assert research.auc_score(probs, ys) == 1.0  # perfectly separable
    curve = research.reliability_curve(probs, ys, bins=5)
    assert curve and all("predicted" in c and "actual" in c for c in curve)


def test_auc_no_discrimination():
    assert research.auc_score([0.5, 0.5, 0.5, 0.5], [1, 0, 1, 0]) == 0.5


# --- EV after costs + significance gate -------------------------------------
def test_ev_significant_when_model_beats_market():
    # model says 0.8 YES, market 0.5; 8 of 10 resolve up -> positive, significant EV
    pts = [_feats(pm_yes=0.5, label_up=(i < 8)) for i in range(10)]
    probs = [0.8] * 10
    ev = research.ev_after_costs(probs, pts, slippage=0.01)
    assert ev["n_trades"] == 10 and ev["ev_after_cost"] > 0
    assert ev["t_stat"] >= 1.96 and ev["significant"] is True
    assert ev["ci_low"] <= ev["ev_after_cost"] <= ev["ci_high"]


def test_ev_no_trades_when_priced_efficiently():
    # model agrees with market (no gap) -> edge <= cost -> no trades, not significant
    pts = [_feats(pm_yes=0.5, label_up=(i % 2 == 0)) for i in range(10)]
    probs = [0.5] * 10
    ev = research.ev_after_costs(probs, pts, slippage=0.01)
    assert ev["n_trades"] == 0 and ev["significant"] is False


# --- fair-value model -------------------------------------------------------
def test_fair_value_learns_and_reports(in_memory_db):
    db = in_memory_db
    _seed_predictive(db)
    fv = research.fair_value(db, slippage=0.01)
    assert fv["ok"] and fv["n_train"] >= 20
    assert fv["auc"] > 0.5                       # learned to discriminate
    assert "reliability" in fv and "ev" in fv and fv["sample"]
    assert any(tf["feature"].startswith("btc_ret") for tf in fv["top_features"])


def test_fair_value_too_small(in_memory_db):
    db = in_memory_db
    _insert(db, split="train", label_up=True, mid="a", btc_ret_sofar=0.001)
    db.commit()
    assert research.fair_value(db)["ok"] is False


# --- feature discovery ------------------------------------------------------
def test_discover_features_promotes_and_prunes(in_memory_db):
    db = in_memory_db
    _seed_predictive(db)
    out = research.discover_features(db, top=12)
    assert out["ok"] and out["generated"] > 30
    # a transform of a predictive base feature should be promoted
    assert out["promoted"], "expected at least one stable predictive feature"
    names = {p["feature"] for p in out["promoted"]}
    assert any("btc_ret" in n or "sign" in n or "flow" in n or "wallet" in n for n in names)


# --- ensemble ---------------------------------------------------------------
def test_ensemble_combines_perspectives(in_memory_db):
    db = in_memory_db
    _seed_predictive(db)
    ens = research.ensemble(db, slippage=0.01)
    assert ens["ok"] and len(ens["members"]) >= 3
    assert abs(sum(m["weight"] for m in ens["members"]) - 1.0) < 1e-6   # weights normalized
    assert "ensemble" in ens and ens["ensemble"]["n"] > 0
    # perspectives are present
    persp = {m["perspective"] for m in ens["members"]}
    assert "price_action" in persp and "wallet_behavior" in persp


# --- microstructure + cross-market ------------------------------------------
def test_microstructure_and_cross_market(in_memory_db):
    db = in_memory_db
    _seed_predictive(db)
    micro = research.microstructure(db)
    assert micro["ok"] and "large_trade_impact" in micro and "spread" in micro
    assert "order_book_depth" in str(micro["unavailable"])
    cross = research.cross_market(db)
    assert cross["ok"] and "by_duration" in cross
    assert "ETH prediction markets" in cross["scoped_next"]


# --- evolutionary search ----------------------------------------------------
def test_evolve_finds_survivors_on_edge(in_memory_db):
    db = in_memory_db
    # strong btc_lead edge in all splits so a rule strategy survives + mutates
    for split in ("train", "val", "holdout"):
        for i in range(20):
            _insert(db, split=split, label_up=True, mid=f"{split}{i}",
                    btc_ret_sofar=0.001, lag=0.2, pm_yes=0.55)
        for i in range(6):
            _insert(db, split=split, label_up=False, mid=f"{split}n{i}",
                    btc_ret_sofar=-0.001, lag=-0.2, pm_yes=0.55)
    db.commit()
    evo = research.evolve(db, generations=3, slippage=0.01)
    assert evo["ok"] and evo["evaluated"] > 50
    assert len(evo["generation_log"]) == 4                  # gen 0 + 3
    assert evo["best"] is not None and evo["best"]["holdout_trades"] >= research.MIN_TRADES


# --- decay ------------------------------------------------------------------
def test_detect_decay(in_memory_db):
    db = in_memory_db
    _seed_predictive(db)
    d = research.detect_decay(db)
    assert d["ok"] and "brier_degradation" in d and "decayed" in d


# --- nightly pipeline + report + persistence + isolation --------------------
def test_run_pipeline_produces_report_and_models(in_memory_db):
    db = in_memory_db
    _seed_predictive(db)
    rep = research.run_pipeline(db, build=False, slippage=0.01)
    assert rep["verdict_code"] in (1, 2, 3, 4)
    assert rep["fair_value"]["ok"] and rep["ensemble"]["ok"]
    assert "feature_discovery" in rep and "microstructure" in rep and "cross_market" in rep
    # models persisted to the leaderboard
    lb = research.model_leaderboard(db)
    assert any(m["name"] == "fair_value" for m in lb["models"])
    assert any(m["name"] == "ensemble" for m in lb["models"])
    # research stored on state + readable via status
    st = research.research_status(db)
    assert st["research"]["verdict_code"] == rep["verdict_code"]
    # PAPER-ONLY: nothing trades
    assert db.scalar(select(func.count()).select_from(LiveExecution)) == 0


def test_pipeline_data_insufficient_verdict(in_memory_db):
    db = in_memory_db
    # too few points / no quality flags -> verdict 4
    for i in range(6):
        _insert(db, split="train", label_up=True, mid=f"a{i}", btc_ret_sofar=0.001)
    db.commit()
    rep = research.run_pipeline(db, build=False)
    assert rep["verdict_code"] == 4 and "insufficient" in rep["verdict"]


def test_status_paper_only(in_memory_db):
    db = in_memory_db
    bank0 = live.get_state(db).bankroll
    st = research.research_status(db)
    assert "research/paper only" in st["safety"]
    assert live.get_state(db).bankroll == bank0


# --- wallet-as-feature (in the lab dataset builder) -------------------------
def test_wallet_features_uses_profitable_flow():
    trades = [NS(seconds_from_creation=s, side="buy", direction="YES", usd_value=50,
                 wallet_address="0xGOOD") for s in range(5, 40, 5)]
    trades += [NS(seconds_from_creation=s, side="buy", direction="NO", usd_value=10,
                  wallet_address="0xBAD") for s in range(5, 40, 5)]
    profitable = {"0xgood"}                                 # only the good wallet counts
    wf = lab.wallet_features(trades, 60, profitable)
    assert wf["wallet_present"] == 1 and wf["wallet_signal"] > 0   # net profitable flow = YES
    # no profitable wallets -> neutral signal
    assert lab.wallet_features(trades, 60, set())["wallet_signal"] == 0.0
