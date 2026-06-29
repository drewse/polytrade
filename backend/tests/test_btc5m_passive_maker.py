"""BTC 5M Passive-Maker PAPER harness tests: disabled no-op, enabled creates PAPER
quotes only (no LiveExecution, bankroll/live state unchanged), 5s cancel window,
fills inferred from the trade stream, settlement updates paper PnL only, the
pre-registered gate (needs 100 fills + P(EV>0)>=0.95 + top-5 exclusion), and
fail-soft L2 snapshot capture.
"""
from __future__ import annotations

from sqlalchemy import func, select

from app import btc5m_passive_maker as harness
from app import btc5m_passive_maker_models as pmm
from app import live
from app.models import LiveExecution
from tests.test_btc5m_execution_lab import _seed


# --- disabled vs enabled ----------------------------------------------------
def test_disabled_harness_is_noop(in_memory_db, monkeypatch):
    db = in_memory_db
    _seed(db)
    monkeypatch.delenv("BTC_PASSIVE_MAKER_PAPER_ENABLED", raising=False)
    out = harness.run_once(db)                       # disabled by default
    assert out["ran"] is False and "disabled" in out["skipped"]
    assert db.scalar(select(func.count()).select_from(pmm.Btc5mPaperQuote)) == 0


def test_enabled_creates_paper_quotes_only(in_memory_db, monkeypatch):
    db = in_memory_db
    _seed(db)
    bank0 = live.get_state(db).bankroll
    monkeypatch.setenv("BTC_PASSIVE_MAKER_PAPER_ENABLED", "true")
    out = harness.run_once(db)
    assert out["ran"] and out["created"] > 0
    quotes = db.scalars(select(pmm.Btc5mPaperQuote)).all()
    assert quotes and all(q.quote_lifetime_s == 5.0 and q.queue_assumption == "worst" for q in quotes)
    # 5s cancel window encoded
    assert all(q.cancel_t_offset_s == q.quote_t_offset_s + 5 for q in quotes)
    # PAPER ONLY: no live execution, bankroll + live state untouched
    assert db.scalar(select(func.count()).select_from(LiveExecution)) == 0
    assert live.get_state(db).bankroll == bank0


def test_run_once_idempotent_per_market(in_memory_db, monkeypatch):
    db = in_memory_db
    _seed(db)
    monkeypatch.setenv("BTC_PASSIVE_MAKER_PAPER_ENABLED", "true")
    n1 = harness.run_once(db)["created"]
    n2 = harness.run_once(db)["created"]
    assert n1 > 0 and n2 == 0                        # no market quoted twice


def test_fills_use_trade_stream_and_settle(in_memory_db, monkeypatch):
    db = in_memory_db
    _seed(db)
    monkeypatch.setenv("BTC_PASSIVE_MAKER_PAPER_ENABLED", "true")
    harness.run_once(db)
    filled = db.scalars(select(pmm.Btc5mPaperQuote).where(pmm.Btc5mPaperQuote.filled.is_(True))).all()
    for q in filled:
        assert q.fill_evidence and "worst-queue" in q.fill_evidence
        assert q.settled and q.realized_pnl is not None and q.spread_captured is not None
    # expired quotes carry a reason and 0 paper PnL
    expired = db.scalars(select(pmm.Btc5mPaperQuote).where(pmm.Btc5mPaperQuote.status == "expired")).all()
    assert all(q.reason_not_filled and q.realized_pnl == 0.0 for q in expired)


# --- pre-registered gate ----------------------------------------------------
def _fills(n, pnl=0.3, weeks=2, regime="mixed"):
    out = []
    for i in range(n):
        out.append({"pnl": pnl, "spread_captured": 0.01, "won": pnl > 0,
                    "regime": regime if i % 3 else "other", "week": f"2026-W{10 + (i % weeks):02d}",
                    "resolved_up": True, "side": "YES"})
    return out


def test_gate_cannot_pass_before_100_fills():
    status, cond = harness.evaluate_gate(_fills(50, pnl=0.4))
    assert cond["min_100_fills"] is False
    assert status == "research_only_not_validated"


def test_gate_requires_high_prob_ev_positive():
    # 120 fills but NEAR-ZERO mean with high variance -> P(EV>0) should not reach 0.95
    import random
    rng = random.Random(1)
    noisy = [{"pnl": rng.choice([0.5, -0.5]), "spread_captured": 0.0, "won": True,
              "regime": "mixed", "week": f"2026-W{10 + i % 2:02d}", "resolved_up": True, "side": "YES"}
             for i in range(120)]
    status, cond = harness.evaluate_gate(noisy)
    assert cond["min_100_fills"] is True
    assert cond["prob_ev_positive_ge_0.95"] is False
    assert status in ("research_only_not_validated", "failed_validation")


def test_gate_top5_exclusion_kills_fluke():
    # mostly losers + 5 huge winners that fake a positive mean -> excluding top-5 flips negative
    fills = [{"pnl": -0.05, "spread_captured": 0.0, "won": False, "regime": "mixed",
              "week": f"2026-W{10 + i % 2:02d}", "resolved_up": False, "side": "YES"} for i in range(120)]
    for i in range(5):
        fills[i]["pnl"] = 5.0
    status, cond = harness.evaluate_gate(fills)
    assert cond["ev_positive_excluding_top5"] is False


def test_gate_passes_only_when_all_true(in_memory_db):
    # strong, consistent, multi-week, multi-regime, robust-to-top5 -> paper_validated
    fills = []
    for i in range(140):
        fills.append({"pnl": 0.25 + (0.02 if i % 2 else -0.02), "spread_captured": 0.01, "won": True,
                      "regime": ["a", "b", "c", "d"][i % 4], "week": f"2026-W{10 + i % 4:02d}",
                      "resolved_up": True, "side": "YES"})
    status, cond = harness.evaluate_gate(fills)
    assert all(cond.values()), cond
    assert status == "paper_validated"


# --- L2 snapshot fail-soft --------------------------------------------------
def test_l2_capture_fail_soft(in_memory_db):
    db = in_memory_db
    _seed(db)
    # resolved market has no live book -> error stored, no raise
    snap = harness.capture_book(db, "train0")
    assert snap.error is not None
    assert db.scalar(select(func.count()).select_from(pmm.Btc5mPaperBookSnapshot)) == 1


# --- status + isolation -----------------------------------------------------
def test_status_paper_only_and_recompute(in_memory_db, monkeypatch):
    db = in_memory_db
    _seed(db)
    monkeypatch.setenv("BTC_PASSIVE_MAKER_PAPER_ENABLED", "true")
    harness.run_once(db)
    st = harness.status(db)
    assert "research/paper only" in st["safety"]
    assert st["status"] in ("research_only_not_validated", "failed_validation", "paper_validated")
    assert "gate" in st and st["fills_target"] == 100
    assert st["config"]["timeout_s"] == 5 and st["config"]["queue"] == "worst"
    # quotes/fills read APIs
    assert "quotes" in harness.quotes(db) and "fills" in harness.fills(db)
    assert db.scalar(select(func.count()).select_from(LiveExecution)) == 0
