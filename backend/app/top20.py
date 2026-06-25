"""
TOP 20 — a paper-trading strategy lab.

Runs 20 independent copy-trading strategies over the SAME live signal stream
(the existing tracked-sharp-wallet signals). Each strategy applies its own
entry/sizing/filter rules and keeps its own bankroll, positions and P&L.

STRICTLY PAPER ONLY: this module never places an order, never touches a wallet
or key, and never calls any live-trading code. It only reads PaperSignal rows
and writes Top20* rows.

Sizing uses FRACTIONAL Kelly (default 0.25) with hard caps. The pure functions
(`estimate_probability`, `kelly_stake`, `passes_filters`) are DB-free and unit
tested in tests/test_top20.py; the rest is DB orchestration.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (
    Market,
    PaperSignal,
    Top20Snapshot,
    Top20Strategy,
    Top20Trade,
    Wallet,
    WalletCandidate,
    WalletStat,
)

# ---------------------------------------------------------------------------
# Constants (sizing caps). These are the *defaults*; conservative/aggressive
# strategies override fractional_kelly / max_bet / max_position_pct.
# ---------------------------------------------------------------------------
STARTING_BANKROLL = 10_000.0
FRACTIONAL_KELLY_DEFAULT = 0.25
MIN_BET = 5.0
MAX_BET = 250.0
MAX_POSITION_PCT = 0.05          # 5% of strategy bankroll per position
MAX_MARKET_EXPOSURE_PCT = 0.10   # 10% of strategy bankroll per market

PRICE_FLOOR, PRICE_CEIL = 0.01, 0.99


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ---------------------------------------------------------------------------
# Strategy definitions — exactly 20 variants.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StrategyConfig:
    key: str
    name: str
    description: str
    top_n: int | None = None
    classifications: tuple[str, ...] | None = None
    min_confidence: float = 0.0          # 0..100
    min_edge: float = 0.0                # fraction (0.03 = 3%)
    min_liquidity: float = 0.0           # USD
    max_age_min: float | None = None     # minutes
    fractional_kelly: float = FRACTIONAL_KELLY_DEFAULT
    min_bet: float = MIN_BET
    max_bet: float = MAX_BET
    max_position_pct: float = MAX_POSITION_PCT
    max_market_exposure_pct: float = MAX_MARKET_EXPOSURE_PCT

    def to_params(self) -> dict:
        return {
            "top_n": self.top_n, "classifications": list(self.classifications or []),
            "min_confidence": self.min_confidence, "min_edge": self.min_edge,
            "min_liquidity": self.min_liquidity, "max_age_min": self.max_age_min,
            "fractional_kelly": self.fractional_kelly, "min_bet": self.min_bet,
            "max_bet": self.max_bet, "max_position_pct": self.max_position_pct,
            "max_market_exposure_pct": self.max_market_exposure_pct,
        }


STRATEGIES: list[StrategyConfig] = [
    StrategyConfig("top5", "Top 5 Wallets", "Copy only the 5 highest-copyability wallets.", top_n=5),
    StrategyConfig("top10", "Top 10 Wallets", "Copy only the 10 highest-copyability wallets.", top_n=10),
    StrategyConfig("top20", "Top 20 Wallets", "Copy only the 20 highest-copyability wallets.", top_n=20),
    StrategyConfig("good_only", "Good Candidates Only", "Only signals from good_candidate wallets.",
                   classifications=("good_candidate",)),
    StrategyConfig("good_watch", "Good + Watchlist", "Signals from good_candidate or watchlist wallets.",
                   classifications=("good_candidate", "watchlist")),
    StrategyConfig("conf70", "Confidence ≥ 70", "Enter only when signal confidence ≥ 70.", min_confidence=70),
    StrategyConfig("conf80", "Confidence ≥ 80", "Enter only when signal confidence ≥ 80.", min_confidence=80),
    StrategyConfig("conf90", "Confidence ≥ 90", "Enter only when signal confidence ≥ 90.", min_confidence=90),
    StrategyConfig("edge3", "Edge ≥ 3%", "Enter only when observed edge ≥ 3%.", min_edge=0.03),
    StrategyConfig("edge5", "Edge ≥ 5%", "Enter only when observed edge ≥ 5%.", min_edge=0.05),
    StrategyConfig("edge10", "Edge ≥ 10%", "Enter only when observed edge ≥ 10%.", min_edge=0.10),
    StrategyConfig("liq1k", "Liquidity ≥ $1k", "Enter only on markets with ≥ $1,000 liquidity.", min_liquidity=1_000),
    StrategyConfig("liq5k", "Liquidity ≥ $5k", "Enter only on markets with ≥ $5,000 liquidity.", min_liquidity=5_000),
    StrategyConfig("liq10k", "Liquidity ≥ $10k", "Enter only on markets with ≥ $10,000 liquidity.", min_liquidity=10_000),
    StrategyConfig("fresh30", "Fresh ≤ 30m", "Only signals fresher than 30 minutes.", max_age_min=30),
    StrategyConfig("fresh60", "Fresh ≤ 60m", "Only signals fresher than 60 minutes.", max_age_min=60),
    StrategyConfig("fresh120", "Fresh ≤ 120m", "Only signals fresher than 120 minutes.", max_age_min=120),
    StrategyConfig("conservative", "Conservative Sizing",
                   "Smaller fractional-Kelly (0.15), tight 2% position cap.",
                   fractional_kelly=0.15, max_position_pct=0.02, max_bet=100),
    StrategyConfig("aggressive", "Aggressive Sizing",
                   "Larger fractional-Kelly (0.50), full 10% position cap.",
                   fractional_kelly=0.50, max_position_pct=0.10, max_bet=250),
    StrategyConfig("balanced", "Balanced Default",
                   "All signals, default fractional-Kelly (0.25) and caps."),
]

CONFIG_BY_KEY: dict[str, StrategyConfig] = {c.key: c for c in STRATEGIES}
assert len(STRATEGIES) == 20, "TOP 20 must define exactly 20 strategies"


# ---------------------------------------------------------------------------
# Pure functions (no DB) — unit tested.
# ---------------------------------------------------------------------------
def estimate_probability(observed_price: float, edge: float | None,
                         win_rate: float | None, confidence: float | None) -> float:
    """Conservative estimate of P(win) for an outcome, clamped to 0.01..0.99.

    Prefers price + observed edge (so a wallet buying *below* its win-rate looks
    favourable). Falls back to a conservative blend of win-rate, confidence and
    price when no edge is available. Never fabricates extreme probabilities."""
    price = clamp(_safe(observed_price, 0.5), PRICE_FLOOR, PRICE_CEIL)
    if edge is not None:
        p = price + float(edge)
    else:
        win = clamp(_safe(win_rate, 0.5), 0.0, 1.0)
        conf = clamp(_safe(confidence, 50.0) / 100.0, 0.0, 1.0)
        p = 0.50 * win + 0.30 * conf + 0.20 * price
    return clamp(p, PRICE_FLOOR, PRICE_CEIL)


@dataclass
class SizingResult:
    stake: float | None              # None => skip
    kelly_fraction: float            # raw Kelly (can be <= 0)
    shares: float
    reason: str


def kelly_stake(price: float, p: float, bankroll: float, cfg: StrategyConfig,
                market_exposure_used: float = 0.0) -> SizingResult:
    """Fractional-Kelly stake for a 0..1 priced share, with strict caps.

    Returns SizingResult(stake=None, ...) to SKIP when Kelly is non-positive or
    the caps leave no room. Never returns a negative stake, never divides by
    zero (price clamped away from 0), never exceeds the caps."""
    price = clamp(_safe(price, 0.5), PRICE_FLOOR, PRICE_CEIL)
    p = clamp(_safe(p, 0.5), PRICE_FLOOR, PRICE_CEIL)
    b = (1.0 - price) / price            # net odds; price in [0.01,0.99] => b finite > 0
    q = 1.0 - p
    kelly = (b * p - q) / b
    if kelly <= 0 or bankroll <= 0:
        return SizingResult(None, round(kelly, 4), 0.0, "kelly<=0" if kelly <= 0 else "no bankroll")

    stake_fraction = max(0.0, kelly * cfg.fractional_kelly)
    stake = stake_fraction * bankroll

    # Upper bounds: per-position cap and remaining per-market exposure room.
    pos_cap = min(cfg.max_bet, cfg.max_position_pct * bankroll)
    exposure_room = cfg.max_market_exposure_pct * bankroll - market_exposure_used
    upper = min(pos_cap, exposure_room)
    if upper < cfg.min_bet:
        return SizingResult(None, round(kelly, 4), 0.0, "caps below min_bet")

    stake = min(stake, upper)
    stake = max(stake, cfg.min_bet)  # floor; safe because upper >= min_bet
    stake = round(stake, 2)
    shares = round(stake / price, 4)
    reason = (f"kelly={kelly:.3f} x {cfg.fractional_kelly} -> "
              f"${stake:.2f} (cap ${upper:.2f})")
    return SizingResult(stake, round(kelly, 4), shares, reason)


@dataclass
class SignalContext:
    rank: int | None          # wallet copyability rank (0 = best), None if unranked
    classification: str | None
    confidence: float
    edge: float
    liquidity: float
    age_min: float


def passes_filters(cfg: StrategyConfig, ctx: SignalContext) -> bool:
    """Whether a strategy's entry filters admit this signal (sizing is separate)."""
    if cfg.top_n is not None and (ctx.rank is None or ctx.rank >= cfg.top_n):
        return False
    if cfg.classifications is not None and ctx.classification not in cfg.classifications:
        return False
    if ctx.confidence < cfg.min_confidence:
        return False
    if ctx.edge < cfg.min_edge:
        return False
    if ctx.liquidity < cfg.min_liquidity:
        return False
    if cfg.max_age_min is not None and ctx.age_min > cfg.max_age_min:
        return False
    return True


