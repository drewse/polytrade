"""
Live-money execution validation layer.

PURPOSE: validate infrastructure (auth, order placement, fills, slippage,
settlement, bookkeeping, DB sync, P&L accounting) with the SMALLEST possible
capital — NOT to chase returns. Our probability estimator is not yet proven
better than market-implied probability (audit: Brier 0.245 vs market 0.212), so
sizing makes NO probabilistic claim: it is FIXED FRACTIONAL (2% of bankroll),
not Kelly.

SAFETY MODEL (defense in depth):
  1. LIVE_TRADING_ENABLED=false by default — nothing live happens otherwise.
  2. Even when enabled, the default executor is DRY-RUN (simulated fills) — it
     exercises the entire pipeline (sizing, limits, logging, reconciliation,
     P&L) WITHOUT touching the chain. Real submission requires LIVE_EXECUTOR=
     polymarket AND a completed, key-handling adapter (left as a documented seam
     — see PolymarketExecutor — because a bug there loses real money).
  3. Hard limits + a halt latch that requires manual /api/live/resume.

This module never stores or logs a private key. Credentials, if ever used, come
from the environment at runtime and belong to the operator.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import LiveExecution, LiveState, Market


# ---------------------------------------------------------------------------
# Config (env). Safe production defaults; the switch is OFF.
# ---------------------------------------------------------------------------
def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class LiveConfig:
    enabled: bool
    executor: str            # dry_run | polymarket
    sizing: str              # fixed_fractional (only conservative method for now)
    position_pct: float      # fraction of bankroll per position (2%)
    min_stake: float         # exchange minimum / dust floor
    max_positions: int       # max simultaneous open positions
    max_wallet_pct: float    # max exposure to one copied wallet
    max_market_pct: float    # max exposure to one market
    max_daily_loss_pct: float
    max_weekly_loss_pct: float


def get_config() -> LiveConfig:
    return LiveConfig(
        enabled=_truthy(os.getenv("LIVE_TRADING_ENABLED", "false")),
        executor=os.getenv("LIVE_EXECUTOR", "dry_run").strip().lower(),
        sizing=os.getenv("LIVE_SIZING", "fixed_fractional").strip().lower(),
        position_pct=float(os.getenv("LIVE_POSITION_PCT", "0.02")),
        min_stake=float(os.getenv("LIVE_MIN_STAKE", "1.0")),
        max_positions=int(os.getenv("LIVE_MAX_POSITIONS", "5")),
        max_wallet_pct=float(os.getenv("LIVE_MAX_WALLET_PCT", "0.10")),
        max_market_pct=float(os.getenv("LIVE_MAX_MARKET_PCT", "0.05")),
        max_daily_loss_pct=float(os.getenv("LIVE_MAX_DAILY_LOSS_PCT", "0.10")),
        max_weekly_loss_pct=float(os.getenv("LIVE_MAX_WEEKLY_LOSS_PCT", "0.20")),
    )


# ---------------------------------------------------------------------------
# Conservative sizing — fixed fractional, exposure-capped. PURE + tested.
# ---------------------------------------------------------------------------
def conservative_stake(bankroll: float, cfg: LiveConfig,
                       wallet_exposure: float = 0.0, market_exposure: float = 0.0) -> float | None:
    """2% of CURRENT bankroll, clamped by per-wallet (10%) and per-market (5%)
    caps. Returns None if no room for the minimum stake. No Kelly, no probability
    dependence: this can't be wrong about an edge it never claims."""
    if bankroll <= 0:
        return None
    target = cfg.position_pct * bankroll
    wallet_room = cfg.max_wallet_pct * bankroll - wallet_exposure
    market_room = cfg.max_market_pct * bankroll - market_exposure
    stake = min(target, wallet_room, market_room)
    if stake < cfg.min_stake:
        return None
    return round(stake, 2)


# ---------------------------------------------------------------------------
# Live state + risk gate
# ---------------------------------------------------------------------------
def get_state(db: Session) -> LiveState:
    st = db.get(LiveState, 1)
    if st is None:
        start = float(os.getenv("LIVE_STARTING_BANKROLL", "100.0"))
        st = LiveState(id=1, starting_bankroll=start, bankroll=start)
        db.add(st)
        db.commit()
    return st


def _open(db: Session) -> list[LiveExecution]:
    return list(db.scalars(select(LiveExecution).where(LiveExecution.status == "open")).all())


