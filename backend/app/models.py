"""SQLAlchemy ORM models for the paper-trading copy lab."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Wallet(Base):
    __tablename__ = "wallets"

    id: Mapped[int] = mapped_column(primary_key=True)
    address: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    copy_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_active: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    trades: Mapped[list["Trade"]] = relationship(back_populates="wallet")
    stats: Mapped["WalletStat | None"] = relationship(
        back_populates="wallet", uselist=False, cascade="all, delete-orphan"
    )


class Market(Base):
    __tablename__ = "markets"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)  # condition_id / market id
    question: Mapped[str] = mapped_column(Text)
    slug: Mapped[str | None] = mapped_column(String(200), nullable=True)
    category: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    outcomes: Mapped[list] = mapped_column(JSON, default=list)        # e.g. ["Yes", "No"]
    prices: Mapped[list] = mapped_column(JSON, default=list)          # aligned with outcomes, 0..1
    token_ids: Mapped[list] = mapped_column(JSON, default=list)       # CLOB token ids per outcome
    best_bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    best_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    liquidity: Mapped[float] = mapped_column(Float, default=0.0)
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_outcome: Mapped[str | None] = mapped_column(String(80), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    def price_for(self, outcome: str) -> float | None:
        try:
            idx = self.outcomes.index(outcome)
            return float(self.prices[idx])
        except (ValueError, IndexError, TypeError):
            return None


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (UniqueConstraint("external_id", name="uq_trade_external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), index=True)
    market_id: Mapped[str] = mapped_column(ForeignKey("markets.id"), index=True)
    outcome: Mapped[str] = mapped_column(String(80))
    side: Mapped[str] = mapped_column(String(8))   # "buy" | "sell"
    price: Mapped[float] = mapped_column(Float)     # 0..1
    size: Mapped[float] = mapped_column(Float)      # USD notional
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)

    wallet: Mapped[Wallet] = relationship(back_populates="trades")
    market: Mapped[Market] = relationship()


class WalletStat(Base):
    __tablename__ = "wallet_stats"

    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), primary_key=True)
    num_trades: Mapped[int] = mapped_column(Integer, default=0)
    # Count of *resolved* positions backing the stats (live: reconstructed from
    # fills; mock: trades carrying realized P&L). Drives the copyability sample gate.
    num_settled: Mapped[int] = mapped_column(Integer, default=0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    realized_roi: Mapped[float] = mapped_column(Float, default=0.0)   # fraction, e.g. 0.25 = 25%
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)        # 0..1
    avg_trade_size: Mapped[float] = mapped_column(Float, default=0.0)
    consistency: Mapped[float] = mapped_column(Float, default=0.0)     # 0..1
    recency_score: Mapped[float] = mapped_column(Float, default=0.0)   # 0..1
    category_performance: Mapped[dict] = mapped_column(JSON, default=dict)  # {category: roi}
    score: Mapped[float] = mapped_column(Float, default=0.0)           # 0..100
    classification: Mapped[str] = mapped_column(String(24), default="insufficient_data")
    # True when stats were computed from a recent live window (not full history).
    partial_history: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    wallet: Mapped[Wallet] = relationship(back_populates="stats")


class PaperStrategy(Base):
    """A named bundle of copy rules. The MVP uses a single default strategy
    whose params mirror the runtime Settings, but the table supports more."""

    __tablename__ = "paper_strategies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    min_wallet_score: Mapped[float] = mapped_column(Float, default=65.0)
    min_trade_count: Mapped[int] = mapped_column(Integer, default=20)
    min_trade_size: Mapped[float] = mapped_column(Float, default=50.0)
    max_position_pct: Mapped[float] = mapped_column(Float, default=1.0)
    max_market_exposure_pct: Mapped[float] = mapped_column(Float, default=5.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PaperSignal(Base):
    __tablename__ = "paper_signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), index=True)
    market_id: Mapped[str] = mapped_column(ForeignKey("markets.id"), index=True)
    trade_id: Mapped[int | None] = mapped_column(ForeignKey("trades.id"), nullable=True)
    outcome: Mapped[str] = mapped_column(String(80))
    side: Mapped[str] = mapped_column(String(8), default="buy")
    observed_price: Mapped[float] = mapped_column(Float)
    suggested_entry: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)  # 0..100
    reason: Mapped[str] = mapped_column(Text, default="")
    copied: Mapped[bool] = mapped_column(Boolean, default=False)
    edge_estimate: Mapped[float] = mapped_column(Float, default=0.0)  # est. P(win) - price
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    # --- signal quality: signed price move (in the expected direction) at each
    # horizon after the signal. Positive = market moved the way we predicted.
    move_5m: Mapped[float | None] = mapped_column(Float, nullable=True)
    move_30m: Mapped[float | None] = mapped_column(Float, nullable=True)
    move_2h: Mapped[float | None] = mapped_column(Float, nullable=True)
    move_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    mfe: Mapped[float | None] = mapped_column(Float, nullable=True)  # max favorable excursion
    mae: Mapped[float | None] = mapped_column(Float, nullable=True)  # max adverse excursion
    quality_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    wallet: Mapped[Wallet] = relationship()
    market: Mapped[Market] = relationship()


class PaperPosition(Base):
    __tablename__ = "paper_positions"

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("paper_signals.id"), nullable=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), index=True)
    market_id: Mapped[str] = mapped_column(ForeignKey("markets.id"), index=True)
    outcome: Mapped[str] = mapped_column(String(80))
    side: Mapped[str] = mapped_column(String(8), default="buy")
    size: Mapped[float] = mapped_column(Float)          # USD notional at entry
    shares: Mapped[float] = mapped_column(Float)        # size / entry_price
    entry_price: Mapped[float] = mapped_column(Float)
    current_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="open", index=True)  # open|closed
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    wallet: Mapped[Wallet] = relationship()
    market: Mapped[Market] = relationship()
    signal: Mapped[PaperSignal | None] = relationship()
    fills: Mapped[list["PaperFill"]] = relationship(
        back_populates="position", cascade="all, delete-orphan"
    )


class PaperFill(Base):
    __tablename__ = "paper_fills"

    id: Mapped[int] = mapped_column(primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("paper_positions.id"), index=True)
    kind: Mapped[str] = mapped_column(String(8))  # "entry" | "exit"
    price: Mapped[float] = mapped_column(Float)
    size: Mapped[float] = mapped_column(Float)
    slippage: Mapped[float] = mapped_column(Float, default=0.0)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    position: Mapped[PaperPosition] = relationship(back_populates="fills")


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    bankroll: Mapped[float] = mapped_column(Float)          # cash bankroll (after realized PnL)
    open_exposure: Mapped[float] = mapped_column(Float, default=0.0)
    equity: Mapped[float] = mapped_column(Float)            # bankroll + unrealized mark-to-market
    total_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    open_positions: Mapped[int] = mapped_column(Integer, default=0)


class Setting(Base):
    """Key/value runtime settings, edited from the dashboard."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)  # stored as string; coerced by services


