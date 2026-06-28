"""ORM tables for Research Platform V1 — a self-improving paper-research layer on
top of the BTC 5M Reversal Lab.

NEW tables only (created by Base.metadata.create_all). 100% isolated: they
reference NO production tables and are never read by live trading, ranking,
discovery, eligibility, or execution. main.py imports this module so the tables
are created at startup.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _utcnow() -> datetime:
    return datetime.utcnow()


# Strategy lifecycle statuses (Phase 1)
STATUSES = ("Research", "Paper Trading", "Candidate", "Champion", "Retired", "Archived")


class ResearchStrategy(Base):
    """One discovered/derived strategy (Phase 1). Strategies are versioned and
    never overwritten — a mutation creates a NEW row with parent_id set."""
    __tablename__ = "research_strategies"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
    name: Mapped[str] = mapped_column(String(120), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    archetype: Mapped[str] = mapped_column(String(32), index=True)   # momentum|mean_reversion|...
    params: Mapped[dict] = mapped_column(JSON, default=dict)         # tunable rule params
    origin_wallets: Mapped[list] = mapped_column(JSON, default=list)
    origin_cluster: Mapped[str | None] = mapped_column(String(32), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="Research", index=True)
    is_champion: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_ensemble: Mapped[bool] = mapped_column(Boolean, default=False)
    # paper-trading results (recomputed each replay; Phase 2)
    paper_bankroll: Mapped[float] = mapped_column(Float, default=100.0)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)        # roi/pf/wr/ev/maxdd/sharpe/calmar/...
    equity_curve: Mapped[list] = mapped_column(JSON, default=list)   # [{t, equity}]
    robust_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    trades: Mapped[int] = mapped_column(Integer, default=0)


class StrategyPaperTrade(Base):
    """One INDEPENDENT paper trade by a strategy (Phase 2). Never a real order."""
    __tablename__ = "research_paper_trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(Integer, index=True)
    market_id: Mapped[str] = mapped_column(String(120), index=True)
    market_question: Mapped[str] = mapped_column(Text, default="")
    decision_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    action: Mapped[str] = mapped_column(String(10))                 # BUY_YES|BUY_NO|NO_TRADE
    direction: Mapped[str | None] = mapped_column(String(8), nullable=True)
    entry_price: Mapped[float] = mapped_column(Float, default=0.5)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    edge: Mapped[float] = mapped_column(Float, default=0.0)
    size: Mapped[float] = mapped_column(Float, default=0.0)
    shares: Mapped[float] = mapped_column(Float, default=0.0)
    won: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    bankroll_after: Mapped[float] = mapped_column(Float, default=0.0)
    explanation: Mapped[dict] = mapped_column(JSON, default=dict)    # Phase 9 explainability


class ResearchHypothesis(Base):
    """An automatically-generated research hypothesis + evidence (Phase 8)."""
    __tablename__ = "research_hypotheses"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    text: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(32), default="general")
    status: Mapped[str] = mapped_column(String(16), default="Pending", index=True)  # Pending|Testing|Confirmed|Rejected|Inconclusive
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    tested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class NightlyReview(Base):
    """A permanently-stored nightly research review (Phase 7)."""
    __tablename__ = "research_nightly_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    report: Mapped[dict] = mapped_column(JSON, default=dict)        # the 18 sections


class ResearchExperiment(Base):
    """Append-only experiment/lineage log (Phase 10): seeds, mutations, tournaments,
    champion changes. Nothing is ever overwritten — full reproducible history."""
    __tablename__ = "research_experiments"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)       # seed|mutation|tournament|champion|cycle
    strategy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