def _safe(v, default: float) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# DB orchestration
# ---------------------------------------------------------------------------
def ensure_strategies(db: Session) -> list[Top20Strategy]:
    """Idempotently create the 20 strategy rows; return them ordered by id."""
    existing = {s.key: s for s in db.scalars(select(Top20Strategy)).all()}
    changed = False
    for cfg in STRATEGIES:
        row = existing.get(cfg.key)
        if row is None:
            db.add(Top20Strategy(
                key=cfg.key, name=cfg.name, description=cfg.description,
                starting_bankroll=STARTING_BANKROLL, fractional_kelly=cfg.fractional_kelly,
                params=cfg.to_params(),
            ))
            changed = True
        else:
            # keep config text/params fresh without disturbing results
            if row.name != cfg.name or row.description != cfg.description:
                row.name, row.description = cfg.name, cfg.description
                changed = True
            row.params = cfg.to_params()
            row.fractional_kelly = cfg.fractional_kelly
    if changed:
        db.commit()
    return list(db.scalars(select(Top20Strategy).order_by(Top20Strategy.id)).all())


def _wallet_rank(db: Session) -> dict[int, int]:
    """Map wallet_id -> rank (0 = best) by copyability score, excluding
    insufficient_data wallets."""
    rows = db.scalars(
        select(WalletCandidate)
        .where(WalletCandidate.classification != "insufficient_data")
        .order_by(WalletCandidate.copyability_score.desc())
    ).all()
    return {c.wallet_id: i for i, c in enumerate(rows)}


