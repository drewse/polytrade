"""Tests for the TOP 20 quant research platform: sizing, probability, analytics
(Sharpe/Sortino/profit factor/drawdown/expectancy), exits, leaderboard, wallet
profiling, explainability, independent evaluation, and paper-only safety."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app import top20
from app.models import (
    Market,
    PaperSignal,
    Top20Strategy,
    Top20Trade,
    Wallet,
    WalletCandidate,
    WalletStat,
)
from app.top20 import analytics, exits, leaderboard, probability, sizing
from app.top20.sizing import SizingPolicy


# --- sizing -----------------------------------------------------------------
DEFAULT = SizingPolicy()  # kelly 0.25


def test_kelly_formula():
    r = sizing.size(DEFAULT, price=0.5, p=0.6, bankroll=10_000)
    assert abs(r.kelly_fraction - 0.20) < 1e-9
    assert r.stake is not None and r.stake > 0


def test_kelly_non_positive_skipped():
    r = sizing.size(DEFAULT, price=0.6, p=0.5, bankroll=10_000)
    assert r.stake is None and r.kelly_fraction <= 0 and r.shares == 0.0


def test_zero_price_no_division_by_zero():
    r = sizing.size(DEFAULT, price=0.0, p=0.5, bankroll=10_000)
    assert r.stake is not None and r.stake >= DEFAULT.min_bet


def test_caps_never_exceeded():
    r = sizing.size(DEFAULT, price=0.5, p=0.99, bankroll=10_000)
    assert r.stake is not None
    assert r.stake <= min(DEFAULT.max_bet, DEFAULT.max_position_pct * 10_000) + 1e-9


def test_market_exposure_cap_blocks_when_full():
    used = DEFAULT.max_market_exposure_pct * 10_000
    r = sizing.size(DEFAULT, price=0.5, p=0.7, bankroll=10_000, market_exposure_used=used)
    assert r.stake is None


def test_fixed_dollar_and_pct_modes():
    rd = sizing.size(SizingPolicy(mode="fixed_dollar", fixed_dollar=100), price=0.5, p=0.6, bankroll=10_000)
    assert rd.stake is not None and rd.stake <= 100 + 1e-9
    rp = sizing.size(SizingPolicy(mode="fixed_pct", fixed_pct=0.02), price=0.5, p=0.6, bankroll=10_000)
    # 2% of 10k = 200, capped at pos cap (min(250, 5%*10k=500)) = 200
    assert rp.stake is not None and abs(rp.stake - 200) < 1e-6


# --- probability ------------------------------------------------------------
def test_probability_clamped_01_99():
    hi = probability.estimate(probability.ProbFeatures(market_price=0.9, edge=0.5, num_settled=100))
    lo = probability.estimate(probability.ProbFeatures(market_price=0.1, edge=-0.5, num_settled=100))
    assert 0.01 <= lo <= hi <= 0.99


def test_probability_weights_sum_to_one():
    w = probability.weights()
    s = sum(v for k, v in w.items() if k != "trust_settled")
    assert abs(s - 1.0) < 1e-9


def test_probability_handles_missing_inputs():
    p = probability.estimate(probability.ProbFeatures(market_price=0.5))
    assert 0.01 <= p <= 0.99


# --- analytics --------------------------------------------------------------
def test_sharpe_and_sortino():
    rets = [0.1, -0.05, 0.2, 0.0, 0.15]
    assert analytics.sharpe(rets) > 0
    # sortino >= sharpe when downside deviation < total stdev
    assert analytics.sortino(rets) >= analytics.sharpe(rets)
    assert analytics.sharpe([0.1]) == 0.0  # <2 samples


def test_profit_factor():
    assert analytics.profit_factor([10, 20, -5]) == 30 / 5
    assert analytics.profit_factor([10, 20]) > 0   # no losses
    assert analytics.profit_factor([-5, -5]) == 0.0


def test_expectancy():
    assert analytics.expectancy([10, -5, 15]) == round(20 / 3, 4)


def test_max_drawdown():
    assert analytics.max_drawdown([100, 120, 90, 130]) == 0.25  # 120 -> 90
    assert analytics.max_drawdown([100, 110, 120]) == 0.0


def test_streaks():
    w, l = analytics.streaks([1, 2, -1, -1, -1, 3, 4])
    assert w == 2 and l == 3


def test_kelly_growth_rate_runs():
    g = analytics.kelly_growth_rate([100, -50, 200], 10_000)
    assert isinstance(g, float)


# --- exits ------------------------------------------------------------------
def test_exit_tp_sl():
    assert exits.decide("tp_sl", unrealized_return=0.6, holding_minutes=10).close
    assert exits.decide("tp_sl", unrealized_return=-0.5, holding_minutes=10).close
    assert not exits.decide("tp_sl", unrealized_return=0.1, holding_minutes=10).close


def test_exit_time_stop():
    assert exits.decide("time_stop", unrealized_return=0.0, holding_minutes=2000).close
    assert not exits.decide("time_stop", unrealized_return=0.0, holding_minutes=10).close


def test_exit_mirror_and_hold():
    assert exits.decide("mirror", unrealized_return=0, holding_minutes=10, wallet_exited=True).close
    assert not exits.decide("mirror", unrealized_return=0, holding_minutes=10, wallet_exited=False).close
    assert not exits.decide("hold", unrealized_return=5, holding_minutes=99999).close


# --- leaderboard ------------------------------------------------------------
def test_leaderboard_ranks_by_weighted_score():
    rows = [
        {"id": 1, "key": "a", "name": "A", "metrics": {
            "sharpe": 2.0, "profit_factor": 3.0, "max_drawdown": 0.05, "annualized_return": 0.5,
            "win_rate": 0.7, "consistency": 0.8, "closed_positions": 10}},
        {"id": 2, "key": "b", "name": "B", "metrics": {
            "sharpe": 0.2, "profit_factor": 1.1, "max_drawdown": 0.4, "annualized_return": 0.05,
            "win_rate": 0.45, "consistency": 0.2, "closed_positions": 10}},
    ]
    ranked = leaderboard.rank(rows)
    assert ranked[0]["key"] == "a" and ranked[0]["rank"] == 1
    assert ranked[0]["score"] > ranked[1]["score"]
    assert ranked[0]["strengths"] and ranked[0]["reason"]
    expl = leaderboard.explain_pair(ranked[0], ranked[1])
    assert "ranks above" in expl


def test_leaderboard_no_trades_scores_zero():
    rows = [{"id": 1, "key": "a", "name": "A", "metrics": {"closed_positions": 0}}]
    ranked = leaderboard.rank(rows)
    assert ranked[0]["score"] == 0.0 and not ranked[0]["has_trades"]


# --- DB: evaluation, settlement, profiling, explainability ------------------
def _seed_max_signal(db, *, price=0.5, conf=95.0, edge=0.12, liquidity=20_000.0,
                     classification="good_candidate", win_rate=0.8):
    w = Wallet(address="0xtopwallet", copy_enabled=True)
    db.add(w); db.flush()
    db.add(WalletStat(wallet_id=w.id, num_trades=300, score=80.0, win_rate=win_rate,
                      realized_roi=0.2, consistency=0.7, classification="sharp", num_settled=200))
    db.add(WalletCandidate(wallet_id=w.id, copyability_score=90.0, classification=classification))
    db.add(Market(id="0xmktA", question="Will the Lakers win the championship?",
                  outcomes=["Yes", "No"], prices=[price, 1 - price],
                  liquidity=liquidity, resolved=False))
    db.flush()
    sig = PaperSignal(wallet_id=w.id, market_id="0xmktA", outcome="Yes", side="buy",
                      observed_price=price, suggested_entry=price, confidence=conf,
                      edge_estimate=edge, reason="t", created_at=datetime.utcnow())
    db.add(sig); db.commit()
    return sig


def test_creates_exactly_20_strategies(in_memory_db):
    s = top20.ensure_strategies(in_memory_db)
    assert len(s) == 20 and len({x.key for x in s}) == 20
    assert len(top20.ensure_strategies(in_memory_db)) == 20  # idempotent


def test_each_strategy_independently_evaluates(in_memory_db):
    db = in_memory_db
    _seed_max_signal(db)
    summary = top20.evaluate_signals(db)
    assert summary["signals"] == 1
    trades = db.scalars(select(Top20Trade)).all()
    # a max signal is admitted by many philosophies; each enters at most once
    per = {}
    for t in trades:
        per[t.strategy_id] = per.get(t.strategy_id, 0) + 1
    assert all(c == 1 for c in per.values())
    assert len(trades) >= 10  # broad admission
    # explanation recorded
    assert all(t.explanation and "summary" in t.explanation for t in trades)


def test_duplicate_signal_not_entered_twice(in_memory_db):
    db = in_memory_db
    _seed_max_signal(db)
    top20.evaluate_signals(db)
    n1 = db.scalar(select(top20.func.count()).select_from(Top20Trade))
    top20.evaluate_signals(db)
    n2 = db.scalar(select(top20.func.count()).select_from(Top20Trade))
    assert n1 == n2


def test_settlement_and_metrics(in_memory_db):
    db = in_memory_db
    _seed_max_signal(db, price=0.5)
    top20.run_cycle(db)
    m = db.get(Market, "0xmktA")
    m.resolved = True; m.resolved_outcome = "Yes"; db.commit()
    top20.run_cycle(db)  # settles + recomputes metrics
    closed = db.scalars(select(Top20Trade).where(Top20Trade.status == "closed")).all()
    assert closed
    for t in closed:
        assert t.realized_pnl > 0 and t.holding_minutes is not None
    # metrics persisted on strategies
    strat = db.scalar(select(Top20Strategy).where(Top20Strategy.key == "top5"))
    assert "sharpe" in (strat.metrics or {}) and strat.metrics["closed_positions"] >= 1


def test_market_filter_excludes(in_memory_db):
    db = in_memory_db
    # a sports market -> politics/crypto strategies must not enter
    _seed_max_signal(db)  # question mentions Lakers championship -> Sports
    top20.evaluate_signals(db)
    entered = {db.get(Top20Strategy, t.strategy_id).key
               for t in db.scalars(select(Top20Trade)).all()}
    assert "politics" not in entered
    assert "crypto" not in entered


def test_explain_signal_returns_20_decisions(in_memory_db):
    db = in_memory_db
    sig = _seed_max_signal(db)
    out = top20.explain_signal(db, sig.id)
    assert out is not None
    assert len(out["decisions"]) == 20
    assert out["taken_by"] >= 1
    assert all(d["decision"] in ("TAKE", "SKIP") for d in out["decisions"])
    # every skip carries a reason
    assert all(d["reason"] for d in out["decisions"])


def test_wallet_profile(in_memory_db):
    db = in_memory_db
    w = Wallet(address="0xprofile", copy_enabled=True)
    db.add(w); db.flush()
    db.add(WalletStat(wallet_id=w.id, num_trades=20, score=70, win_rate=0.6,
                      realized_roi=0.15, consistency=0.6, num_settled=10))
    db.add(WalletCandidate(wallet_id=w.id, copyability_score=72, classification="good_candidate"))
    # one resolved winning market + a buy trade
    from app.models import Trade as T
    db.add(Market(id="0xm1", question="Will BTC hit $100k?", outcomes=["Yes", "No"],
                  prices=[1.0, 0.0], resolved=True, resolved_outcome="Yes"))
    db.flush()
    db.add(T(wallet_id=w.id, market_id="0xm1", outcome="Yes", side="buy", price=0.4,
             size=40.0, timestamp=datetime.utcnow() - timedelta(days=2)))
    db.commit()
    prof = top20.wallet_profile(db, "0xprofile")
    assert prof is not None
    assert prof["paper_only"] is True
    assert prof["num_settled"] == 1
    assert prof["category_breakdown"]
    assert "equity_curve" in prof and "sharpe" in prof


def test_api_returns_20_strategies_with_metrics(in_memory_db):
    from app import main
    payload = main.top20_strategies(db=in_memory_db)
    assert payload["paper_only"] is True
    assert len(payload["strategies"]) == 20
    metric_fields = ["sharpe", "sortino", "profit_factor", "expectancy", "max_drawdown",
                     "win_rate", "avg_win", "avg_loss", "total_return", "kelly_growth_rate",
                     "consecutive_wins", "consecutive_losses", "avg_holding_min",
                     "signal_acceptance", "avg_kelly_fraction", "avg_position_size"]
    for s in payload["strategies"]:
        assert s["paper_only"] is True
        for f in metric_fields:
            assert f in s


def test_portfolio_and_forward_test(in_memory_db):
    db = in_memory_db
    _seed_max_signal(db)
    top20.run_cycle(db)
    port = top20.portfolio(db)
    assert port["paper_only"] is True and "exposure_by_category" in port
    ft = top20.forward_test(db)
    assert ft["paper_only"] is True and len(ft["strategies"]) == 20