def _realized_since(db: Session, since: datetime) -> float:
    val = db.scalar(select(func.coalesce(func.sum(LiveExecution.realized_pnl), 0.0)).where(
        LiveExecution.status == "closed", LiveExecution.closed_at >= since))
    return float(val or 0.0)


def _wallet_exposure(open_: list, addr: str) -> float:
    return sum(e.size_usd for e in open_ if e.wallet_address == addr)


def _market_exposure(open_: list, mid: str) -> float:
    return sum(e.size_usd for e in open_ if e.market_id == mid)


def check_can_open(db: Session, cfg: LiveConfig, *, wallet: str, market_id: str) -> tuple[bool, str]:
    """All hard pre-trade gates. Returns (allowed, reason)."""
    st = get_state(db)
    if not cfg.enabled:
        return False, "LIVE_TRADING_ENABLED is false"
    if st.halted:
        return False, f"trading halted: {st.halt_reason}"
    open_ = _open(db)
    if len(open_) >= cfg.max_positions:
        return False, f"max simultaneous positions ({cfg.max_positions}) reached"
    # daily / weekly loss limits trip the halt latch
    now = datetime.utcnow()
    day_pnl = _realized_since(db, now.replace(hour=0, minute=0, second=0, microsecond=0))
    week_pnl = _realized_since(db, now - timedelta(days=7))
    if day_pnl <= -cfg.max_daily_loss_pct * st.starting_bankroll:
        _trip_halt(db, st, f"daily loss limit ({cfg.max_daily_loss_pct*100:.0f}%) hit")
        return False, "daily loss limit hit — halted"
    if week_pnl <= -cfg.max_weekly_loss_pct * st.starting_bankroll:
        _trip_halt(db, st, f"weekly loss limit ({cfg.max_weekly_loss_pct*100:.0f}%) hit")
        return False, "weekly loss limit hit — halted"
    return True, "ok"


def _trip_halt(db: Session, st: LiveState, reason: str) -> None:
    st.halted = True
    st.halt_reason = reason
    st.halted_at = datetime.utcnow()
    db.commit()


def resume(db: Session) -> dict:
    """Manual intervention required after a tripped limit before resuming."""
    st = get_state(db)
    st.halted = False
    st.halt_reason = None
    db.commit()
    return {"halted": False, "resumed_at": datetime.utcnow().isoformat()}


def halt(db: Session, reason: str = "manual") -> dict:
    st = get_state(db)
    _trip_halt(db, st, reason)
    return {"halted": True, "reason": reason}


# ---------------------------------------------------------------------------
# Executors
# ---------------------------------------------------------------------------
@dataclass
class Fill:
    fill_price: float
    fees: float
    order_latency_ms: float
    confirm_latency_ms: float


class DryRunExecutor:
    """Simulates a fill at the expected price (zero modeled slippage/fees) and
    runs the FULL bookkeeping pipeline — validates everything except the on-chain
    submission. This is the default even when LIVE_TRADING_ENABLED=true."""

    name = "dry_run"

    def place(self, *, market_id: str, outcome: str, side: str, price: float,
              size_usd: float) -> Fill:
        return Fill(fill_price=price, fees=0.0, order_latency_ms=0.0, confirm_latency_ms=0.0)


class PolymarketExecutor:
    """REAL order submission — DELIBERATELY NOT IMPLEMENTED HERE.

    Completing this is the operator's responsibility because it requires handling
    a funded wallet's private key and signing real orders; a mistake loses money.
    To finish it safely:
      1. `pip install py-clob-client`; load the API/private key from the
         environment ONLY (never commit it).
      2. In `place()`: resolve the CLOB token id for (market_id, outcome), respect
         the market's tick size + min order size, post a marketable limit order
         (FOK/IOC), and return the actual fill price, fees, and latencies.
      3. Add an idempotent client order id so a retry never double-submits.
      4. Verify on Polymarket's TESTNET / with $1 first, then reconcile.
    Until then it refuses to run so it can never silently place a bad order.
    """

    name = "polymarket"

    def __init__(self) -> None:
        # credentials are read at runtime by the operator's adapter, never stored here
        self._key_present = bool(os.getenv("POLYMARKET_PRIVATE_KEY"))

    def place(self, **_kw) -> Fill:
        raise NotImplementedError(
            "PolymarketExecutor.place is intentionally unimplemented. Complete the "
            "CLOB adapter (see docstring) and verify on testnet before enabling.")