def _realized(db: Session, strategy_id: int) -> float:
    val = db.scalar(
        select(func.coalesce(func.sum(Top20Trade.realized_pnl), 0.0)).where(
            Top20Trade.strategy_id == strategy_id, Top20Trade.status == "closed"
        )
    )
    return float(val or 0.0)


def evaluate_signals(db: Session, settings: dict | None = None) -> dict:
    """Evaluate every not-yet-seen PaperSignal against all 20 strategies and
    enter paper trades where filters + sizing permit. PAPER ONLY."""
    strategies = ensure_strategies(db)
    min_wm = min((s.last_signal_id for s in strategies), default=0)
    signals = db.scalars(
        select(PaperSignal).where(PaperSignal.id > min_wm).order_by(PaperSignal.id)
    ).all()
    if not signals:
        return {"evaluated": 0, "entered": 0, "signals": 0}

    rank = _wallet_rank(db)
    stats = {s.wallet_id: s for s in db.scalars(select(WalletStat)).all()}
    cands = {c.wallet_id: c for c in db.scalars(select(WalletCandidate)).all()}
    wallets = {w.id: w for w in db.scalars(select(Wallet)).all()}
    mids = {s.market_id for s in signals}
    markets = {m.id: m for m in db.scalars(select(Market).where(Market.id.in_(mids))).all()}
    now = datetime.utcnow()

    total_eval = total_entered = 0
    last_id = signals[-1].id
    for strat in strategies:
        cfg = CONFIG_BY_KEY[strat.key]
        bankroll = strat.starting_bankroll + _realized(db, strat.id)
        # current per-market exposure (open positions) + signals already entered
        exposure: dict[str, float] = {}
        for t in db.scalars(select(Top20Trade).where(
                Top20Trade.strategy_id == strat.id, Top20Trade.status == "open")).all():
            exposure[t.market_id] = exposure.get(t.market_id, 0.0) + t.stake
        seen = set(db.scalars(
            select(Top20Trade.signal_id).where(Top20Trade.strategy_id == strat.id)
        ).all())

        for sig in signals:
            if sig.id <= strat.last_signal_id:
                continue
            strat.signals_evaluated += 1
            total_eval += 1
            if sig.id in seen:                       # duplicate guard
                continue
            wallet = wallets.get(sig.wallet_id)
            market = markets.get(sig.market_id)
            if not (wallet and market) or market.resolved:
                continue
            stat = stats.get(sig.wallet_id)
            cand = cands.get(sig.wallet_id)
            ctx = SignalContext(
                rank=rank.get(sig.wallet_id),
                classification=cand.classification if cand else None,
                confidence=_safe(sig.confidence, 0.0),
                edge=_safe(sig.edge_estimate, 0.0),
                liquidity=_safe(market.liquidity, 0.0),
                age_min=(now - sig.created_at).total_seconds() / 60.0,
            )
            if not passes_filters(cfg, ctx):
                continue
            p = estimate_probability(sig.observed_price, sig.edge_estimate,
                                     stat.win_rate if stat else None, sig.confidence)
            res = kelly_stake(sig.observed_price, p, bankroll, cfg,
                              exposure.get(sig.market_id, 0.0))
            if res.stake is None:
                continue
            db.add(Top20Trade(
                strategy_id=strat.id, signal_id=sig.id, wallet_address=wallet.address,
                market_id=market.id, market_question=market.question or "",
                outcome=sig.outcome, side=sig.side or "buy",
                entry_price=round(clamp(_safe(sig.observed_price, 0.5), PRICE_FLOOR, PRICE_CEIL), 4),
                size_shares=res.shares, stake=res.stake,
                estimated_probability=round(p, 4), kelly_fraction=res.kelly_fraction,
                fractional_kelly_used=cfg.fractional_kelly, sizing_reason=res.reason,
                entry_time=now, status="open",
                current_price=round(clamp(_safe(sig.observed_price, 0.5), PRICE_FLOOR, PRICE_CEIL), 4),
            ))
            exposure[sig.market_id] = exposure.get(sig.market_id, 0.0) + res.stake
            seen.add(sig.id)
            strat.trades_entered += 1
            total_entered += 1
        strat.last_signal_id = last_id
    db.commit()
    return {"evaluated": total_eval, "entered": total_entered, "signals": len(signals)}


