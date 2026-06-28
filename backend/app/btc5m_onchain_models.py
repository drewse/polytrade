"""ORM tables for BTC 5M Micro-Test V3 Phase 1 — on-chain OrderFilled detection
+ latency measurement. PAPER-ONLY: these tables record detected wallet fills and
their latency/price-drift, and NOTHING here ever places an order, touches
LiveExecution/LiveState, production copy trading, sizing, or bankroll.

NEW tables only (created by Base.metadata.create_all). Importing this module
registers them; main.py imports it so they are created at startup.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _utcnow() -> datetime:
    return datetime.utcnow()


class Btc5mOnchainSignal(Base):
    """One detected on-chain OrderFilled involving a watched micro-test wallet, with
    latency + price-drift measurement. Paper-only record — never an order."""
    __tablename__ = "btc5m_onchain_signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    source: Mapped[str] = mapped_column(String(20), default="onchain_ws")

    # on-chain provenance (dedup key = tx_hash + log_index)
    tx_hash: Mapped[str] = mapped_column(String(80), index=True)
    log_index: Mapped[int] = mapped_column(Integer, default=0)
    block_number: Mapped[int] = mapped_column(BigInteger, index=True)
    block_timestamp: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    exchange_address: Mapped[str] = mapped_column(String(64), default="")

    # decoded fill
    watched_wallet: Mapped[str] = mapped_column(String(64), index=True)
    wallet_role: Mapped[str] = mapped_column(String(8), default="maker")   # maker | taker
    market_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    condition_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    token_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    question: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(40), nullable=True)
    direction: Mapped[str | None] = mapped_column(String(8), nullable=True)  # YES | NO
    side: Mapped[str] = mapped_column(String(8), default="buy")              # buy | sell
    price: Mapped[float | None] = mapped_column(Float, nullable=True)        # wallet fill price
    shares: Mapped[float | None] = mapped_column(Float, nullable=True)
    usd_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seconds_until_expiry: Mapped[float | None] = mapped_column(Float, nullable=True)

    # measurement
    detection_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_price_at_detection: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_drift: Mapped[float | None] = mapped_column(Float, nullable=True)   # detected - wallet
    missed_edge: Mapped[float | None] = mapped_column(Float, nullable=True)

    # gate simulation (would the micro-test have acted?) — paper only
    would_pass_gates: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ignored_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class Btc5mOnchainState(Base):
    """Singleton detector state (id=1): cursor + connection status. Holds NO money
    state — purely the measurement detector's bookkeeping."""
    __tablename__ = "btc5m_onchain_state"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    running: Mapped[bool] = mapped_column(Boolean, default=False)
    rpc_connected: Mapped[bool] = mapped_column(Boolean, default=False)
    last_processed_block: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    token_map_size: Mapped[int] = mapped_column(Integer, default=0)
    signals_captured: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
