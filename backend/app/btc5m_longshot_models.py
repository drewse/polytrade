"""ORM table for the BTC 5M Longshot/Value Lab — research/paper ONLY.

Caches the result of the decisive experiment: does systematically buying the CHEAP
side (favorite-longshot bias / value market-making) have positive EV in our own
data — the strategy the 12 profitable wallets actually run? New table only
(create_all). Never read by live trading; never places orders.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Btc5mLongshotState(Base):
    __tablename__ = "btc5m_longshot_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    built_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Phase-0 favorite/under-reaction backtest result (separate research module, same table)
    favorite_report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    favorite_built_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
