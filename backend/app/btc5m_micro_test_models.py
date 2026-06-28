"""ORM tables for the BTC 5M Micro-Test Mode — a fully isolated, opt-in live
micro-test layer.

These are NEW tables only (created by Base.metadata.create_all). They share the
declarative Base but reference NO production tables by foreign key and are NEVER
read or written by general live copy trading, ranking, discovery, sizing,
bankroll/accounting, or settlement. Micro-test trades live ONLY here — they are
deliberately kept out of the LiveExecution table so production accounting
(`live.settle_live`, bankroll, open-position counts) can never see or settle
them. Importing this module registers the tables; main.py imports it so they are
created at startup.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

STRATEGY_MODE = "btc5m_micro_test"


def _utcnow() -> datetime:
    return datetime.utcnow()


class Btc5mMicroTestTrade(Base):
    """One BTC 5M micro-test order attempt (filled / rejected / closed). Tagged
    with strategy_mode='btc5m_micro_test' and stored in its OWN table so it is
    always distinguishable from, and invisible to, normal copy trades."""
    __tablename__ = "btc5m_micro_test_trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    strategy_mode: Mapped[str] = mapped_column(String(32), default=STRATEGY_MODE, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    executor: Mapped[str] = mapped_column(String(16), default="paper")  # paper | dry_run | polymarket

    market_id: Mapped[str] = mapped_column(String(120), index=True)
    market_question: Mapped[str] = mapped_column(Text, default="")
    outcome: Mapped[str] = mapped_column(String(32), default="")        # the market outcome bought
    direction: Mapped[str] = mapped_column(String(8), default="")       # YES | NO (normalized)
    side: Mapped[str] = mapped_column(String(8), default="buy")
    wallet_triggered: Mapped[str] = mapped_column(String(64), default="", index=True)
    wallet_role: Mapped[str] = mapped_column(String(12), default="primary")  # primary | backup
    regime: Mapped[str | None] = mapped_column(String(40), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    reference_price: Mapped[float] = mapped_column(Float, default=0.0)
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    shares: Mapped[float] = mapped_column(Float, default=0.0)
    size_usd: Mapped[float] = mapped_column(Float, default=0.0)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    slippage: Mapped[float | None] = mapped_column(Float, nullable=True)

    status: Mapped[str] = mapped_column(String(12), default="open", index=True)  # open | closed | rejected
    fill_outcome: Mapped[str | None] = mapped_column(String(40), nullable=True)
    order_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    tick_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_order_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    venue_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    entry_reason: Mapped[str] = mapped_column(Text, default="")
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    won: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # paper twin (for paper-vs-live comparison): what a $-stake paper fill at the
    # reference price would have returned, recorded alongside the live attempt.
    paper_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    paper_realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)


class Btc5mMicroTestState(Base):
    """Singleton arm/stop latch for the micro-test (id=1). Default disarmed.
    A stop latch requires a manual re-arm. Holds ONLY micro-test control state —
    never touches LiveState / production bankroll."""
    __tablename__ = "btc5m_micro_test_state"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    armed: Mapped[bool] = mapped_column(Boolean, default=False)
    stopped: Mapped[bool] = mapped_column(Boolean, default=False)
    stop_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    armed_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    armed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_signal: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_rejection: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
