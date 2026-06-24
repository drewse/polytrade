"""
Signal quality tracking.

For each signal we record how the market moved *after* the signal fired, in the
direction we predicted (a buy on outcome O expects O's price to rise):

  * move_5m / move_30m / move_2h : signed price move at each horizon
  * move_close                   : move to resolution (worth $1 or $0)
  * mfe / mae                    : max favorable / adverse excursion in the window

Two data paths:
  * `update_from_snapshots()` — the real thing, using MarketPriceSnapshot rows
    captured by the worker each cycle. Horizons only fill once enough time has
    passed, so fresh signals fill in gradually.
  * `synthesize()` — used at seed time so the Signals page shows plausible
    quality immediately (there is no real price history right after seeding).
    Synthetic values are only written to signals that have never been evaluated.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Market, MarketPriceSnapshot, PaperSignal

HORIZONS = {"move_5m": 5, "move_30m": 30, "move_2h": 120}


def _outcome_price(snapshot_price: float, market_outcomes: list, outcome: str) -> float:
    """Price of `outcome` given a snapshot that stored outcomes[0]'s price."""
    if market_outcomes and outcome == market_outcomes[0]:
        return snapshot_price
    return round(1 - snapshot_price, 4)  # binary complement


def update_from_snapshots(db: Session, lookback_hours: int = 8) -> int:
    """Fill quality fields for recent signals from captured price snapshots."""
    cutoff = datetime.utcnow() - timedelta(hours=lookback_hours)
    signals = db.scalars(select(PaperSignal).where(PaperSignal.created_at >= cutoff)).all()
    now = datetime.utcnow()
    updated = 0
    for sig in signals:
        market = db.get(Market, sig.market_id)
        if market is None:
            continue
        snaps = db.scalars(
            select(MarketPriceSnapshot)
            .where(MarketPriceSnapshot.market_id == sig.market_id)
            .where(MarketPriceSnapshot.timestamp >= sig.created_at)
            .order_by(MarketPriceSnapshot.timestamp)
        ).all()
        # build (offset_minutes, outcome_price) series
        series = [
            ((s.timestamp - sig.created_at).total_seconds() / 60.0,
             _outcome_price(s.price, market.outcomes, sig.outcome))
            for s in snaps
        ]
        changed = False
        for field_name, mins in HORIZONS.items():
            if getattr(sig, field_name) is not None:
                continue
            if (now - sig.created_at).total_seconds() / 60.0 < mins:
                continue  # horizon not reached yet
            # nearest snapshot to the horizon, within a tolerance window
            tol = max(2.0, mins * 0.5)
            candidates = [(abs(off - mins), price) for off, price in series if abs(off - mins) <= tol]
            if candidates:
                _, price = min(candidates, key=lambda c: c[0])
                setattr(sig, field_name, round(price - sig.observed_price, 4))
                changed = True
        if series:
            favorable = max(price - sig.observed_price for _, price in series)
            adverse = min(price - sig.observed_price for _, price in series)
            sig.mfe = round(max(sig.mfe or 0.0, favorable), 4)
            sig.mae = round(min(sig.mae if sig.mae is not None else 0.0, adverse), 4)
            changed = True
        if market.resolved and market.resolved_outcome is not None:
            final = 1.0 if sig.outcome == market.resolved_outcome else 0.0
            sig.move_close = round(final - sig.observed_price, 4)
            changed = True
        if changed:
            sig.quality_updated_at = now
            updated += 1
    db.commit()
    return updated


def synthesize(db: Session, rng) -> int:
    """Fill plausible quality for never-evaluated signals (seed-time only)."""
    signals = db.scalars(select(PaperSignal).where(PaperSignal.move_5m.is_(None))).all()
    updated = 0
    for sig in signals:
        market = db.get(Market, sig.market_id)
        if market is None:
            continue
        # Final direction: toward resolution if known, else a drift correlated
        # with signal confidence (good signals tend to move favorably).
        if market.resolved and market.resolved_outcome is not None:
            final = 1.0 if sig.outcome == market.resolved_outcome else 0.0
            target = final - sig.observed_price
        else:
            bias = (sig.confidence - 55.0) / 100.0  # -ish in [-0.5, 0.45]
            target = rng.gauss(bias * 0.12, 0.05)
        m5 = round(target * 0.2 + rng.gauss(0, 0.01), 4)
        m30 = round(target * 0.5 + rng.gauss(0, 0.015), 4)
        m2h = round(target * 0.8 + rng.gauss(0, 0.02), 4)
        sig.move_5m, sig.move_30m, sig.move_2h = m5, m30, m2h
        sig.mfe = round(max(0.0, m5, m30, m2h) + abs(rng.gauss(0, 0.01)), 4)
        sig.mae = round(min(0.0, m5, m30, m2h) - abs(rng.gauss(0, 0.01)), 4)
        if market.resolved and market.resolved_outcome is not None:
            sig.move_close = round((1.0 if sig.outcome == market.resolved_outcome else 0.0)
                                   - sig.observed_price, 4)
        sig.quality_updated_at = datetime.utcnow()
        updated += 1
    db.commit()
    return updated
