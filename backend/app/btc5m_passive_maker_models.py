"""ORM tables for the BTC 5M Passive-Maker PAPER harness — research/paper ONLY.

Forward-collects PAPER quotes/fills for the 5-second passive-maker edge so we can
decide, with a real sample, whether it is genuine. These are NEW tables only
(created by Base.metadata.create_all). They reference NO production tables by
foreign key and are never read by live trading / ranking / execution / bankroll.
There is NO order-placement field anywhere here — a "fill" is a SIMULATED paper
fill inferred from the historical trade stream.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _utcnow() -> datetime:
    return datetime.utcnow()


class Btc5mPaperQuote(Base):
    """One PAPER quote: where we WOULD have posted a passive bid, whether it would
    have filled (per the worst-case queue model on the real trade stream), and the
    settled paper PnL. No real order is ever created."""
    __tablename__ = "btc5m_paper_quotes"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    market_id: Mapped[str] = mapped_column(String(120), index=True)
    token_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(8), nullable=True)     # YES | NO
    side: Mapped[str] = mapped_column(String(8))                              # the side we quote
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    policy: Mapped[str] = mapped_column(String(16), default="join_bid")       # join_bid | improve_bid

    # quote economics
    quote_price: Mapped[float] = mapped_column(Float)
    best_bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    best_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    spread: Mapped[float | None] = mapped_column(Float, nullable=True)
    quote_t_offset_s: Mapped[int] = mapped_column(Integer)                    # seconds from market open
    cancel_t_offset_s: Mapped[int] = mapped_column(Integer)                   # quote + 5s
    quote_lifetime_s: Mapped[float] = mapped_column(Float, default=5.0)
    queue_assumption: Mapped[str] = mapped_column(String(8), default="worst")
    queue_ahead_usd: Mapped[float] = mapped_column(Float, default=0.0)

    # fill (paper — inferred from the trade stream, never a real order)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)  # pending|filled|expired|skipped
    filled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    fill_t_offset_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fill_delay_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fill_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)    # which trade crossed/through
    reason_not_filled: Mapped[str | None] = mapped_column(String(120), nullable=True)
    reason_skipped: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # settlement
    market_resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_up: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)  # per $1 paper stake
    spread_captured: Mapped[float | None] = mapped_column(Float, nullable=True)
    won: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    settled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    regime: Mapped[str | None] = mapped_column(String(40), nullable=True)
    week: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)   # ISO YYYY-Www
    # forward-pipeline tagging — keep market families + quote kinds SEPARATE so the
    # BTC gate never mixes correlated multi-point quotes or broad-universe markets.
    market_family: Mapped[str] = mapped_column(String(24), default="btc", index=True)  # btc | sports | politics | ...
    quote_kind: Mapped[str] = mapped_column(String(16), default="independent", index=True)  # independent | multi_point
    decision_index: Mapped[int] = mapped_column(Integer, default=0)                  # 0 = earliest/market-level
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class Btc5mForwardState(Base):
    """Singleton state for the forward conversion pipeline: per-stage diagnostics so we
    can see exactly where the funnel stalls. No money state."""
    __tablename__ = "btc5m_forward_state"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    runs: Mapped[int] = mapped_column(Integer, default=0)
    # per-stage snapshots: {stage: {total, new_since_last, latest_ts, last_run_at, last_error, blocked}}
    funnel: Mapped[dict] = mapped_column(JSON, default=dict)
    last_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class Btc5mPaperBookSnapshot(Base):
    """A periodic L2 order-book snapshot (read-only). The path to eventually replace
    best/mid/worst queue ASSUMPTIONS with MEASURED queue position. Fail-soft: an
    `error` row is stored when the book is unavailable."""
    __tablename__ = "btc5m_paper_book_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    market_id: Mapped[str] = mapped_column(String(120), index=True)
    token_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    best_bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    best_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    spread: Mapped[float | None] = mapped_column(Float, nullable=True)
    bid_levels: Mapped[list] = mapped_column(JSON, default=list)              # [[price, size], ...]
    ask_levels: Mapped[list] = mapped_column(JSON, default=list)
    depth_at_quote: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Btc5mPaperMakerState(Base):
    """Singleton harness state: cumulative paper stats + the hard-coded validation
    gate. No money state. `status` is never 'live' — at best it becomes
    'paper_validated' (still no live path)."""
    __tablename__ = "btc5m_paper_maker_state"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    status: Mapped[str] = mapped_column(String(32), default="research_only_not_validated", index=True)
    quotes: Mapped[int] = mapped_column(Integer, default=0)
    fills: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    fill_rate: Mapped[float] = mapped_column(Float, default=0.0)
    ev_per_fill: Mapped[float] = mapped_column(Float, default=0.0)
    ev_per_day_estimate: Mapped[float] = mapped_column(Float, default=0.0)
    prob_ev_positive: Mapped[float] = mapped_column(Float, default=0.0)
    ci_low: Mapped[float] = mapped_column(Float, default=0.0)
    ci_high: Mapped[float] = mapped_column(Float, default=0.0)
    spread_captured: Mapped[float] = mapped_column(Float, default=0.0)
    adverse_selection: Mapped[float] = mapped_column(Float, default=0.0)
    weeks_covered: Mapped[int] = mapped_column(Integer, default=0)
    gate: Mapped[dict] = mapped_column(JSON, default=dict)                    # per-condition pass/fail
    queue_breakdown: Mapped[dict] = mapped_column(JSON, default=dict)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
