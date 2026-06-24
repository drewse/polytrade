from app import scoring
from tests.conftest import make_trade


def test_empty_history_is_insufficient_data():
    res = scoring.score_wallet([])
    assert res.classification == "insufficient_data"
    assert res.score == 0.0


def test_tiny_sample_not_overrated():
    # 3 perfect wins but below MIN_TRADES_FOR_SCORE -> insufficient_data.
    trades = [make_trade(1, f"m{i}", "Yes", 0.4, 100, realized_pnl=150) for i in range(3)]
    res = scoring.score_wallet(trades)
    assert res.num_trades == 3
    assert res.classification == "insufficient_data"


def test_consistent_winner_scores_high_and_sharp():
    trades = [make_trade(1, f"m{i}", "Yes", 0.4, 100, realized_pnl=150) for i in range(40)]
    res = scoring.score_wallet(trades)
    assert res.win_rate == 1.0
    assert res.realized_roi > 0
    assert res.score >= 65
    assert res.classification == "sharp"


def test_consistent_loser_is_bad():
    trades = [make_trade(2, f"m{i}", "No", 0.6, 100, realized_pnl=-100) for i in range(40)]
    res = scoring.score_wallet(trades)
    assert res.win_rate == 0.0
    assert res.realized_roi < 0
    assert res.classification == "bad"


def test_score_is_bounded_0_100():
    trades = [make_trade(3, f"m{i}", "Yes", 0.2, 500, realized_pnl=2000) for i in range(80)]
    res = scoring.score_wallet(trades)
    assert 0.0 <= res.score <= 100.0


def test_category_performance_tracked():
    trades = (
        [make_trade(4, f"a{i}", "Yes", 0.4, 100, realized_pnl=150, category="Crypto") for i in range(20)]
        + [make_trade(4, f"b{i}", "No", 0.5, 100, realized_pnl=-100, category="Sports") for i in range(20)]
    )
    res = scoring.score_wallet(trades)
    assert res.category_performance["Crypto"] > 0
    assert res.category_performance["Sports"] < 0
