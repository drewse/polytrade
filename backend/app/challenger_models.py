"""ORM tables for the Paper Challenger Framework V1.

A fully isolated research/A-B-testing layer on top of the BTC 5M Reversal Lab.
NEW tables only (created by Base.metadata.create_all). It reads btc5m_* /
research_* / mi_* tables and writes ONLY to its own pc_* tables. It NEVER places
live trades or changes execution, eligibility, rankings, bankroll, copy trading,
production strategies, or risk controls. main.py imports this module so the tables
are created at startup.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _utcnow() -> datetime:
    return datetime.utcnow()


class PcChallenger(Base):
    """One paper challenger variant + its INDEPENDENT paper portfolio. The
    'production' baseline is itself a challenger (key='production')."""
    __tablename__ = "pc_challengers"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(16), index=True)   # production|timing|sizing|confidence|consensus|strategy
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    is_production: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_champion: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # independent paper portfolio (recomputed deterministically each run)
    paper_bankroll: Mapped[float] = mapped_column(Float, default=100.0)
    trades: Mapped[int] = mapped_column(Integer, default=0)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)        # roi/pf/wr/maxdd/sharpe/ev/...
    by_regime: Mapped[dict] = mapped_column(JSON, default=dict)      # {regime: {trades,roi,win_rate,improvement}}
    decay: Mapped[dict] = mapped_column(JSON, default=dict)          # rolling 7/30/90/lifetime + trend
    equity_curve: Mapped[list] = mapped_column(JSON, default=list)
    vs_production: Mapped[dict] = mapped_column(JSON, default=dict)  # {improvement,p_value,significance,n,...}
    robust_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class PcTrade(Base):
    """One INDEPENDENT paper trade by a challenger on one experiment. Never a real
    order; never mixed with production accounting."""
    __tablename__ = "pc_trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    challenger_id: Mapped[int] = mapped_column(Integer, index=True)
    challenger_key: Mapped[str] = mapped_column(String(48), index=True)
    experiment_id: Mapped[int] = mapped_column(Integer, index=True)
    market_id: Mapped[str] = mapped_column(String(120), index=True)
    regime: Mapped[str] = mapped_column(String(24), default="Mixed", index=True)
    action: Mapped[str] = mapped_column(String(10))                 # BUY_YES|BUY_NO|NO_TRADE
    direction: Mapped[str | None] = mapped_column(String(8), nullable=True)
    entry_price: Mapped[float] = mapped_column(Float, default=0.5)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    size: Mapped[float] = mapped_column(Float, default=0.0)
    shares: Mapped[float] = mapped_column(Float, default=0.0)
    won: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    bankroll_after: Mapped[float] = mapped_column(Float, default=0.0)
    decision_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    explanation: Mapped[dict] = mapped_column(JSON, default=dict)


class PcExperiment(Base):
    """One immutable A/B experiment per production 'would-buy' opportunity. Stores
    the production decision + every challenger's decision + outcome + winner.
    Append-only — never overwritten."""
    __tablename__ = "pc_experiments"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    market_id: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    market_question: Mapped[str] = mapped_column(Text, default="")
    regime: Mapped[str] = mapped_column(String(24), default="Mixed", index=True)
    decision_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(8), nullable=True)   # YES|NO
    production_decision: Mapped[dict] = mapped_column(JSON, default=dict)
    challenger_decisions: Mapped[dict] = mapped_column(JSON, default=dict)  # {key: {action,pnl,...}}
    winner: Mapped[str | None] = mapped_column(String(48), nullable=True)
    improvement: Mapped[float] = mapped_column(Float, default=0.0)          # winner_pnl - production_pnl


class PcRecommendation(Base):
    """Automatic research recommendation (timing/sizing/confidence/consensus/
    strategy). Informational only — never modifies production."""
    __tablename__ = "pc_recommendations"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    category: Mapped[str] = mapped_column(String(16), index=True)
    text: Mapped[str] = mapped_column(Text)
    significance: Mapped[str] = mapped_column(String(20), default="Promising")
    scope: Mapped[str] = mapped_column(String(32), default="global")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)


class PcNightlyReview(Base):
    """Permanently-stored nightly challenger review."""
    __tablename__ = "pc_nightly_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    report: Mapped[dict] = mapped_column(JSON, default=dict)
