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
    # Quant research platform (fair-value / ensemble / feature-discovery / nightly)
    research: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    research_built_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Phase 2 — Alpha Discovery Engine (feature mining / meta-learning generations)
    alpha_generation: Mapped[int] = mapped_column(Integer, default=0)
    alpha_research: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    alpha_built_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Phase 3 — Execution Research Lab (passive-vs-market execution simulation)
    execution: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    execution_built_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class Btc5mResearchModel(Base):
    """A trained fair-value / perspective / ensemble probability model with
    calibration + EV-after-cost metrics. Research/paper only — estimates P(YES),
    never trades. The platform promotes ONLY models whose post-cost expected value
    is statistically significant out-of-sample."""
    __tablename__ = "btc5m_research_models"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    name: Mapped[str] = mapped_column(String(60), index=True)          # fair_value | ensemble | perspective:<group>
    kind: Mapped[str] = mapped_column(String(24), default="fair_value")
    algo: Mapped[str | None] = mapped_column(String(40), nullable=True)  # logistic_regression | random_forest | ...
    perspective: Mapped[str | None] = mapped_column(String(40), nullable=True)  # price_action | order_flow | ...

    # calibration (lower brier = better; ece = expected calibration error)
    brier: Mapped[float] = mapped_column(Float, default=0.25)
    calibration_score: Mapped[float] = mapped_column(Float, default=0.0)
    ece: Mapped[float] = mapped_column(Float, default=0.0)
    auc: Mapped[float] = mapped_column(Float, default=0.5)
    weight: Mapped[float] = mapped_column(Float, default=0.0)          # ensemble weight (inverse-Brier)

    # EV after realistic costs (the promotion gate)
    n_trades: Mapped[int] = mapped_column(Integer, default=0)
    ev_after_cost: Mapped[float] = mapped_column(Float, default=0.0)   # mean per-trade PnL on holdout
    ev_t_stat: Mapped[float] = mapped_column(Float, default=0.0)
    ev_ci_low: Mapped[float] = mapped_column(Float, default=0.0)
    ev_ci_high: Mapped[float] = mapped_column(Float, default=0.0)
    roi: Mapped[float] = mapped_column(Float, default=0.0)
    significant: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    promoted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    metrics: Mapped[dict] = mapped_column(JSON, default=dict)          # reliability curve, per-split, top features


class Btc5mAlphaFeature(Base):
    """A mined candidate feature in the persistent registry — tracked across nightly
    GENERATIONS so we can see which features gain/lose predictive power over time.
    Research/paper only: a feature is a number computed from a decision point; it
    never trades. Only statistically stable features survive."""
    __tablename__ = "btc5m_alpha_features"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), index=True)
    category: Mapped[str] = mapped_column(String(40), index=True)
    description: Mapped[str | None] = mapped_column(String(160), nullable=True)
    generation: Mapped[int] = mapped_column(Integer, default=0, index=True)   # last gen evaluated
    first_seen_gen: Mapped[int] = mapped_column(Integer, default=0)

    # statistical metrics (computed on the chronological splits)
    ic: Mapped[float] = mapped_column(Float, default=0.0)             # Spearman info-coefficient (train)
    ic_pearson: Mapped[float] = mapped_column(Float, default=0.0)
    mutual_info: Mapped[float] = mapped_column(Float, default=0.0)
    shap_importance: Mapped[float] = mapped_column(Float, default=0.0)  # permutation importance
    stability_splits: Mapped[float] = mapped_column(Float, default=0.0)
    stability_regime: Mapped[float] = mapped_column(Float, default=0.0)
    stability_month: Mapped[float] = mapped_column(Float, default=0.0)
    redundancy: Mapped[float] = mapped_column(Float, default=0.0)
    decay: Mapped[float] = mapped_column(Float, default=0.0)
    ic_change: Mapped[float] = mapped_column(Float, default=0.0)      # vs previous generation (gain/loss)

    survived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)  # new|active|decayed|retired
    history: Mapped[list] = mapped_column(JSON, default=list)         # [{gen, ic, mi, shap}]
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class Btc5mAlphaModelGen(Base):
    """A meta-learning model GENERATION: a fair-value model retrained on the surviving
    mined features, with its out-of-sample metrics and a lifecycle state. Meta-learning
    promotes/demotes/retires based ONLY on out-of-sample performance. A model can reach
    at most 'paper' here — it must succeed in paper trading before any live consideration
    (no live path exists in this module)."""
    __tablename__ = "btc5m_alpha_model_gens"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    generation: Mapped[int] = mapped_column(Integer, default=0, index=True)
    name: Mapped[str] = mapped_column(String(60), index=True)
    algo: Mapped[str | None] = mapped_column(String(40), nullable=True)
    feature_set: Mapped[list] = mapped_column(JSON, default=list)
    n_features: Mapped[int] = mapped_column(Integer, default=0)

    auc: Mapped[float] = mapped_column(Float, default=0.5)
    brier: Mapped[float] = mapped_column(Float, default=0.25)
    calibration_score: Mapped[float] = mapped_column(Float, default=0.0)
    ev_after_cost: Mapped[float] = mapped_column(Float, default=0.0)
    ev_t_stat: Mapped[float] = mapped_column(Float, default=0.0)
    n_trades: Mapped[int] = mapped_column(Integer, default=0)
    significant: Mapped[bool] = mapped_column(Boolean, default=False)
    regime_stability: Mapped[float] = mapped_column(Float, default=0.0)
    decay: Mapped[float] = mapped_column(Float, default=0.0)
    robust: Mapped[bool] = mapped_column(Boolean, default=False)

    lifecycle_state: Mapped[str] = mapped_column(String(16), default="candidate", index=True)  # candidate|paper|demoted|retired
    promotion_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    vs_prev: Mapped[str | None] = mapped_column(String(16), nullable=True)   # new|improved|degraded|same
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
