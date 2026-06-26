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
    # profitability metrics (drive the redesigned copyability model)
    profit_factor: Mapped[float] = mapped_column(Float, default=0.0)
    expectancy: Mapped[float] = mapped_column(Float, default=0.0)      # USD per settled position
    sharpe: Mapped[float] = mapped_column(Float, default=0.0)          # per-trade
    max_drawdown: Mapped[float] = mapped_column(Float, default=0.0)    # fraction 0..1
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


class Top20Strategy(Base):
    """One of the 20 paper copy-trading strategy variants (the TOP 20 lab).

    Each strategy consumes the SAME live signal stream but applies different
    entry/sizing/filter rules. Independent from the main paper engine
    (PaperStrategy/PaperPosition) so the dashboard is untouched. PAPER ONLY —
    no real orders are ever placed."""

    __tablename__ = "top20_strategies"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    starting_bankroll: Mapped[float] = mapped_column(Float, default=10_000.0)
    fractional_kelly: Mapped[float] = mapped_column(Float, default=0.25)
    exit_policy: Mapped[str] = mapped_column(String(40), default="hold")  # see top20/exits.py
    philosophy: Mapped[str] = mapped_column(String(24), default="mixed")  # wallet|signal|market|sizing
    # experiment metadata (Phase 11) + lifecycle (Phase 18)
    version: Mapped[int] = mapped_column(Integer, default=1)
    parent_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="production")  # experimental|candidate|production|retired
    notes: Mapped[str] = mapped_column(Text, default="")
    param_hash: Mapped[str] = mapped_column(String(32), default="")  # reproducibility id
    params: Mapped[dict] = mapped_column(JSON, default=dict)  # filter/sizing knobs (transparency)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)  # persisted Phase-1 analytics (notional)
    realistic_metrics: Mapped[dict] = mapped_column(JSON, default=dict)  # capital-constrained replay
    signals_evaluated: Mapped[int] = mapped_column(Integer, default=0)
    trades_entered: Mapped[int] = mapped_column(Integer, default=0)
    last_signal_id: Mapped[int] = mapped_column(Integer, default=0)  # evaluation watermark
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    trades: Mapped[list["Top20Trade"]] = relationship(
        back_populates="strategy", cascade="all, delete-orphan"
    )


