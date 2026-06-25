"""Pydantic response/request schemas for the API."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- Wallets ----------------------------------------------------------------
class WalletStatOut(_ORM):
    num_trades: int
    realized_pnl: float
    realized_roi: float
    win_rate: float
    avg_trade_size: float
    consistency: float
    recency_score: float
    category_performance: dict
    score: float
    classification: str
    partial_history: bool = False
    updated_at: datetime | None = None


class WalletOut(_ORM):
    id: int
    address: str
    label: str | None
    copy_enabled: bool
    last_active: datetime | None
    stats: WalletStatOut | None = None


class WalletCreate(BaseModel):
    address: str
    label: str | None = None
    copy_enabled: bool = True


class WalletUpdate(BaseModel):
    label: str | None = None
    copy_enabled: bool | None = None


# ---- Markets ----------------------------------------------------------------
class MarketOut(_ORM):
    id: str
    question: str
    slug: str | None
    category: str | None
    outcomes: list
    prices: list
    token_ids: list = []
    best_bid: float | None = None
    best_ask: float | None = None
    liquidity: float
    volume: float
    resolved: bool
    resolved_outcome: str | None
    updated_at: datetime | None = None


# ---- Signals ----------------------------------------------------------------
class SignalOut(_ORM):
    id: int
    wallet_id: int
    market_id: str
    outcome: str
    side: str
    observed_price: float
    suggested_entry: float
    confidence: float
    reason: str
    copied: bool
    created_at: datetime
    wallet_address: str | None = None
    market_question: str | None = None


# ---- Positions --------------------------------------------------------------
class PositionOut(_ORM):
    id: int
    signal_id: int | None
    wallet_id: int
    market_id: str
    outcome: str
    side: str
    size: float
    shares: float
    entry_price: float
    current_price: float
    exit_price: float | None
    status: str
    realized_pnl: float
    unrealized_pnl: float
    reason: str
    opened_at: datetime
    closed_at: datetime | None
    wallet_address: str | None = None
    market_question: str | None = None


# ---- Overview ---------------------------------------------------------------
class TopWallet(BaseModel):
    wallet_id: int
    address: str
    label: str | None
    score: float
    classification: str
    realized_roi: float
    copied_positions: int


class EquityPoint(BaseModel):
    timestamp: datetime
    equity: float
    total_pnl: float


class OverviewOut(BaseModel):
    bankroll: float
    starting_bankroll: float
    equity: float
    total_pnl: float
    roi: float
    realized_pnl: float
    unrealized_pnl: float
    open_positions: int
    closed_positions: int
    win_rate: float
    signals_today: int
    tracked_wallets: int
    tracked_markets: int
    top_wallets: list[TopWallet]
    equity_curve: list[EquityPoint]


# ---- Settings ---------------------------------------------------------------
class SettingsOut(BaseModel):
    bankroll: float
    min_wallet_score: float
    min_trade_count: int
    min_trade_size: float
    max_position_pct: float
    max_market_exposure_pct: float
    slippage_cents: float
    min_market_liquidity: float
    max_price_staleness_min: int
    min_confidence: float
    min_volume: float
    min_edge: float
    polling_interval_seconds: int
    data_mode: str
    max_daily_loss: float
    max_open_positions: int
    max_correlated_exposure_pct: float
    cooldown_losses: int
    cooldown_minutes: int
    auto_discovery_enabled: int
    discovery_interval_minutes: int
    max_wallets_to_backfill_per_cycle: int
    min_candidate_trade_count: int
    min_candidate_notional: float


class SettingsUpdate(BaseModel):
    bankroll: float | None = None
    min_wallet_score: float | None = None
    min_trade_count: int | None = None
    min_trade_size: float | None = None
    max_position_pct: float | None = None
    max_market_exposure_pct: float | None = None
    slippage_cents: float | None = None
    min_market_liquidity: float | None = None
    max_price_staleness_min: int | None = None
    min_confidence: float | None = None
    min_volume: float | None = None
    min_edge: float | None = None
    polling_interval_seconds: int | None = None
    data_mode: str | None = Field(default=None, pattern="^(mock|live)$")
    max_daily_loss: float | None = None
    max_open_positions: int | None = None
    max_correlated_exposure_pct: float | None = None
    cooldown_losses: int | None = None
    cooldown_minutes: int | None = None
    auto_discovery_enabled: int | None = None
    discovery_interval_minutes: int | None = None
    max_wallets_to_backfill_per_cycle: int | None = None
    min_candidate_trade_count: int | None = None
    min_candidate_notional: float | None = None


class MessageOut(BaseModel):
    ok: bool = True
    message: str
    detail: dict | None = None


# ---- Status / data-source indicators ---------------------------------------
class StatusOut(BaseModel):
    data_mode: str
    last_run_at: datetime | None = None
    ok: bool = True
    markets_ok: bool = True
    trades_ok: bool = True
    prices_ok: bool = True
    n_markets: int = 0
    n_trades: int = 0
    error: str | None = None
    age_seconds: float | None = None
    stale: bool = False
    partial_wallets: int = 0
    # auto-ingest worker visibility
    auto_ingest_enabled: bool = False
    auto_ingest_interval_seconds: int = 0
    worker_running: bool = False
    last_worker_error: str | None = None
    last_worker_cycle_at: datetime | None = None


class BackfillRequest(BaseModel):
    address: str
    limit: int = 200


# ---- Discovery --------------------------------------------------------------
class CandidateOut(BaseModel):
    wallet_id: int
    address: str
    label: str | None
    copyability_score: float
    classification: str
    state: str
    suspected_noise: bool
    distinct_markets: int
    reasons: list
    copy_enabled: bool
    last_active: datetime | None
    partial_history: bool
    profitability_score: float
    realized_roi: float
    win_rate: float
    num_trades: int
    avg_trade_size: float


class CandidateDetailOut(BaseModel):
    address: str
    label: str | None
    copy_enabled: bool
    state: str
    copyability_score: float
    classification: str
    suspected_noise: bool
    reasons: list
    partial_history: bool
    num_trades: int
    realized_roi: float
    win_rate: float
    avg_trade_size: float
    distinct_markets: int
    best_categories: list
    worst_categories: list
    profit_curve: list
    copied_paper_pnl: float
    copied_positions: int
    recent_trades: list
    weak_sample: bool


class DiscoveryRunRequest(BaseModel):
    max_backfill: int | None = None


# ---- Signal quality ---------------------------------------------------------
class SignalQualityOut(_ORM):
    id: int
    created_at: datetime
    wallet_address: str | None = None
    market_question: str | None = None
    outcome: str
    observed_price: float
    confidence: float
    edge_estimate: float
    copied: bool
    move_5m: float | None = None
    move_30m: float | None = None
    move_2h: float | None = None
    move_close: float | None = None
    mfe: float | None = None
    mae: float | None = None


# ---- Attribution ------------------------------------------------------------
class WalletAttributionOut(BaseModel):
    wallet_id: int
    address: str
    label: str | None
    score: float
    classification: str
    copied_signals: int
    copied_positions: int
    closed_positions: int
    winning_positions: int
    win_rate: float
    realized_pnl: float
    unrealized_pnl: float
    total_pnl: float
    roi: float
    avg_entry_price: float


# ---- Backtests --------------------------------------------------------------
class BacktestConfig(BaseModel):
    name: str = "backtest"
    starting_bankroll: float | None = None
    train_fraction: float = Field(default=0.5, ge=0.1, le=0.9)
    category: str | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None
    min_wallet_score: float | None = None
    strategies: list[str] | None = None  # default = all


class BacktestResultOut(_ORM):
    strategy: str
    starting_bankroll: float
    ending_bankroll: float
    total_pnl: float
    roi: float
    max_drawdown: float
    win_rate: float
    num_trades: int
    avg_trade_return: float
    best_trade: float
    worst_trade: float
    equity_curve: list


class BacktestOut(_ORM):
    id: int
    name: str
    created_at: datetime
    config: dict
    summary: dict
    results: list[BacktestResultOut] = []


class BacktestListItem(_ORM):
    id: int
    name: str
    created_at: datetime
    summary: dict


class BacktestTradeOut(_ORM):
    id: int
    strategy: str
    wallet_id: int | None
    market_id: str
    category: str | None
    outcome: str
    side: str
    size: float
    entry_price: float
    exit_price: float
    pnl: float
    return_pct: float
    opened_at: datetime
    closed_at: datetime
    reason: str