def settle_and_mark(db: Session) -> dict:
    """Close paper positions whose market has resolved (payout 1.0/0.0) and
    mark the rest to the current market price. PAPER ONLY."""
    open_trades = db.scalars(select(Top20Trade).where(Top20Trade.status == "open")).all()
    closed = marked = 0
    now = datetime.utcnow()
    for t in open_trades:
        market = db.get(Market, t.market_id)
        if market is None:
            continue
        if market.resolved and market.resolved_outcome is not None:
            won = market.resolved_outcome == t.outcome
            exit_price = 1.0 if won else 0.0
            t.exit_price = exit_price
            t.current_price = exit_price
            t.realized_pnl = round(t.size_shares * exit_price - t.stake, 2)
            t.unrealized_pnl = 0.0
            t.status = "closed"
            t.closed_at = now
            closed += 1
        else:
            price = market.price_for(t.outcome)
            if price is not None:
                t.current_price = round(float(price), 4)
                t.unrealized_pnl = round(t.size_shares * float(price) - t.stake, 2)
                marked += 1
    db.commit()
    return {"closed": closed, "marked": marked}


def snapshot(db: Session) -> int:
    """Record one equity snapshot per strategy (for drawdown / curve)."""
    strategies = db.scalars(select(Top20Strategy)).all()
    n = 0
    for strat in strategies:
        agg = _aggregate(db, strat)
        db.add(Top20Snapshot(
            strategy_id=strat.id, bankroll=agg["bankroll"], equity=agg["equity"],
            realized_pnl=agg["realized_pnl"], unrealized_pnl=agg["unrealized_pnl"],
            open_positions=agg["open_positions"],
        ))
        n += 1
    db.commit()
    return n