class WalletCandidate(Base):
    """A wallet surfaced by the discovery pipeline, with a copyability score and
    a track/ignore decision state. Profitability metrics are read from the
    related WalletStat; this table holds the *copyability* verdict + workflow."""

    __tablename__ = "wallet_candidates"

    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), primary_key=True)
    copyability_score: Mapped[float] = mapped_column(Float, default=0.0)  # 0..100
    classification: Mapped[str] = mapped_column(String(24), default="insufficient_data")
    state: Mapped[str] = mapped_column(String(12), default="new", index=True)  # new|tracked|ignored
    suspected_noise: Mapped[bool] = mapped_column(Boolean, default=False)
    distinct_markets: Mapped[int] = mapped_column(Integer, default=0)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    wallet: Mapped[Wallet] = relationship()


class IngestStatus(Base):
    """Singleton (id=1) row recording the most recent ingest cycle's health,
    so the dashboard can show LIVE / MOCK / API-ERROR / STALE badges."""

    __tablename__ = "ingest_status"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    data_mode: Mapped[str] = mapped_column(String(8), default="mock")
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    markets_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    trades_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    prices_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    n_markets: Mapped[int] = mapped_column(Integer, default=0)
    n_trades: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_discovery_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MarketPriceSnapshot(Base):
    """Point-in-time price for a market, recorded each ingest cycle. Used to
    evaluate signal quality (how price moved after a signal fired)."""

    __tablename__ = "market_price_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[str] = mapped_column(ForeignKey("markets.id"), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    outcome: Mapped[str] = mapped_column(String(80))
    price: Mapped[float] = mapped_column(Float)


class Backtest(Base):
    """A single backtest run comparing several strategies over a data window."""

    __tablename__ = "backtests"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), default="backtest")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)    # filters + params used
    summary: Mapped[dict] = mapped_column(JSON, default=dict)   # quick top-line numbers

    results: Mapped[list["BacktestResult"]] = relationship(
        back_populates="backtest", cascade="all, delete-orphan"
    )
    trades: Mapped[list["BacktestTrade"]] = relationship(
        back_populates="backtest", cascade="all, delete-orphan"
    )


class BacktestResult(Base):
    """Per-strategy summary metrics for one backtest."""

    __tablename__ = "backtest_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    backtest_id: Mapped[int] = mapped_column(ForeignKey("backtests.id"), index=True)
    strategy: Mapped[str] = mapped_column(String(40), index=True)
    starting_bankroll: Mapped[float] = mapped_column(Float)
    ending_bankroll: Mapped[float] = mapped_column(Float)
    total_pnl: Mapped[float] = mapped_column(Float)
    roi: Mapped[float] = mapped_column(Float)              # fraction
    max_drawdown: Mapped[float] = mapped_column(Float)     # fraction (0..1)
    win_rate: Mapped[float] = mapped_column(Float)         # 0..1
    num_trades: Mapped[int] = mapped_column(Integer)
    avg_trade_return: Mapped[float] = mapped_column(Float)  # fraction per trade
    best_trade: Mapped[float] = mapped_column(Float)       # USD pnl
    worst_trade: Mapped[float] = mapped_column(Float)      # USD pnl
    equity_curve: Mapped[list] = mapped_column(JSON, default=list)  # [{t, equity}]

    backtest: Mapped[Backtest] = relationship(back_populates="results")


class BacktestTrade(Base):
    """An individual simulated trade produced during a backtest replay."""

    __tablename__ = "backtest_trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    backtest_id: Mapped[int] = mapped_column(ForeignKey("backtests.id"), index=True)
    strategy: Mapped[str] = mapped_column(String(40), index=True)
    wallet_id: Mapped[int | None] = mapped_column(ForeignKey("wallets.id"), nullable=True)
    market_id: Mapped[str] = mapped_column(ForeignKey("markets.id"))
    category: Mapped[str | None] = mapped_column(String(80), nullable=True)
    outcome: Mapped[str] = mapped_column(String(80))
    side: Mapped[str] = mapped_column(String(8), default="buy")
    size: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float] = mapped_column(Float)
    pnl: Mapped[float] = mapped_column(Float)
    return_pct: Mapped[float] = mapped_column(Float)
    opened_at: Mapped[datetime] = mapped_column(DateTime)
    closed_at: Mapped[datetime] = mapped_column(DateTime)
    reason: Mapped[str] = mapped_column(Text, default="")

    backtest: Mapped[Backtest] = relationship(back_populates="trades")
