"""Shared test fixtures: lightweight stand-in objects + an in-memory DB."""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest


def make_trade(wallet_id, market_id, outcome, price, size, days_ago=10, side="buy",
               realized_pnl=0.0, category="Politics"):
    """A duck-typed trade usable by scoring/backtest (no DB needed)."""
    return SimpleNamespace(
        wallet_id=wallet_id, market_id=market_id, outcome=outcome, side=side,
        price=price, size=size, realized_pnl=realized_pnl, category=category,
        timestamp=datetime(2026, 1, 1) - timedelta(days=days_ago),
        market=SimpleNamespace(category=category),
    )


def make_market(market_id, resolved=True, resolved_outcome="Yes", liquidity=5000.0,
                resolved_days_ago=1, outcomes=("Yes", "No")):
    return SimpleNamespace(
        id=market_id, resolved=resolved, resolved_outcome=resolved_outcome,
        liquidity=liquidity, outcomes=list(outcomes),
        resolved_at=datetime(2026, 1, 1) - timedelta(days=resolved_days_ago),
    )


@pytest.fixture
def in_memory_db():
    """A fresh in-memory SQLite session with all tables created."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import Base
    from app import models  # noqa: F401  register tables

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    db = Session()
    try:
        yield db
    finally:
        db.close()
