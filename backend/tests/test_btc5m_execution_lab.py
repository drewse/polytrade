"""BTC 5M Execution Research Lab (Phase 3) tests: per-trade significance, signal
generation, fill simulation from the historical trade stream (incl. adverse
selection + spread capture), execution methods (market/join/improve/passive/
adaptive/fair-value-maker), metric suite + breakdowns, fill-probability model,
execution frontier, the promotion experiment (re-gate under best passive, no
retrain), research answers, and paper-only isolation (no LiveExecution/bankroll).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace as NS

from sqlalchemy import func, select

from app import btc5m_execution_lab as ex
from app import btc5m_alpha_research as ph1
from app import btc5m_strategy_lab as lab
from app import btc5m_models as bm
from app import btc5m_strategy_models as lm
from app import btc5m_ml as ml
from app import live
from app.models import LiveExecution


# --- significance -----------------------------------------------------------
def test_significance_gate():
    good = ex.significance([0.49] * 6 + [-0.51] * 1, [0.5] * 7)
    assert good["ev_after_cost"] > 0 and good["t_stat"] > 0
    flat = ex.significance([0.5] * 3, [0.5] * 3)         # n < MIN_TRADES
    assert flat["significant"] is False


# --- execution methods (pure, on a hand-built signal) -----------------------
def _sig(side="YES", up=True, mid=0.5, half=0.03, future=None):
    return {"market_id": "m", "t": 60, "mid": mid, "half": half, "side": side, "model_prob": 0.7,
            "edge": 0.2, "up": up, "regime": "mixed", "secs_to_expiry": 200, "btc_vol": 0.0006,
            "volume_usd": 200, "flow_imbalance": 0.3, "btc_ret_sofar": 0.001,
            "future": future if future is not None else []}


def test_market_order_pays_spread():
    r = ex.simulate_method(_sig(), "market")
    assert r["filled"] and r["entry"] > 0.5            # paid ask (mid+half+slip)
    assert r["spread_captured"] < 0                    # negative => paid spread


def test_passive_fill_captures_spread_and_adverse_selection():
    # a later trade drops to the YES bid (0.5-0.03=0.47) -> fills at the bid
    filled = ex.simulate_method(_sig(future=[(1.0, 0.46)]), "join_bid", timeout=2.0)
    assert filled["filled"] and filled["entry"] < 0.5  # bought below mid (captured spread)
    assert filled["spread_captured"] > 0
    # no crossing trade within timeout -> missed fill
    missed = ex.simulate_method(_sig(future=[(3.0, 0.40)]), "join_bid", timeout=2.0)
    assert missed["filled"] is False and missed["pnl"] == 0.0


def test_improve_bid_fills_easier_than_join():
    fut = [(1.0, 0.475)]                                # crosses improve (0.48) not join (0.47)
    join = ex.simulate_method(_sig(future=fut), "join_bid", timeout=2.0)
    improve = ex.simulate_method(_sig(future=fut), "improve_bid", timeout=2.0)
    assert join["filled"] is False and improve["filled"] is True


def test_fair_value_maker_skips_low_edge():
    # model_prob 0.7, YES bid ~0.47 -> bid-edge 0.23 > 0 -> posts; raise threshold to skip
    s = _sig(future=[(1.0, 0.46)])
    posts = ex.simulate_method(s, "fair_value_maker", timeout=5.0, ev_threshold=0.0)
    skips = ex.simulate_method(s, "fair_value_maker", timeout=5.0, ev_threshold=0.5)
    assert posts["filled"] is True and skips["filled"] is False


# --- dataset + signals ------------------------------------------------------
def _seed(db, n=24):
    for split in ("train", "val", "holdout"):
        for i in range(n):
            up = i % 2 == 0
            s = 1.0 if up else -1.0
            mid = f"{split}{i}"
            created = datetime(2026, 6, 1 + (i % 20), 12, 0, 0)
            db.add(bm.Btc5mMarket(market_id=mid, slug="btc-updown-5m", question="Bitcoin Up or Down",
                                  created_time=created, resolution_time=created + timedelta(minutes=5),
                                  resolved=True, final_outcome="Up" if up else "Down"))
            # trades that drift toward the eventual outcome (so passive bids can fill)
            for sec in range(65, 130, 5):
                yp = 0.5 + 0.06 * s * ((sec - 60) / 70.0)
                db.add(bm.Btc5mTrade(market_id=mid, wallet_address="0xw", side="sell" if up else "buy",
                                     direction="YES", price=yp, shares=10, usd_value=40,
                                     timestamp=created + timedelta(seconds=sec),
                                     seconds_from_creation=sec, seconds_until_expiry=300 - sec, features={}))
            br = 0.0015 * s
            db.add(lm.Btc5mLabPoint(
                market_id=mid, duration_minutes=5, t_offset_s=60, secs_to_expiry=240,
                regime=("high_vol" if i % 2 else "chop"),
                features={"btc_ret_sofar": br, "btc_ret_1s": br, "btc_ret_3s": br, "btc_ret_5s": br,
                          "btc_ret_10s": br, "btc_ret_30s": br, "btc_momentum": br * 100, "btc_vol": 0.0006,
                          "flow_imbalance": 0.6 * s, "recent_flow_imbalance": 0.5 * s, "pm_momentum": 0.0,
                          "lag": 0.2 * s, "wallet_signal": 0.5 * s, "volume_usd": 200, "trade_freq": 0.2,
                          "has_large_trade": 0, "large_trade_usd": 0, "pm_yes": 0.5},
                pm_yes=0.5, spread=0.04, btc_ret_30s=br, flow_imbalance=0.6 * s, label_up=up, split=split))
    db.commit()


def _model(db):
    trX, trY, _ = ph1.feature_matrix(lab._point_dicts(db, "train"), ph1.ALL_FEATURES)
    return ml.LogisticRegression().fit(trX, trY)


def test_build_signals_carries_future_trades(in_memory_db):
    db = in_memory_db
    _seed(db)
    sigs = ex.build_signals(db, "holdout", _model(db), ph1.ALL_FEATURES)
    assert sigs and all("side" in s and "future" in s for s in sigs)
    assert any(s["future"] for s in sigs)              # subsequent trades attached


# --- metric suite + frontier ------------------------------------------------
def test_frontier_includes_all_policies(in_memory_db):
    db = in_memory_db
    _seed(db)
    sigs = ex.build_signals(db, "holdout", _model(db), ph1.ALL_FEATURES)
    fr = ex.execution_frontier(sigs)
    policies = {r["policy"] for r in fr["frontier"]}
    for need in ("market", "join_bid", "improve_bid", "passive_1s", "passive_5s", "adaptive", "fair_value_maker"):
        assert need in policies, need
    assert fr["best_policy"] is not None
    m = next(r for r in fr["frontier"] if r["policy"] == "market")
    assert "fill_rate" in m and "ev_after_cost" in m and "sharpe" in m and "max_drawdown" in m


def test_metrics_breakdowns(in_memory_db):
    db = in_memory_db
    _seed(db)
    sigs = ex.build_signals(db, "holdout", _model(db), ph1.ALL_FEATURES)
    res = ex._run_policy(sigs, "passive_2s", {"method": "join_bid", "timeout": 2.0})
    bd = ex._breakdowns(res)
    for axis in ("by_regime", "by_volatility", "by_liquidity", "by_market_age", "by_entry_price"):
        assert axis in bd
    m = ex._metrics(res)
    assert "opportunity_cost" in m and "missed_profitable_pnl" in m["opportunity_cost"]


# --- fill probability model -------------------------------------------------
def test_fill_probability_model(in_memory_db):
    db = in_memory_db
    _seed(db)
    sigs = []
    for sp in ("train", "val", "holdout"):
        sigs += ex.build_signals(db, sp, _model(db), ph1.ALL_FEATURES)
    fm = ex.fill_probability_model(sigs)
    assert fm["ok"]
    assert set(fm["empirical_fill_rate"]) == {"1.0", "2.0", "5.0"}
    assert "0.25" in fm["modelled_fill_rate"] and "0.5" in fm["modelled_fill_rate"]
    # modelled fill prob is monotonic in timeout
    mr = fm["modelled_fill_rate"]
    assert mr["0.25"] <= mr["1.0"] <= mr["5.0"]


# --- promotion experiment + full run ----------------------------------------
def test_promotion_experiment_same_gate(in_memory_db):
    db = in_memory_db
    _seed(db)
    promo = ex.promotion_experiment(db, "passive_2s", {"method": "join_bid", "timeout": 2.0})
    assert "models_tested" in promo and "models_flipped_to_paper" in promo
    for r in promo["results"]:
        if "skipped" not in r:
            assert "market" in r and "passive" in r and "flipped_to_paper" in r


def test_run_execution_lab_report_and_isolation(in_memory_db):
    db = in_memory_db
    _seed(db)
    rep = ex.run_execution_lab(db)
    assert rep["ok"] and rep["verdict_code"] in (1, 2, 3)
    assert "execution_frontier" in rep and "fill_probability" in rep
    assert "promotion_experiment" in rep and len(rep["research_answers"]) == 7
    assert "adverse selection" in str(rep["approximations"])
    # stored on state
    st = ex.execution_status(db)
    assert st["execution"]["verdict_code"] == rep["verdict_code"]
    # PAPER-ONLY: nothing trades
    assert db.scalar(select(func.count()).select_from(LiveExecution)) == 0


def test_too_small_dataset(in_memory_db):
    db = in_memory_db
    rep = ex.run_execution_lab(db)
    assert rep["ok"] is False


def test_execution_paper_only(in_memory_db):
    db = in_memory_db
    bank0 = live.get_state(db).bankroll
    st = ex.execution_status(db)
    assert "research/paper only" in st["safety"]
    assert live.get_state(db).bankroll == bank0


# --- rest-window sweep ------------------------------------------------------
def test_full_life_timeout_and_long_horizon(in_memory_db):
    db = in_memory_db
    _seed(db)
    # max_future=None captures trades for the whole market life (not just 5s)
    sigs = ex.build_signals(db, "holdout", _model(db), ph1.ALL_FEATURES, max_future=None)
    assert sigs and any(len(s["future"]) > 0 for s in sigs)
    assert all("duration_minutes" in s for s in sigs)
    # timeout=None rests for the full market life -> >= the fills of a 5s window
    f5 = sum(1 for s in sigs if ex.simulate_method(s, "join_bid", timeout=5)["filled"])
    ffull = sum(1 for s in sigs if ex.simulate_method(s, "join_bid", timeout=None)["filled"])
    assert ffull >= f5


def test_two_sided_matched_and_adverse():
    # both sides cross within window -> matched, neutral spread capture
    s = _sig(future=[(1.0, 0.46), (2.0, 0.54)], half=0.03)
    r = ex.simulate_method(s, "two_sided", timeout=5.0)
    assert r["filled"] and r["matched"] and r["pnl"] > 0   # captured 2h spread
    # only the bid side crosses -> one-sided (adverse) inventory, not matched
    one = ex.simulate_method(_sig(future=[(1.0, 0.46)], half=0.03), "two_sided", timeout=5.0)
    assert one["filled"] and not one["matched"]


def test_metrics_report_adverse_selection(in_memory_db):
    db = in_memory_db
    _seed(db)
    sigs = ex.build_signals(db, "holdout", _model(db), ph1.ALL_FEATURES, max_future=None)
    res = ex._run_policy(sigs, "join_bid", {"timeout": 60})
    m = ex._metrics(res)
    for k in ("uncond_win_rate", "filled_win_rate", "adverse_selection_cost", "matched_fills"):
        assert k in m


def test_rest_window_sweep_structure_and_verdict(in_memory_db):
    db = in_memory_db
    _seed(db)
    sw = ex.run_rest_window_sweep(db)
    assert sw["ok"]
    assert sw["verdict_code"] in (1, 2, 3, 4)
    # every policy × window × universe row present, with the key per-fill metrics
    assert len(sw["rows"]) >= len(ex.SWEEP_POLICIES) * len(ex.REST_WINDOWS)
    r = sw["rows"][0]
    for k in ("rest_window_s", "fills_per_day", "ev_per_fill", "ev_per_day",
              "adverse_selection_cost", "avg_concurrent_positions", "capital_required_usd", "significant"):
        assert k in r
    # the per-fill-EV-vs-window curve + the headline analysis fields
    assert sw["ev_vs_window_join_bid"] and "answers" in sw
    assert "window_max_total_ev_day" in sw["answers"]
    # persisted under the execution state, nothing traded
    assert ex.execution_status(db)["sweep"]["verdict_code"] == sw["verdict_code"]
    assert db.scalar(select(func.count()).select_from(LiveExecution)) == 0


def test_fills_per_day_uses_cadence(in_memory_db):
    db = in_memory_db
    _seed(db)
    sigs = ex.build_signals(db, "holdout", _model(db), ph1.ALL_FEATURES, max_future=None)
    res = [ex.simulate_method(s, "join_bid", timeout=None) for s in sigs]
    fpd = ex._fills_per_day(sigs, res)
    # all seeded markets are 5m (288/day); fills/day = 288 * fill_rate <= 288
    assert 0 <= fpd <= 288
