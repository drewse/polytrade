"""ORM tables for the BTC 5M Independent Strategy Lab — research/paper only.

Tests OUR OWN strategies on BTC spot movement + Polymarket movement + order flow,
instead of copying wallets. 100% READ-ONLY w.r.t. production: it reads the indexed
btc5m_* tables + fetches BTC spot price, and writes only to its own
btc5m_lab_* tables. It NEVER places orders or touches live trading / execution /
sizing / bankroll / copy ranking.

NEW tables only (created by Base.metadata.create_all). Importing this module
registers them; main.py imports it so they are created at startup.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _utcnow() -> datetime:
    return datetime.utcnow()


class Btc5mLabPoint(Base):
    """One synchronized decision-point feature row: the joint state of BTC spot +
    Polymarket + order flow + timing at `t_offset_s` into a market, plus the
    market's eventual resolution (the label). This is the dataset strategies are
    backtested over."""
    __tablename__ = "btc5m_lab_points"

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[str] = mapped_column(String(120), index=True)
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    t_offset_s: Mapped[int] = mapped_column(Integer)            # seconds after market open
    secs_to_expiry: Mapped[int | None] = mapped_column(Integer, nullable=True)
    regime: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

    # joint features (BTC spot + Polymarket + order flow + timing)
    features: Mapped[dict] = mapped_column(JSON, default=dict)

    # convenience columns used heavily by the backtester / analyses
    pm_yes: Mapped[float | None] = mapped_column(Float, nullable=True)   # implied YES prob at t
    spread: Mapped[float | None] = mapped_column(Float, nullable=True)   # approx spread (proxy)
    btc_ret_30s: Mapped[float | None] = mapped_column(Float, nullable=True)
    flow_imbalance: Mapped[float | None] = mapped_column(Float, nullable=True)

    label_up: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # market resolved Up
    split: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)  # train|val|holdout
    built_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Btc5mLabStrategy(Base):
    """A generated + backtested independent strategy with train/val/holdout metrics
    and an overfit verdict. Ranked by robust out-of-sample performance."""
    __tablename__ = "btc5m_lab_strategies"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    name: Mapped[str] = mapped_column(String(120))
    family: Mapped[str] = mapped_column(String(40), index=True)   # btc_lead | fade | flow | ...
    params: Mapped[dict] = mapped_column(JSON, default=dict)

    # headline (holdout) metrics
    trades: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    roi: Mapped[float] = mapped_column(Float, default=0.0)
    profit_factor: Mapped[float] = mapped_column(Float, default=0.0)
    max_drawdown: Mapped[float] = mapped_column(Float, default=0.0)
    avg_edge: Mapped[float] = mapped_column(Float, default=0.0)
    robust_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)

    overfit: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    rejected_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="candidate")  # candidate|accepted|rejected

    metrics: Mapped[dict] = mapped_column(JSON, default=dict)     # full train/val/holdout + by-regime/duration


class Btc5mLabState(Base):
    """Singleton lab state: dataset build status + last search summary. No money
    state — research bookkeeping only."""
    __tablename__ = "btc5m_lab_state"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    markets_built: Mapped[int] = mapped_column(Integer, default=0)
    points_built: Mapped[int] = mapped_column(Integer, default=0)
    btc_price_source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    btc_fetch_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    btc_resolution_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    btc_coverage_pct: Mapped[float] = mapped_column(Float, default=0.0)
    btc_missing_s: Mapped[int] = mapped_column(Integer, default=0)
    btc_stale_s: Mapped[int] = mapped_column(Integer, default=0)
    lag_profile: Mapped[dict] = mapped_column(JSON, default=dict)   # {lag_s: avg BTC->YES corr}
    dataset_built_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    strategies_tested: Mapped[int] = mapped_column(Integer, default=0)
    strategies_accepted: Mapped[int] = mapped_column(Integer, default=0)
    last_search_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