def run_cycle(db: Session, settings: dict | None = None) -> dict:
    """Full TOP 20 tick: evaluate new signals, settle/mark, snapshot."""
    ev = evaluate_signals(db, settings)
    sm = settle_and_mark(db)
    snapshot(db)
    return {**ev, **sm}


# ---------------------------------------------------------------------------
# Read models (stats for the API / page)
# ---------------------------------------------------------------------------
def _aggregate(db: Session, strat: Top20Strategy) -> dict:
    trades = db.scalars(select(Top20Trade).where(Top20Trade.strategy_id == strat.id)).all()
    closed = [t for t in trades if t.status == "closed"]
    open_ = [t for t in trades if t.status == "open"]
    realized = round(sum(t.realized_pnl for t in closed), 2)
    unrealized = round(sum(t.unrealized_pnl for t in open_), 2)
    bankroll = round(strat.starting_bankroll + realized, 2)
    equity = round(bankroll + unrealized, 2)
    wins = [t for t in closed if t.realized_pnl > 0]
    win_rate = round(len(wins) / len(closed), 4) if closed else 0.0
    returns = [t.realized_pnl / t.stake for t in closed if t.stake]
    avg_return = round(sum(returns) / len(returns), 4) if returns else 0.0
    last_trade = max((t.entry_time for t in trades), default=None)
    return {
        "bankroll": bankroll, "equity": equity, "realized_pnl": realized,
        "unrealized_pnl": unrealized, "total_pnl": round(realized + unrealized, 2),
        "open_positions": len(open_), "closed_positions": len(closed),
        "trades_entered": len(trades), "win_rate": win_rate,
        "avg_return_per_trade": avg_return,
        "max_drawdown": _max_drawdown(db, strat.id),
        "roi": round((equity - strat.starting_bankroll) / strat.starting_bankroll, 4),
        "last_trade_at": last_trade,
    }


def _max_drawdown(db: Session, strategy_id: int) -> float:
    """Max peak-to-trough drawdown (fraction 0..1) from the equity snapshots."""
    eq = db.scalars(
        select(Top20Snapshot.equity).where(Top20Snapshot.strategy_id == strategy_id)
        .order_by(Top20Snapshot.timestamp)
    ).all()
    peak = None
    mdd = 0.0
    for e in eq:
        if peak is None or e > peak:
            peak = e
        if peak and peak > 0:
            mdd = max(mdd, (peak - e) / peak)
    return round(mdd, 4)


