"""ORM table for the DREW FINDS wallet-research tab — research/read-only.

Caches the reverse-engineered profiles of the two target wallets + the similar
BTC-5m wallets discovered from their co-traders. New table only (create_all). It
references NO production tables and is never read by live trading. The harness only
reads public Polymarket APIs + our indexed btc5m data — it never places orders.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Btc5mDrewFindsState(Base):
    __tablename__ = "btc5m_drew_finds_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    built_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
