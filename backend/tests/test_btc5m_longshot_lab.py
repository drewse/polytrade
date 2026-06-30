"""BTC 5M Longshot/Value Lab tests: signal construction (cheap side), calibration
detects mispricing, cheap-side backtest across mid/maker/taker, threshold gating,
the verdict on a known-mispriced vs efficient synthetic market, and read-only
isolation (no LiveExecution / bankroll touch)."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from app import btc5m_longshot_lab as ls
from app import btc5m_models as bm
from app import btc5m_strategy_models as lm
from app import live
from app.models import LiveExecution


def _seed_points(db, n, *, mispriced: bool, base=0.5):
    """n markets. mispriced=True: the cheap side wins MORE than priced (overreaction
    reversion); False: efficiently priced (cheap side wins exactly its price)."""
    created = datetime(2026, 6, 1, 12, 0, 0)
    for i in range(n):
        mid = i % 100  # deterministic spread of prices
        pm = 0.30 + 0.40 * (mid / 99)        # prices 0.30..0.70
        mk = f"m{i}"
        db.add(bm.Btc5mMarket(market_id=mk, slug="btc-updown-5m", question="Bitcoin Up or Down",
                              created_time=created + timedelta(minutes=5 * i),
                              resolution_time=created + timedelta(minutes=5 * i + 5),
                              resolved=True, final_outcome="Up"))
        # true up-probability: efficient => pm; mispriced => regress toward 0.5 (overreaction)
        true_p = pm if not mispriced else 0.5 + 0.5 * (pm - 0.5)   # halve the deviation from 0.5
        up = (i * 7919 % 1000) / 1000.0 < true_p                   # deterministic Bernoulli
        db.add(lm.Btc5mLabPoint(market_id=mk, duration_minutes=5, t_offset_s=60, secs_to_expiry=240,
            regime=("hi" if i % 2 else "lo"), features={"pm_yes": pm}, pm_yes=round(pm, 4),
            spread=0.02, btc_ret_30s=0.0, flow_imbalance=0.0, label_up=up, split="train"))
    db.commit()


# --- signals + calibration --------------------------------------------------
def test_signals_pick_cheap_side(in_memory_db):
    db = in_memory_db
    _seed_points(db, 60, mispriced=True)
    sigs = ls._signals(db)
    assert sigs
    for s in sigs:
        assert s["cheap_price"] <= 0.5 + 1e-9
        # cheap side is YES when pm<0.5 else NO
        assert s["side"] == ("YES" if s["pm_yes"] < 0.5 else "NO")


def test_calibration_detects_mispricing(in_memory_db):
    db = in_memory_db
    _seed_points(db, 400, mispriced=True)
    sigs = ls._signals(db)
    cal = ls.calibration_curve(sigs)
    # overreaction-reversion => slope < 1 and cheap side wins more than priced
    assert cal["calibration_slope"] < 1.0
    assert cal["cheap_side_edge_at_mid"] > 0


def test_calibration_efficient_no_edge(in_memory_db):
    db = in_memory_db
    _seed_points(db, 400, mispriced=False)
    cal = ls.calibration_curve(ls._signals(db))
    # efficiently priced => slope ~1 and edge ~0 (allow noise)
    assert cal["cheap_side_edge_at_mid"] < 0.03


# --- backtest ---------------------------------------------------------------
def test_backtest_mid_positive_when_mispriced(in_memory_db):
    db = in_memory_db
    _seed_points(db, 400, mispriced=True)
    sigs = ls._signals(db)
    mid = ls.backtest(sigs, execution="mid", max_entry=0.45)
    taker = ls.backtest(sigs, execution="taker", max_entry=0.45)
    assert mid["n"] > 0 and mid["ev_per_trade"] > 0
    assert taker["ev_per_trade"] < mid["ev_per_trade"]        # paying spread is worse
    assert mid["max_entry"] == 0.45 and mid["avg_entry_price"] <= 0.45


def test_threshold_gates_entries(in_memory_db):
    db = in_memory_db
    _seed_points(db, 400, mispriced=True)
    sigs = ls._signals(db)
    wide = ls.backtest(sigs, execution="mid", max_entry=0.50)
    tight = ls.backtest(sigs, execution="mid", max_entry=0.35)
    assert tight["n"] < wide["n"]                            # tighter threshold => fewer entries


# --- full run + verdict + isolation -----------------------------------------
def test_run_mispriced_verdict(in_memory_db):
    db = in_memory_db
    _seed_points(db, 500, mispriced=True)
    rep = ls.run(db)
    assert rep["ok"] and rep["verdict_code"] in (1, 2, 3)
    assert rep["calibration"]["cheap_side_edge_at_mid"] > 0
    assert "grid" in rep and len(rep["grid"]) == 15          # 3 exec × 5 thresholds
    st = ls.status(db)
    assert st["report"]["verdict_code"] == rep["verdict_code"]
    assert db.scalar(select(func.count()).select_from(LiveExecution)) == 0


def test_run_efficient_verdict(in_memory_db):
    db = in_memory_db
    _seed_points(db, 500, mispriced=False)
    rep = ls.run(db)
    assert rep["verdict_code"] == 4                          # no mispricing => not longshot bias


def test_paper_only(in_memory_db):
    db = in_memory_db
    _seed_points(db, 60, mispriced=True)
    bank0 = live.get_state(db).bankroll
    ls.run(db)
    assert live.get_state(db).bankroll == bank0
    assert "research/paper only" in ls.status(db)["safety"]
