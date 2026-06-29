"""BTC 5M Alpha Discovery Engine (Phase 2) tests: statistical primitives
(Spearman IC, mutual information, permutation importance), candidate generation,
feature mining + survival filter, the persistent registry with generational
tracking (new/gained/lost), meta-learning lifecycle + strict promotion gate,
multi-asset cross-market lead detection, the nightly generation + report, and
paper-only isolation (no LiveExecution / bankroll / orders).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from app import btc5m_alpha_discovery as disc
from app import btc5m_alpha_research as ph1
from app import btc5m_strategy_lab as lab
from app import btc5m_models as bm
from app import btc5m_strategy_models as lm
from app import live
from app.models import LiveExecution


# --- statistical primitives -------------------------------------------------
def test_spearman_and_mutual_information():
    xs = list(range(16))
    ys = [0 if i < 8 else 1 for i in range(16)]   # cleanly separable by x
    assert disc.spearman_ic(xs, ys) > 0.5
    assert disc.mutual_information(xs, ys) > 0
    # random / constant -> ~0
    assert disc.spearman_ic([1, 1, 1, 1], [0, 1, 0, 1]) == 0.0
    assert disc.mutual_information([5.0] * 16, [i % 2 for i in range(16)]) == 0.0


def test_permutation_importance_ranks_informative_feature():
    # f1 perfectly separates the label; f2 is noise
    train = [{"_label": i % 2} for i in range(40)]
    ev = [{"_label": i % 2} for i in range(20)]
    vals = {"train": {"f1": [p["_label"] for p in train], "f2": [0.0] * 40},
            "val": {"f1": [p["_label"] for p in ev], "f2": [0.0] * 20}}
    imp = disc.permutation_importance(["f1", "f2"], vals, train, ev, eval_split="val")
    assert imp["f1"] >= imp["f2"]


# --- candidate generation ---------------------------------------------------
def test_generate_candidates_covers_categories():
    cands = disc.generate_candidates()
    assert len(cands) > 200
    cats = {c for _, c, _ in cands}
    for need in ("nonlinear", "interaction", "regime", "wallet", "whale",
                 "multi_timeframe", "acceleration", "time_to_expiry", "cross_market"):
        assert need in cats, need
    # extractors are callable over a point dict
    name, cat, fn = cands[0]
    assert isinstance(fn({"btc_ret_sofar": 0.001}), float)


# --- dataset seeding (predictive) -------------------------------------------
def _noise(i, seed):
    """Deterministic pseudo-noise in [-0.5, 0.5] (varies per feature via `seed`)."""
    return ((i * 9301 + seed * 49297 + 233) % 233280) / 233280.0 - 0.5


def _seed(db, n=26, months=("2026-05", "2026-06")):
    # Several INDEPENDENT predictive signals (BTC / flow / wallet / lag each carry the
    # label plus their own noise) so multiple non-redundant features survive pruning.
    for split in ("train", "val", "holdout"):
        for i in range(n):
            up = i % 2 == 0
            s = 1.0 if up else -1.0
            mid = f"{split}{i}"
            mo = months[i % len(months)]
            created = datetime(int(mo[:4]), int(mo[5:7]), 1 + (i % 20), 12, 0, 0)
            db.add(bm.Btc5mMarket(market_id=mid, slug="btc-updown-5m", question="Bitcoin Up or Down",
                                  created_time=created, resolution_time=created + timedelta(minutes=5),
                                  resolved=True, final_outcome="Up" if up else "Down"))
            br = 0.0015 * s + 0.0008 * _noise(i, 1)
            flow = 0.6 * s + 0.4 * _noise(i, 2)
            wal = 0.5 * s + 0.4 * _noise(i, 3)
            lg = 0.2 * s + 0.15 * _noise(i, 4)
            db.add(lm.Btc5mLabPoint(
                market_id=mid, duration_minutes=5, t_offset_s=60, secs_to_expiry=240,
                regime=("high_vol" if i % 2 else "chop"),
                features={"btc_ret_sofar": br, "btc_ret_1s": br, "btc_ret_2s": br, "btc_ret_3s": br,
                          "btc_ret_5s": br, "btc_ret_10s": br, "btc_ret_20s": br, "btc_ret_30s": br,
                          "btc_ret_60s": br, "btc_momentum": br * 100, "btc_acceleration": 0.0001 * _noise(i, 5),
                          "btc_vol": 0.0006, "btc_breakout": 1, "flow_imbalance": flow,
                          "recent_flow_imbalance": flow * 0.9, "pm_momentum": 0.01 * _noise(i, 6),
                          "lag": lg, "wallet_signal": wal,
                          "wallet_recent_signal": wal * 0.8, "wallet_trade_count": 3, "trade_freq": 0.2,
                          "volume_usd": 200, "has_large_trade": 1, "large_trade_usd": 60, "pm_yes": 0.5},
                pm_yes=0.5, spread=0.0, btc_ret_30s=br, flow_imbalance=flow,
                label_up=up, split=split))
    db.commit()
    st = lab._state(db)
    st.btc_resolution_s = 1
    st.btc_coverage_pct = 98.0
    st.points_built = db.scalar(select(func.count()).select_from(lm.Btc5mLabPoint)) or 0
    st.lag_profile = {"0": 0.0, "1": 0.03, "2": 0.05, "3": 0.07, "4": 0.06}
    db.commit()


# --- feature mining ---------------------------------------------------------
def test_mine_features_scores_and_survives(in_memory_db):
    db = in_memory_db
    _seed(db)
    mined = disc.mine_features(db)
    assert mined["ok"] and mined["generated"] > 200
    assert mined["survived"] >= 1
    s = mined["survivors"][0]
    for key in ("ic", "ic_pearson", "mutual_info", "stability_splits", "stability_regime",
                "stability_month", "redundancy", "decay", "shap_importance"):
        assert key in s
    assert abs(s["ic"]) >= disc.MIN_IC and s["stability_splits"] >= disc.MIN_STABILITY


def test_mine_features_too_small(in_memory_db):
    db = in_memory_db
    assert disc.mine_features(db)["ok"] is False


# --- registry + generational tracking ---------------------------------------
def test_registry_tracks_generations(in_memory_db):
    db = in_memory_db
    _seed(db)
    mined = disc.mine_features(db)
    diff1 = disc._update_registry(db, mined["survivors"], generation=1)
    assert diff1["new_alpha"]                       # first gen: all new
    reg = disc.feature_registry(db)
    assert reg["n_active"] >= 1 and reg["active"][0]["status"] == "active"
    assert reg["active"][0]["history"][0]["gen"] == 1
    # second generation: same survivors -> not new; history grows
    diff2 = disc._update_registry(db, mined["survivors"], generation=2)
    assert diff2["new_alpha"] == []
    reg2 = disc.feature_registry(db)
    assert len(reg2["active"][0]["history"]) == 2


def test_registry_marks_lost_power(in_memory_db):
    db = in_memory_db
    _seed(db)
    mined = disc.mine_features(db)
    disc._update_registry(db, mined["survivors"], generation=1)
    # gen 2 with NO survivors -> previously active features become decayed (lost power)
    diff = disc._update_registry(db, [], generation=2)
    assert diff["lost_power"]
    reg = disc.feature_registry(db)
    assert reg["n_active"] == 0


# --- meta-learning lifecycle + promotion gate -------------------------------
def test_promotion_gate_rules():
    good = {"significant": True, "ev_after_cost": 0.05, "n_trades": 20,
            "regime_stability": 0.8, "decay": 0.01}
    state, _ = disc._promotion_decision(good)
    assert state == "paper"
    bad = {"significant": False, "ev_after_cost": -0.1, "n_trades": 3,
           "regime_stability": 0.2, "decay": 0.2}
    state2, reason = disc._promotion_decision(bad)
    assert state2 == "candidate" and "significant" in reason


def test_meta_learn_persists_generation(in_memory_db):
    db = in_memory_db
    _seed(db)
    mined = disc.mine_features(db)
    meta = disc.meta_learn(db, mined, generation=1)
    assert meta["ok"] and meta["lifecycle_state"] in ("paper", "candidate")
    mg = disc.model_generations(db)
    assert mg["generations"] and mg["generations"][0]["generation"] == 1
    assert mg["generations"][0]["lifecycle_state"] == meta["lifecycle_state"]


# --- cross-market multi-asset -----------------------------------------------
def test_cross_market_assets_injected(in_memory_db):
    db = in_memory_db
    _seed(db)
    # injected ETH/SOL series that lead the YES move (rising over the window)
    def fetch(label, start, end):
        secs = int((end - start).total_seconds())
        return [(t, 3000 * (1 + 0.001 * t / secs)) for t in range(0, secs + 1, 5)]
    out = disc.cross_market_assets(db, sample_markets=4, fetch_fn=fetch)
    assert out["ok"] and "ETH" in out["assets"] and "SOL" in out["assets"]
    assert "funding rates" in str(out["data_gaps"])


# --- nightly generation + report + isolation --------------------------------
def test_run_discovery_report_and_state(in_memory_db):
    db = in_memory_db
    _seed(db)
    rep = disc.run_discovery(db, cross_assets=False)
    assert rep["ok"] and rep["generation"] == 1
    assert rep["verdict_code"] in (1, 2, 3)
    assert "mining" in rep and rep["mining"]["survived"] >= 1
    assert "new_alpha" in rep and "top_features" in rep and "promotion_rules" in rep
    assert "raw L2 order book" in str(rep["data_gaps"])
    # state persisted + generation incremented
    st = disc.discovery_status(db)
    assert st["generation"] == 1 and st["alpha_research"]["generation"] == 1
    # second run -> generation 2
    rep2 = disc.run_discovery(db, cross_assets=False)
    assert rep2["generation"] == 2
    # PAPER-ONLY: nothing trades
    assert db.scalar(select(func.count()).select_from(LiveExecution)) == 0


def test_run_nightly_combines_phases(in_memory_db):
    db = in_memory_db
    _seed(db)
    out = disc.run_nightly(db, build=False, cross_assets=False)
    assert "phase1" in out and "alpha_discovery" in out
    assert out["alpha_discovery"]["generation"] == 1
    assert db.scalar(select(func.count()).select_from(LiveExecution)) == 0


def test_discovery_paper_only(in_memory_db):
    db = in_memory_db
    bank0 = live.get_state(db).bankroll
    st = disc.discovery_status(db)
    assert "research/paper only" in st["safety"]
    assert live.get_state(db).bankroll == bank0
