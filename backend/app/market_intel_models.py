"""ORM tables for Market Intelligence & Regime Engine V1.

A fully isolated research/analytics layer on top of the BTC 5M Reversal Lab. NEW
tables only (created by Base.metadata.create_all). They read the btc5m_* and
research_* tables and write ONLY to their own mi_* tables — never touching live
trading, execution, ranking, eligibility, discovery, copy-trading, or bankroll.
main.py imports this module so the tables are created at startup.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _utcnow() -> datetime:
    return datetime.utcnow()


class MiMarketProfile(Base):
    """A permanent per-market intelligence profile + regime classification
    (Phase 1 + Phase 2)."""
    __tablename__ = "mi_market_profiles"

    market_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    question: Mapped[str] = mapped_column(Text, default="")
    created_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expiry: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolution_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_outcome: Mapped[str | None] = mapped_column(String(80), nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # phase-1 metrics (price / volume / orderflow / timing), all in JSON
    price: Mapped[dict] = mapped_column(JSON, default=dict)
    volume: Mapped[dict] = mapped_column(JSON, default=dict)
    orderflow: Mapped[dict] = mapped_column(JSON, default=dict)
    timing: Mapped[dict] = mapped_column(JSON, default=dict)
    # phase-2 regime classification
    primary_regime: Mapped[str] = mapped_column(String(24), default="Mixed", index=True)
    secondary_regime: Mapped[str | None] = mapped_column(String(24), nullable=True)
    regime_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    regime_evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    feature_means: Mapped[dict] = mapped_column(JSON, default=dict)   # mean reconstructed features
    indexed_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class MiWalletRegime(Base):
    """Per-wallet market-intelligence aggregate: performance by regime (Phase 3),
    rolling decay (Phase 6), originality (Phase 7) and position-size conviction
    (Phase 8)."""
    __tablename__ = "mi_wallet_regime"

    wallet_address: Mapped[str] = mapped_column(String(64), primary_key=True)
    profitable: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    cluster: Mapped[str] = mapped_column(String(24), default="Unknown")
    trade_count: Mapped[int] = mapped_column(Integer, default=0)
    by_regime: Mapped[dict] = mapped_column(JSON, default=dict)        # {regime: {trades,win_rate,roi}}
    best_regime: Mapped[str | None] = mapped_column(String(24), nullable=True)
    specialization_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    decay: Mapped[dict] = mapped_column(JSON, default=dict)            # {7d,30d,90d,lifetime,trend}
    originality: Mapped[dict] = mapped_column(JSON, default=dict)      # {score,role,avg_delay_s,...}
    originality_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    position_size: Mapped[dict] = mapped_column(JSON, default=dict)    # conviction / sizing metrics
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class MiStrategyRegime(Base):
    """Per-strategy performance heatmap by regime (Phase 4) + decay (Phase 6)."""
    __tablename__ = "mi_strategy_regime"

    strategy_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    archetype: Mapped[str] = mapped_column(String(32), default="")
    by_regime: Mapped[dict] = mapped_column(JSON, default=dict)        # {regime: {win_rate,roi,pf,maxdd,trades,ev,confidence}}
    best_regime: Mapped[str | None] = mapped_column(String(24), nullable=True)
    worst_regime: Mapped[str | None] = mapped_column(String(24), nullable=True)
    decay: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class MiDecaySnapshot(Base):
    """Append-only rolling-performance history (Phase 6) — never deleted, so the
    full decay timeline is preserved."""
    __tablename__ = "mi_decay_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)         # wallet|strategy|regime|consensus
    entity: Mapped[str] = mapped_column(String(120), index=True)
    window: Mapped[str] = mapped_column(String(12))                   # 7d|30d|90d|lifetime
    trades: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    roi: Mapped[float] = mapped_column(Float, default=0.0)
    trend: Mapped[str] = mapped_column(String(12), default="stable")  # improving|stable|decaying|broken


class MiCounterfactual(Base):
    """Counterfactual timing-sensitivity result (Phase 9), append-only per batch."""
    __tablename__ = "mi_counterfactuals"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    scope: Mapped[str] = mapped_column(String(64), default="global")  # global | <wallet>
    trades_tested: Mapped[int] = mapped_column(Integer, default=0)
    optimal_shift_s: Mapped[int] = mapped_column(Integer, default=0)
    expected_improvement: Mapped[float] = mapped_column(Float, default=0.0)
    timing_sensitivity: Mapped[dict] = mapped_column(JSON, default=dict)  # {shift_s: avg_pnl_delta}
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class MiRecommendation(Base):
    """Per-market regime recommendation (Phase 10) — informational only, never
    wired to live trading."""
    __tablename__ = "mi_recommendations"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    market_id: Mapped[str] = mapped_column(String(120), index=True)
    market_question: Mapped[str] = mapped_column(Text, default="")
    regime: Mapped[str] = mapped_column(String(24), default="Mixed")
    analog_markets: Mapped[list] = mapped_column(JSON, default=list)
    best_clusters: Mapped[list] = mapped_column(JSON, default=list)
    best_strategies: Mapped[list] = mapped_column(JSON, default=list)
    best_wallets: Mapped[list] = mapped_column(JSON, default=list)
    consensus_strength: Mapped[float] = mapped_column(Float, default=0.0)
    expected_edge: Mapped[float] = mapped_column(Float, default=0.0)
    research_confidence: Mapped[float] = mapped_column(Float, default=0.0)


class MiNightlyReview(Base):
    """Permanently-stored Market-Intelligence nightly review (Phase 11)."""
    __tablename__ = "mi_nightly_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    report: Mapped[dict] = mapped_column(JSON, default=dict)
