"""ORM tables for the BTC 5M LIVE MAKER execution-research trial.

⚠️ This is the ONLY part of the codebase that can touch real money — and only via
the executor, only when BTC5M_LIVE_MAKER_ENABLED=true AND a live session is armed.
These tables record a small, capped, maker-only execution experiment whose purpose
is DATA COLLECTION (latency / fill probability / adverse selection), not profit.

Fully isolated: new tables only (create_all), referencing NO production tables, never
read by live.py / services.py / copy-trading / bankroll. Secrets (private key) are
NEVER stored here.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _utcnow() -> datetime:
    return datetime.utcnow()


class Btc5mLiveMakerState(Base):
    """Singleton control + running totals. Default DISARMED; live arming requires the
    env master switch. The kill flag immediately stops everything."""
    __tablename__ = "btc5m_live_maker_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    armed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    mode: Mapped[str] = mapped_column(String(8), default="shadow")   # shadow | live
    kill: Mapped[bool] = mapped_column(Boolean, default=False)        # emergency stop
    armed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    arm_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    session_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # running exposure / pnl for the active session (USD)
    deployed_usd: Mapped[float] = mapped_column(Float, default=0.0)      # total notional ever posted (cumulative)
    open_exposure_usd: Mapped[float] = mapped_column(Float, default=0.0)  # currently-resting notional
    session_realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    last_cycle_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class Btc5mLiveMakerSession(Base):
    """One armed session (shadow or live) with its caps + outcome."""
    __tablename__ = "btc5m_live_maker_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    mode: Mapped[str] = mapped_column(String(8), default="shadow")
    caps: Mapped[dict] = mapped_column(JSON, default=dict)            # snapshot of limits
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | ended | killed
    end_reason: Mapped[str | None] = mapped_column(String(80), nullable=True)
    orders: Mapped[int] = mapped_column(Integer, default=0)
    fills: Mapped[int] = mapped_column(Integer, default=0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    fees_paid: Mapped[float] = mapped_column(Float, default=0.0)


class Btc5mLiveMakerOrder(Base):
    """One maker (limit) order, with every timestamp + latency + the post-fill mark-outs
    needed to measure adverse selection. In shadow mode the order is recorded but never
    sent to the exchange (status stays 'shadow')."""
    __tablename__ = "btc5m_live_maker_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(Integer, index=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)     # our idempotency id
    exchange_order_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    market_id: Mapped[str] = mapped_column(String(120))
    token_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(8), nullable=True)
    side: Mapped[str] = mapped_column(String(4), default="BUY")        # maker BUY only this phase
    price: Mapped[float] = mapped_column(Float)
    size_shares: Mapped[float] = mapped_column(Float)
    notional_usd: Mapped[float] = mapped_column(Float)
    mode: Mapped[str] = mapped_column(String(8), default="shadow")

    # lifecycle status: intended|shadow|submitted|acked|resting|partial|filled|cancelled|rejected|error
    status: Mapped[str] = mapped_column(String(16), default="intended", index=True)
    # event timestamps (wall clock, UTC)
    detected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    quote_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    submit_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ack_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    first_fill_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancel_req_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancel_ack_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # latencies (milliseconds, from monotonic clocks)
    submit_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    ack_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    fill_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    cancel_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    queue_lifetime_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    # fills + mark-outs (adverse selection)
    filled_shares: Mapped[float] = mapped_column(Float, default=0.0)
    fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    partial: Mapped[bool] = mapped_column(Boolean, default=False)
    mid_at_quote: Mapped[float | None] = mapped_column(Float, nullable=True)
    mid_at_fill: Mapped[float | None] = mapped_column(Float, nullable=True)
    mid_5s: Mapped[float | None] = mapped_column(Float, nullable=True)
    mid_30s: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_spread: Mapped[float | None] = mapped_column(Float, nullable=True)   # mid_at_fill - fill_price
    adverse_5s: Mapped[float | None] = mapped_column(Float, nullable=True)        # signed mark-out vs fill
    fees_usd: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)      # at market resolution
    cancel_success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class Btc5mLiveMakerEvent(Base):
    """Append-only event log — every state transition with precise timestamps, suitable
    for replay + statistical analysis. NEVER contains secrets."""
    __tablename__ = "btc5m_live_maker_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    mono_ns: Mapped[int] = mapped_column(Integer, default=0)          # monotonic clock for latency math
    session_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    order_client_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(32), index=True)         # arm|disarm|kill|quote|submit|ack|fill|cancel_req|cancel_ack|reject|error|markout
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