def _top_wallets(db: Session, strategy_id: int, limit: int = 5) -> list[dict]:
    trades = db.scalars(select(Top20Trade).where(Top20Trade.strategy_id == strategy_id)).all()
    agg: dict[str, dict] = {}
    for t in trades:
        a = agg.setdefault(t.wallet_address, {"address": t.wallet_address, "trades": 0, "pnl": 0.0})
        a["trades"] += 1
        a["pnl"] += t.realized_pnl + t.unrealized_pnl
    ranked = sorted(agg.values(), key=lambda a: a["trades"], reverse=True)
    for a in ranked:
        a["pnl"] = round(a["pnl"], 2)
    return ranked[:limit]


def _trade_dict(t: Top20Trade) -> dict:
    return {
        "id": t.id, "strategy_id": t.strategy_id, "signal_id": t.signal_id,
        "wallet_address": t.wallet_address, "market_id": t.market_id,
        "market_question": t.market_question, "outcome": t.outcome, "side": t.side,
        "entry_price": t.entry_price, "size_shares": t.size_shares, "stake": t.stake,
        "estimated_probability": t.estimated_probability, "kelly_fraction": t.kelly_fraction,
        "fractional_kelly_used": t.fractional_kelly_used, "sizing_reason": t.sizing_reason,
        "entry_time": t.entry_time.isoformat() if t.entry_time else None,
        "status": t.status, "current_price": t.current_price, "exit_price": t.exit_price,
        "realized_pnl": t.realized_pnl, "unrealized_pnl": t.unrealized_pnl,
        "closed_at": t.closed_at.isoformat() if t.closed_at else None,
    }


def _strategy_summary(db: Session, strat: Top20Strategy) -> dict:
    agg = _aggregate(db, strat)
    return {
        "id": strat.id, "key": strat.key, "name": strat.name,
        "description": strat.description, "active": strat.active,
        "starting_bankroll": strat.starting_bankroll,
        "fractional_kelly": strat.fractional_kelly, "params": strat.params,
        "signals_evaluated": strat.signals_evaluated, **agg,
        "last_trade_at": agg["last_trade_at"].isoformat() if agg["last_trade_at"] else None,
        "paper_only": True,
    }


def list_strategies(db: Session) -> list[dict]:
    ensure_strategies(db)
    return [_strategy_summary(db, s)
            for s in db.scalars(select(Top20Strategy).order_by(Top20Strategy.id)).all()]


def strategy_detail(db: Session, strategy_id: int, recent: int = 20) -> dict | None:
    strat = db.get(Top20Strategy, strategy_id)
    if strat is None:
        return None
    out = _strategy_summary(db, strat)
    trades = db.scalars(
        select(Top20Trade).where(Top20Trade.strategy_id == strat.id)
        .order_by(Top20Trade.entry_time.desc()).limit(recent)
    ).all()
    out["recent_trades"] = [_trade_dict(t) for t in trades]
    out["top_wallets"] = _top_wallets(db, strat.id)
    out["equity_curve"] = [
        {"t": s.timestamp.isoformat(), "equity": s.equity}
        for s in db.scalars(
            select(Top20Snapshot).where(Top20Snapshot.strategy_id == strat.id)
            .order_by(Top20Snapshot.timestamp)
        ).all()
    ]
    return out


def list_trades(db: Session, strategy_id: int | None = None, limit: int = 100) -> list[dict]:
    q = select(Top20Trade).order_by(Top20Trade.entry_time.desc()).limit(limit)
    if strategy_id is not None:
        q = q.where(Top20Trade.strategy_id == strategy_id)
    return [_trade_dict(t) for t in db.scalars(q).all()]


def reset_paper(db: Session) -> dict:
    """Wipe all TOP 20 paper trades + snapshots and reset counters/watermarks.
    PAPER-ONLY dev/admin action (mirrors the existing mock-seed reset)."""
    n_trades = db.query(Top20Trade).delete()
    n_snaps = db.query(Top20Snapshot).delete()
    for strat in db.scalars(select(Top20Strategy)).all():
        strat.signals_evaluated = 0
        strat.trades_entered = 0
        strat.last_signal_id = 0
    db.commit()
    return {"trades_deleted": int(n_trades or 0), "snapshots_deleted": int(n_snaps or 0)}
