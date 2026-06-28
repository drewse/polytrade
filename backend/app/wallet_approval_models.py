"""ORM tables for the Deep Backfill + Manual Wallet Approval system.

NEW tables only (created by Base.metadata.create_all). They govern wallet DATA
QUALITY (backfill coverage) and MANUAL approval/disable controls. They never touch
execution, routing, sizing, bankroll, slippage, or open positions. main.py imports
this module so the tables are created at startup.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _utcnow() -> datetime:
    return datetime.utcnow()


# Manual approval lifecycle
APPROVAL_STATUSES = ("none", "approved", "rejected", "watchlist")


class WalletApproval(Base):
    """Manual operator controls for one wallet. `manually_disabled` is a HARD
    override — a disabled wallet is never copied even if it ranks #1. Approval is a
    positive marker that still requires the normal safety gates to pass."""
    __tablename__ = "wallet_approvals"

    address: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(12), default="none", index=True)  # none|approved|rejected|watchlist
    manually_approved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    manually_disabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    approved_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    disabled_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class WalletBackfillProgress(Base):
    """Deep-backfill coverage + resumable cursor for one wallet."""
    __tablename__ = "wallet_backfill_progress"

    address: Mapped[str] = mapped_column(String(64), primary_key=True)
    cursor_offset: Mapped[int] = mapped_column(Integer, default=0)       # next data-api /trades offset
    pages_fetched: Mapped[int] = mapped_column(Integer, default=0)
    exhausted: Mapped[bool] = mapped_column(Boolean, default=False)      # reached end of available history
    internal_volume: Mapped[float] = mapped_column(Float, default=0.0)
    internal_trades: Mapped[int] = mapped_column(Integer, default=0)
    internal_markets: Mapped[int] = mapped_column(Integer, default=0)
    public_volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    public_predictions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    coverage_volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    coverage_trades: Mapped[float | None] = mapped_column(Float, nullable=True)
    coverage_ratio: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    coverage_grade: Mapped[str] = mapped_column(String(10), default="unknown", index=True)  # unknown|low|medium|high|complete
    partial_history: Mapped[bool] = mapped_column(Boolean, default=True)
    first_public_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    first_internal_trade: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_internal_trade: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)  # pending|running|completed|failed
    priority: Mapped[int] = mapped_column(Integer, default=0, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested: Mapped[bool] = mapped_column(Boolean, default=False)      # operator asked for a deeper backfill
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
