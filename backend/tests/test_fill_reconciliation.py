"""Execution fill-reconciliation tests: VWAP of actual venue fills, parsing,
the place()-path resolver (venue vs pending), historical repair (open + closed
with bankroll adjustment), no-venue-data -> pending (never fabricated), and
delayed reconciliation by the worker pass."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app import live
from app.models import LiveExecution, LiveState, Market


# --- VWAP + parsing ---------------------------------------------------------
def test_vwap_exact_better_and_multi():
    assert live._vwap_fills([{"price": 0.42, "size": 10, "fee": 0}])["avg_price"] == 0.42
    better = live._vwap_fills([{"price": 0.01, "size": 9.52, "fee": 0}])
    assert better["avg_price"] == 0.01 and round(better["cost"], 4) == 0.0952
    multi = live._vwap_fills([{"price": 0.01, "size": 5, "fee": 0}, {"price": 0.03, "size": 5, "fee": 0}])
    assert multi["avg_price"] == 0.02 and multi["quantity"] == 10 and multi["n_fills"] == 2
    assert live._vwap_fills([]) is None


def test_parse_venue_trades_shapes_and_filter():
    raw = {"data": [
        {"price": "0.10", "size": "4", "asset_id": "tokA"},
        {"price": "0.20", "size": "6", "asset_id": "tokB"},   # filtered out by token
        {"price": "x", "size": "2", "asset_id": "tokA"},       # unparseable -> skipped
    ]}
    fills = live._parse_venue_trades(raw, token_id="tokA")
    assert len(fills) == 1 and fills[0]["price"] == 0.10 and fills[0]["size"] == 4


# --- place() resolver: venue VWAP vs pending fallback -----------------------
class _TradesClient:
    def __init__(self, trades):
        self._t = trades

    def get_trades(self, *a, **k):
        return self._t


class _NoTradesClient:
    pass


def test_resolve_fill_uses_venue_vwap():
    client = _TradesClient([{"price": 0.01, "size": 9.52}])   # filled far better than the 0.42 limit
    fp, fusd, fsh, fee, src, pend = live._resolve_fill(
        client, order_id="o1", token_id="tk", filled_shares=9.52, limit_price=0.42)
    assert src == "venue" and pend is False
    assert fp == 0.01 and fusd == 0.1 and fsh == 9.52        # real cost ~$0.10, NOT $4.00


def test_resolve_fill_pending_when_no_venue_data(monkeypatch):
    monkeypatch.setenv("LIVE_FILL_RECON_RETRIES", "1")
    fp, fusd, fsh, fee, src, pend = live._resolve_fill(
        _NoTradesClient(), order_id="o2", token_id="tk", filled_shares=9.52, limit_price=0.42)
    assert src == "pending" and pend is True
    # conservative UPPER bound (limit*shares) for exposure; not a claimed real fill
    assert fp == 0.42 and fusd == round(9.52 * 0.42, 2)


# --- historical repair ------------------------------------------------------
def _exec(db, **kw):
    base = dict(idempotency_key=f"k{kw.get('signal_id', 1)}", executor="polymarket", strategy_key="s",
                wallet_address="0xw", market_id="mJor", market_question="Jordan vs. Argentina O/U 1.5",
                outcome="Under", side="buy", expected_price=0.42, limit_price=0.42, fill_price=0.42,
                size_usd=4.0, shares=9.52, status="open", order_id="0xorder",
                fill_source="pending", bankroll_before=100.0)
    base.update(kw)
    e = LiveExecution(**base)
    db.add(e); db.flush()
    return e


def test_reconcile_open_position_corrects_cost_basis(in_memory_db):
    db = in_memory_db
    ex = _exec(db, signal_id=1)
    db.commit()
    out = live.reconcile_fills(db, fetch_fn=lambda e: [{"price": 0.01, "size": 9.52, "fee": 0.0}])
    assert out["corrected"] == 1
    db.refresh(ex)
    assert ex.fill_price == 0.01 and ex.size_usd == 0.1     # $4.00 -> ~$0.10 (exposure corrected)
    assert ex.fill_source == "venue" and ex.fill_pending_reconciliation is False
    assert ex.slippage < 0                                   # large FAVORABLE slippage now recorded


def test_reconcile_closed_position_fixes_pnl_and_bankroll(in_memory_db):
    db = in_memory_db
    db.add(LiveState(id=1, starting_bankroll=100.0, bankroll=100.0, halted=False))
    db.add(Market(id="mJor", question="Jordan", outcomes=["Under", "Over"], token_ids=["t1", "t2"],
                  prices=[0.0, 1.0], resolved=True, resolved_outcome="Over"))   # Under LOST
    ex = _exec(db, signal_id=2, status="closed", realized_pnl=-4.0)             # booked -$4 on the wrong cost
    db.commit()
    out = live.reconcile_fills(db, fetch_fn=lambda e: [{"price": 0.01, "size": 9.52, "fee": 0.0}])
    db.refresh(ex)
    # real loss is the actual ~$0.10 spend, not $4.00
    assert ex.size_usd == 0.1 and ex.realized_pnl == -0.1
    corr = out["corrections"][0]
    assert corr["old_realized_pnl"] == -4.0 and corr["new_realized_pnl"] == -0.1
    assert corr["bankroll_delta"] == 3.9                    # +$3.90 wrongly-booked loss returned
    assert live.get_state(db).bankroll == 103.9


def test_no_venue_data_marks_pending_not_fabricated(in_memory_db):
    db = in_memory_db
    ex = _exec(db, signal_id=3, fill_source=None)
    db.commit()
    out = live.reconcile_fills(db, fetch_fn=lambda e: [])    # venue returns nothing
    db.refresh(ex)
    assert out["corrected"] == 0 and out["marked_pending"] == 1
    assert ex.fill_pending_reconciliation is True
    assert ex.fill_price == 0.42 and ex.size_usd == 4.0     # UNCHANGED — no fabrication


def test_delayed_reconciliation_by_worker_pass(in_memory_db):
    db = in_memory_db
    ex = _exec(db, signal_id=4, fill_pending_reconciliation=True, fill_source="pending")
    db.commit()
    out = live.reconcile_pending(db, fetch_fn=lambda e: [{"price": 0.02, "size": 9.52, "fee": 0.0}])
    assert out["reconciled"] == 1
    db.refresh(ex)
    assert ex.fill_price == 0.02 and ex.fill_pending_reconciliation is False and ex.fill_source == "venue"


def test_partial_fill_vwap(in_memory_db):
    db = in_memory_db
    # only 4 of the 9.52 shares filled, at two prices -> VWAP over the 4 filled
    ex = _exec(db, signal_id=5, shares=4.0)
    db.commit()
    live.reconcile_fills(db, fetch_fn=lambda e: [{"price": 0.10, "size": 2}, {"price": 0.20, "size": 2}])
    db.refresh(ex)
    assert ex.shares == 4.0 and ex.fill_price == 0.15 and ex.size_usd == 0.6


def test_already_reconciled_rows_are_skipped(in_memory_db):
    db = in_memory_db
    ex = _exec(db, signal_id=6, fill_source="venue", fill_price=0.01, size_usd=0.1)
    db.commit()
    calls = []
    live.reconcile_fills(db, fetch_fn=lambda e: calls.append(e) or [{"price": 0.5, "size": 1}])
    assert calls == []                                       # venue-reconciled rows aren't re-fetched
    db.refresh(ex)
    assert ex.fill_price == 0.01                             # untouched
