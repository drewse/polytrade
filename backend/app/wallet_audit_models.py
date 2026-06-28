"""ORM table for cached PUBLIC Polymarket profile stats.

Stored SEPARATELY from internal ranking stats (WalletStat) and NEVER used to alter
ranking/eligibility — this is audit/visibility only. Cached + timestamped so the
audit dashboard doesn't hit the public APIs on every page load. NEW table only
(created by Base.metadata.create_all). main.py imports this module so it is
created at startup.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _utcnow() -> datetime:
    return datetime.utcnow()


class PublicWalletProfile(Base):
    """Cached public Polymarket profile stats for one wallet (audit only)."""
    __tablename__ = "public_wallet_profiles"

    address: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    pseudonym: Mapped[str | None] = mapped_column(String(120), nullable=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    profile_image: Mapped[str | None] = mapped_column(Text, nullable=True)
    # public P/L by window (USD)
    pnl_all: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_1d: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_7d: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_30d: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_all: Mapped[float | None] = mapped_column(Float, nullable=True)
    position_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    predictions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    biggest_win: Mapped[float | None] = mapped_column(Float, nullable=True)
    biggest_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    largest_position_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    top_positions: Mapped[list] = mapped_column(JSON, default=list)   # [{title, size, cashPnl}]
    # fetch bookkeeping
    fetch_status: Mapped[str] = mapped_column(String(12), default="ok")   # ok | partial | error
    fetch_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