def get_executor(cfg: LiveConfig):
    if cfg.executor == "polymarket":
        return PolymarketExecutor()
    return DryRunExecutor()


# ---------------------------------------------------------------------------
# Order pipeline (gated) + settlement + reconciliation
# ---------------------------------------------------------------------------
def process_signal(db: Session, *, strategy_key: str, wallet: str, signal_id: int | None,
                   market_id: str, market_question: str, outcome: str, price: float,
                   entry_reason: str) -> LiveExecution | None:
    """Size + gate + (dry-run/live) place + log one live order. Idempotent per
    (strategy, signal). Returns the LiveExecution, or None if skipped/blocked."""
    cfg = get_config()
    idem = f"{strategy_key}:{signal_id}"
    # duplicate-order prevention
    if db.scalar(select(LiveExecution).where(LiveExecution.idempotency_key == idem)):
        return None
    ok, reason = check_can_open(db, cfg, wallet=wallet, market_id=market_id)
    st = get_state(db)
    if not ok:
        db.add(LiveExecution(
            idempotency_key=idem, executor=cfg.executor, strategy_key=strategy_key,
            wallet_address=wallet, signal_id=signal_id, market_id=market_id,
            market_question=market_question, outcome=outcome, side="buy",
            expected_price=round(price, 4), size_usd=0.0, status="rejected",
            entry_reason=entry_reason, exit_reason=reason, bankroll_before=st.bankroll))
        db.commit()
        return None
    open_ = _open(db)
    stake = conservative_stake(st.bankroll, cfg,
                               wallet_exposure=_wallet_exposure(open_, wallet),
                               market_exposure=_market_exposure(open_, market_id))
    if stake is None:
        return None  # exposure caps leave no room
    t0 = datetime.utcnow()
    fill = get_executor(cfg).place(market_id=market_id, outcome=outcome, side="buy",
                                   price=price, size_usd=stake)
    pc = max(0.01, min(0.99, fill.fill_price))
    ex = LiveExecution(
        idempotency_key=idem, executor=cfg.executor, strategy_key=strategy_key,
        wallet_address=wallet, signal_id=signal_id, market_id=market_id,
        market_question=market_question, outcome=outcome, side="buy",
        expected_price=round(price, 4), fill_price=round(fill.fill_price, 4),
        slippage=round(fill.fill_price - price, 4), fees=round(fill.fees, 4),
        size_usd=stake, shares=round(stake / pc, 4),
        order_latency_ms=fill.order_latency_ms, confirm_latency_ms=fill.confirm_latency_ms,
        status="open", entry_reason=entry_reason, bankroll_before=st.bankroll)
    db.add(ex)
    db.commit()
    return ex


def settle_live(db: Session) -> dict:
    """Close open live orders whose market has resolved; update realized P&L and
    the live bankroll (paper-only accounting mirrors what the chain settlement
    would produce)."""
    st = get_state(db)
    closed = 0
    now = datetime.utcnow()
    for ex in _open(db):
        m = db.get(Market, ex.market_id)
        if not (m and m.resolved and m.resolved_outcome is not None):
            continue
        won = m.resolved_outcome == ex.outcome
        payout = ex.shares * (1.0 if won else 0.0)
        ex.realized_pnl = round(payout - ex.size_usd - ex.fees, 2)
        ex.status = "closed"
        ex.exit_reason = "resolved"
        ex.closed_at = now
        ex.settled_at = m.resolved_at or now
        st.bankroll = round(st.bankroll + ex.realized_pnl, 2)
        ex.bankroll_after = st.bankroll
        closed += 1
    db.commit()
    return {"closed": closed, "bankroll": st.bankroll}


