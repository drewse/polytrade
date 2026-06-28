"""ORM tables for the BTC 5M Reversal Lab — a fully isolated research module.

These are NEW tables only (created by Base.metadata.create_all). They share the
declarative Base with the rest of the app but reference NO production tables by
foreign key and are never read by live trading, ranking, discovery, or the
indexer. Importing this module registers the tables; main.py imports it so they
are created at startup.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _utcnow() -> datetime:
    return datetime.utcnow()


class Btc5mMarket(Base):
    """One indexed BTC 5-minute market (Phase 1 dataset)."""
    __tablename__ = "btc5m_markets"

    market_id: Mapped[str] = mapped_column(String(120), primary_key=True)   # condition id
    question: Mapped[str] = mapped_column(Text, default="")
    slug: Mapped[str | None] = mapped_column(String(200), nullable=True)
    condition_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    token_ids: Mapped[list] = mapped_column(JSON, default=list)
    outcomes: Mapped[list] = mapped_column(JSON, default=list)
    created_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolution_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expiry: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    final_outcome: Mapped[str | None] = mapped_column(String(80), nullable=True)
    price_history: Mapped[list] = mapped_column(JSON, default=list)          # [{t, yes}] if available
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    liquidity: Mapped[float] = mapped_column(Float, default=0.0)
    orderbook_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    trade_count: Mapped[int] = mapped_column(Integer, default=0)
    wallet_count: Mapped[int] = mapped_column(Integer, default=0)
    indexed_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Btc5mTrade(Base):
    """One trade in a BTC 5m market, with derived timing + a reconstructed
    pre-entry feature vector (Phase 1 + Phase 3)."""
    __tablename__ = "btc5m_trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_trade_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    external_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    market_id: Mapped[str] = mapped_column(String(120), index=True)
    wallet_address: Mapped[str] = mapped_column(String(64), index=True)
    side: Mapped[str] = mapped_column(String(8))            # buy | sell
    direction: Mapped[str] = mapped_column(String(8))       # YES | NO
    price: Mapped[float] = mapped_column(Float)
    shares: Mapped[float] = mapped_column(Float, default=0.0)
    usd_value: Mapped[float] = mapped_column(Float, default=0.0)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    seconds_from_creation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seconds_until_expiry: Mapped[int | None] = mapped_column(Integer, nullable=True)
    opened_position: Mapped[bool] = mapped_column(Boolean, default=True)     # opened vs closed
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    won: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    features: Mapped[dict] = mapped_column(JSON, default=dict)               # reconstructed feature vector
    label_direction: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 1=YES, 0=NO (model target)
    indexed_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Btc5mWalletProfile(Base):
    """Wallet fingerprint + Wallet IQ + cluster assignment (Phase 2 + Phase 5)."""
    __tablename__ = "btc5m_wallet_profiles"

    wallet_address: Mapped[str] = mapped_column(String(64), primary_key=True)
    trade_count: Mapped[int] = mapped_column(Integer, default=0)
    settled_count: Mapped[int] = mapped_column(Integer, default=0)
    roi: Mapped[float] = mapped_column(Float, default=0.0)
    profit_factor: Mapped[float] = mapped_column(Float, default=0.0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    avg_trade_size: Mapped[float] = mapped_column(Float, default=0.0)
    profitable: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    cluster: Mapped[str] = mapped_column(String(24), default="Unknown")
    cluster_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)        # full fingerprint
    wallet_iq: Mapped[dict] = mapped_column(JSON, default=dict)      # Wallet IQ card
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class Btc5mModel(Base):
    """A trained research model on the leaderboard (Phase 4 + Phase 8)."""
    __tablename__ = "btc5m_models"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    name: Mapped[str] = mapped_column(String(40), index=True)        # model family
    scope: Mapped[str] = mapped_column(String(64), default="global")  # global | <wallet>
    accuracy: Mapped[float] = mapped_column(Float, default=0.0)
    precision: Mapped[float] = mapped_column(Float, default=0.0)
    recall: Mapped[float] = mapped_column(Float, default=0.0)
    f1: Mapped[float] = mapped_column(Float, default=0.0)
    cv_f1: Mapped[float] = mapped_column(Float, default=0.0)
    n_train: Mapped[int] = mapped_column(Integer, default=0)
    n_test: Mapped[int] = mapped_column(Integer, default=0)
    is_champion: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    promotion_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    feature_importance: Mapped[list] = mapped_column(JSON, default=list)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)


class Btc5mShadowSignal(Base):
    """A shadow-strategy paper prediction (Phase 7). NEVER a real order."""
    __tablename__ = "btc5m_shadow_signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    market_id: Mapped[str] = mapped_column(String(120), index=True)
    market_question: Mapped[str] = mapped_column(Text, default="")
    action: Mapped[str] = mapped_column(String(10))                 # BUY_YES | BUY_NO | NO_TRADE
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    expected_edge: Mapped[float] = mapped_column(Float, default=0.0)
    predicted_probability: Mapped[float] = mapped_column(Float, default=0.0)
    model_name: Mapped[str] = mapped_column(String(40), default="")
    supporting_wallets: Mapped[list] = mapped_column(JSON, default=list)
    consensus_strength: Mapped[float] = mapped_column(Float, default=0.0)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)


class Btc5mResearchNote(Base):
    """Append-only research log — what the continuous-learning loop did each run
    (Phase 8): champion promotions, batch summaries, notable findings."""
    __tablename__ = "btc5m_research_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(24), default="note")   # batch | promotion | finding
    title: Mapped[str] = mapped_column(String(160), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
