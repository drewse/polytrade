"""BTC 5M passive-maker validation tests: bootstrap P(EV>0), stability buckets,
walk-forward folds, failure analysis, sensitivity grid, the fixed-config run +
verdict, and paper-only isolation (no LiveExecution / bankroll / orders)."""
from __future__ import annotations

from sqlalchemy import func, select

from app import btc5m_maker_validation as mv
from app import btc5m_execution_lab as ex
from app import live
from app.models import LiveExecution
from tests.test_btc5m_execution_lab import _seed, _model
from app import btc5m_alpha_research as ph1


# --- bootstrap --------------------------------------------------------------
def test_bootstrap_prob_ev_positive():
    # clearly-positive fills -> P(EV>0) ~ 1; clearly-negative -> ~0
    pos = [{"pnl": 0.4, "spread_captured": 0.01} for _ in range(20)]
    neg = [{"pnl": -0.4, "spread_captured": 0.01} for _ in range(20)]
    bp = mv.phase_d_bootstrap(pos)
    bn = mv.phase_d_bootstrap(neg)
    assert bp["ok"] and bp["prob_true_ev_positive"] > 0.95
    assert bn["prob_true_ev_positive"] < 0.05
    assert "ci95" in bp["ev_per_fill"]


def test_bootstrap_too_few():
    assert mv.phase_d_bootstrap([{"pnl": 0.1, "spread_captured": 0.0}])["ok"] is False


# --- phase helpers on a built signal set ------------------------------------
def _sigs(db):
    sigs = []
    for s in ("train", "val", "holdout"):
        sigs += ex.build_signals(db, s, _model(db), ph1.ALL_FEATURES, max_future=None)
    return [s for s in sigs if s["duration_minutes"] in mv.DURS]


def test_signals_carry_temporal_fields(in_memory_db):
    db = in_memory_db
    _seed(db)
    sigs = _sigs(db)
    assert sigs and all("month" in s and "created_ts" in s and "day_type" in s for s in sigs)


def test_phase_b_and_c_structure(in_memory_db):
    db = in_memory_db
    _seed(db)
    sigs = _sigs(db)
    res = mv._simulate(sigs, mv.WIN)
    b = mv.phase_b_stability(sigs, res)
    assert "buckets" in b and "fraction_positive" in b and "duration" in b["buckets"]
    c = mv.phase_c_walkforward(sigs, res, folds=4)
    assert "folds" in c and "calendar_span_hours" in c


def test_phase_e_and_f_structure(in_memory_db):
    db = in_memory_db
    _seed(db)
    sigs = _sigs(db)
    res = mv._simulate(sigs, mv.WIN)
    e = mv.phase_e_failure(sigs, res)
    assert "loser_profile" in e and "winner_profile" in e
    f = mv.phase_f_sensitivity(sigs)
    assert f["timeout_policy_grid"] and "queue_robustness" in f
    assert {q["queue"] for q in f["queue_robustness"]} == set(ex.QUEUE_MODES)


# --- full run + verdict + isolation -----------------------------------------
def test_run_validation_and_isolation(in_memory_db):
    db = in_memory_db
    _seed(db)
    rep = mv.run_validation(db)
    assert rep["ok"] and rep["verdict_code"] in (1, 2, 3, 4)
    assert rep["fixed_config"] == mv.WIN
    for k in ("phase_b_stability", "phase_c_walkforward", "phase_d_bootstrap",
              "phase_e_failure", "phase_f_sensitivity", "answers"):
        assert k in rep
    a = rep["answers"]
    for k in ("1_is_edge_real", "2_prob_true_ev_positive", "3_confidence_after_expansion",
              "4_justifies_paper_maker"):
        assert k in a
    # persisted under execution state, nothing traded
    assert mv.validation_status(db)["validation"]["verdict_code"] == rep["verdict_code"]
    assert db.scalar(select(func.count()).select_from(LiveExecution)) == 0


def test_validation_paper_only(in_memory_db):
    db = in_memory_db
    bank0 = live.get_state(db).bankroll
    st = mv.validation_status(db)
    assert "research/paper only" in st["safety"]
    assert live.get_state(db).bankroll == bank0