def process_new_signals(db: Session, limit: int = 20) -> dict:
    """Worker hook. ALWAYS settles existing live positions (monitoring continues
    even when disabled). Only PLACES new orders when LIVE_TRADING_ENABLED — and
    even then via the dry-run executor unless a real adapter is wired in."""
    settle_live(db)
    cfg = get_config()
    if not cfg.enabled:
        return {"enabled": False, "placed": 0}
    from .models import PaperSignal, Wallet
    recent = db.scalars(select(PaperSignal).where(
        PaperSignal.created_at >= datetime.utcnow() - timedelta(hours=2))
        .order_by(PaperSignal.created_at.desc()).limit(limit)).all()
    done = set(db.scalars(select(LiveExecution.signal_id)).all())
    strategy_key = os.getenv("LIVE_STRATEGY", "top_decile_edge")
    placed = 0
    for s in recent:
        if s.id in done:
            continue
        w = db.get(Wallet, s.wallet_id)
        m = db.get(Market, s.market_id)
        if not (w and m) or m.resolved:
            continue
        ex = process_signal(
            db, strategy_key=strategy_key, wallet=w.address, signal_id=s.id,
            market_id=s.market_id, market_question=m.question or "", outcome=s.outcome,
            price=float(s.observed_price or 0.5),
            entry_reason=f"copy {w.address[:10]} conf={s.confidence:.0f} edge={s.edge_estimate}")
        if ex:
            placed += 1
    return {"enabled": True, "placed": placed}


def reconcile(db: Session, reported_balance: float, tolerance: float = 0.50) -> dict:
    """Compare our computed bankroll against the venue-reported balance. Any drift
    beyond tolerance means our bookkeeping diverged from reality — investigate
    before trading more."""
    st = get_state(db)
    realized = _realized_since(db, datetime.min)
    computed = round(st.starting_bankroll + realized, 2)
    open_exposure = round(sum(e.size_usd for e in _open(db)), 2)
    # reported should ~= computed_cash; cash = computed - open_exposure
    expected_cash = round(computed - open_exposure, 2)
    drift = round(reported_balance - expected_cash, 2)
    return {
        "starting_bankroll": st.starting_bankroll, "computed_equity": computed,
        "open_exposure": open_exposure, "expected_cash": expected_cash,
        "reported_balance": round(reported_balance, 2), "drift": drift,
        "reconciled": abs(drift) <= tolerance,
    }


def status(db: Session) -> dict:
    cfg = get_config()
    st = get_state(db)
    open_ = _open(db)
    now = datetime.utcnow()
    day_pnl = _realized_since(db, now.replace(hour=0, minute=0, second=0, microsecond=0))
    week_pnl = _realized_since(db, now - timedelta(days=7))
    total_closed = db.scalar(select(func.count()).select_from(LiveExecution).where(
        LiveExecution.status == "closed"))
    return {
        "paper_trading_default": True,
        "live_trading_enabled": cfg.enabled,
        "executor": cfg.executor,
        "real_submission_implemented": False,   # PolymarketExecutor is a guarded stub
        "sizing": {"method": cfg.sizing, "position_pct": cfg.position_pct,
                   "min_stake": cfg.min_stake},
        "limits": {"max_positions": cfg.max_positions, "max_wallet_pct": cfg.max_wallet_pct,
                   "max_market_pct": cfg.max_market_pct,
                   "max_daily_loss_pct": cfg.max_daily_loss_pct,
                   "max_weekly_loss_pct": cfg.max_weekly_loss_pct},
        "state": {"starting_bankroll": st.starting_bankroll, "bankroll": st.bankroll,
                  "halted": st.halted, "halt_reason": st.halt_reason},
        "open_positions": len(open_), "open_exposure": round(sum(e.size_usd for e in open_), 2),
        "day_pnl": round(day_pnl, 2), "week_pnl": round(week_pnl, 2),
        "closed_trades": int(total_closed or 0),
    }


def list_executions(db: Session, limit: int = 100) -> list[dict]:
    rows = db.scalars(select(LiveExecution).order_by(LiveExecution.created_at.desc()).limit(limit)).all()
    return [{
        "id": e.id, "created_at": e.created_at.isoformat() if e.created_at else None,
        "executor": e.executor, "strategy": e.strategy_key, "wallet": e.wallet_address,
        "signal_id": e.signal_id, "market_id": e.market_id, "market_question": e.market_question,
        "outcome": e.outcome, "side": e.side, "expected_price": e.expected_price,
        "fill_price": e.fill_price, "slippage": e.slippage, "fees": e.fees, "size_usd": e.size_usd,
        "shares": e.shares, "order_latency_ms": e.order_latency_ms,
        "confirm_latency_ms": e.confirm_latency_ms, "status": e.status,
        "entry_reason": e.entry_reason, "exit_reason": e.exit_reason,
        "realized_pnl": e.realized_pnl, "bankroll_before": e.bankroll_before,
        "bankroll_after": e.bankroll_after,
    } for e in rows]