class Top20Trade(Base):
    """A single paper trade/position entered by a TOP 20 strategy. Combines the
    'trade' and 'position' concepts (1:1 here). No hard FK to signals/markets so
    rows survive signal/market churn (mock reseeds). PAPER ONLY."""

    __tablename__ = "top20_trades"
    __table_args__ = (
        UniqueConstraint("strategy_id", "signal_id", name="uq_top20_strategy_signal"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("top20_strategies.id"), index=True)
    signal_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    wallet_address: Mapped[str] = mapped_column(String(64))
    market_id: Mapped[str] = mapped_column(String(80), index=True)
    market_question: Mapped[str] = mapped_column(Text, default="")
    outcome: Mapped[str] = mapped_column(String(80))
    side: Mapped[str] = mapped_column(String(8), default="buy")
    entry_price: Mapped[float] = mapped_column(Float)       # 0..1
    size_shares: Mapped[float] = mapped_column(Float)       # stake / entry_price
    stake: Mapped[float] = mapped_column(Float)             # USD risked
    estimated_probability: Mapped[float] = mapped_column(Float)  # clamped 0.01..0.99
    kelly_fraction: Mapped[float] = mapped_column(Float)         # raw Kelly (pre-fraction)
    fractional_kelly_used: Mapped[float] = mapped_column(Float)  # e.g. 0.25
    sizing_reason: Mapped[str] = mapped_column(Text, default="")
    entry_time: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    status: Mapped[str] = mapped_column(String(12), default="open", index=True)  # open|closed
    source: Mapped[str] = mapped_column(String(8), default="live", index=True)  # live|replay
    current_price: Mapped[float] = mapped_column(Float, default=0.0)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # explainability + analytics (Phases 1/8)
    entry_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    entry_edge: Mapped[float] = mapped_column(Float, default=0.0)
    wallet_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    holding_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    explanation: Mapped[dict] = mapped_column(JSON, default=dict)  # structured why-entered

    strategy: Mapped[Top20Strategy] = relationship(back_populates="trades")


class Top20FeatureVector(Base):
    """Phase 20 — a labeled feature vector per paper trade, captured at entry and
    labeled at settlement. This is the supervised-learning dataset for a future
    probability model (no ML trained yet — collection only). PAPER ONLY."""

    __tablename__ = "top20_feature_vectors"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    strategy_id: Mapped[int] = mapped_column(Integer, index=True)
    strategy_key: Mapped[str] = mapped_column(String(40), index=True)
    signal_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    trade_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # inputs (signal + wallet + market features, probability, sizing, decision)
    features: Mapped[dict] = mapped_column(JSON, default=dict)
    decision: Mapped[str] = mapped_column(String(8), default="take")  # take (skips not stored)
    source: Mapped[str] = mapped_column(String(8), default="live", index=True)  # live|replay
    # labels (filled at settlement)
    label_outcome: Mapped[str | None] = mapped_column(String(80), nullable=True)
    label_realized_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    label_realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    label_exit_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    settled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class Top20Snapshot(Base):
    """Per-strategy equity snapshot over time, used for the drawdown / curve."""

    __tablename__ = "top20_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("top20_strategies.id"), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    bankroll: Mapped[float] = mapped_column(Float)
    equity: Mapped[float] = mapped_column(Float)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    open_positions: Mapped[int] = mapped_column(Integer, default=0)


class LiveExecution(Base):
    """One real (or dry-run) live order, with full execution forensics so every
    trade can be reconstructed and reconciled against Polymarket. Live trading is
    OFF by default and gated behind LIVE_TRADING_ENABLED + a completed executor."""

    __tablename__ = "live_executions"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_live_idempotency"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(120), index=True)  # (strategy,signal) dedupe
    executor: Mapped[str] = mapped_column(String(16), default="dry_run")   # dry_run|polymarket
    strategy_key: Mapped[str] = mapped_column(String(40), index=True)
    wallet_address: Mapped[str] = mapped_column(String(64))
    signal_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    market_id: Mapped[str] = mapped_column(String(80), index=True)
    market_question: Mapped[str] = mapped_column(Text, default="")
    outcome: Mapped[str] = mapped_column(String(80))
    side: Mapped[str] = mapped_column(String(8), default="buy")
    # sizing + fills
    expected_price: Mapped[float] = mapped_column(Float)
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # submitted marketable limit
    fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    slippage: Mapped[float | None] = mapped_column(Float, nullable=True)  # fill - expected (fraction)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    order_id: Mapped[str | None] = mapped_column(String(120), nullable=True)  # venue order id
    size_usd: Mapped[float] = mapped_column(Float)        # stake (USD risked)
    shares: Mapped[float] = mapped_column(Float, default=0.0)
    # latency forensics
    order_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    confirm_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    # lifecycle + accounting
    status: Mapped[str] = mapped_column(String(12), default="open", index=True)  # open|closed|rejected
    entry_reason: Mapped[str] = mapped_column(Text, default="")
    exit_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # limit-at-reference execution forensics
    fill_outcome: Mapped[str | None] = mapped_column(String(28), nullable=True)   # filled|partially_filled_cancelled|unfilled_cancelled|submit_error|cancel_error|simulated
    venue_error: Mapped[str | None] = mapped_column(Text, nullable=True)          # FULL untruncated venue error text
    requested_size_usd: Mapped[float | None] = mapped_column(Float, nullable=True)  # intended stake (size_usd = filled)
    tick_size: Mapped[float | None] = mapped_column(Float, nullable=True)          # venue book tick used for the decision
    min_order_size: Mapped[float | None] = mapped_column(Float, nullable=True)     # venue book min order size (shares) used
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    bankroll_before: Mapped[float] = mapped_column(Float, default=0.0)
    bankroll_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LiveState(Base):
    """Singleton (id=1) live-account state: bankroll + the trading halt latch
    (a tripped limit stops new orders until manual intervention)."""

    __tablename__ = "live_state"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    starting_bankroll: Mapped[float] = mapped_column(Float, default=100.0)
    bankroll: Mapped[float] = mapped_column(Float, default=100.0)  # starting + realized
    halted: Mapped[bool] = mapped_column(Boolean, default=False)
    halt_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    halted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class LiveSignalDecision(Base):
    """Per-signal LIVE decision audit trail — the heart of the event-driven
    executor's observability. Exactly ONE row per PaperSignal the live pipeline
    has evaluated; the existence of a row is the 'processed' marker that
    guarantees a signal is never executed twice. Records the full gate-by-gate
    outcome so EVERY decision (placed or not) is explainable — there is never an
    unexplained 'placed=0'. Live-only; touches no paper-research code."""

    __tablename__ = "live_signal_decisions"
    __table_args__ = (UniqueConstraint("signal_id", name="uq_live_signal_decision"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    signal_id: Mapped[int] = mapped_column(Integer, index=True)
    # terminal lifecycle state: filled | skipped | rejected | expired
    status: Mapped[str] = mapped_column(String(12), index=True)
    category: Mapped[str] = mapped_column(String(32))      # precise machine reason key
    reason: Mapped[str] = mapped_column(Text, default="")  # human-readable explanation
    wallet_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    edge: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    production_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    gates: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # ordered gate trail
    execution_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # -> LiveExecution


class ReplayState(Base):
    """Singleton (id=1) checkpoint for the historical replay engine — supports
    resume / incremental replay so we never restart from scratch. PAPER ONLY."""

    __tablename__ = "replay_state"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    markets_offset: Mapped[int] = mapped_column(Integer, default=0)      # Gamma pagination cursor
    markets_backfilled: Mapped[int] = mapped_column(Integer, default=0)
    wallets_backfilled: Mapped[int] = mapped_column(Integer, default=0)
    last_event_id: Mapped[int] = mapped_column(Integer, default=0)       # replay cursor (Trade.id)
    events_processed: Mapped[int] = mapped_column(Integer, default=0)
    signals_generated: Mapped[int] = mapped_column(Integer, default=0)
    feature_vectors: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="idle")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


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
