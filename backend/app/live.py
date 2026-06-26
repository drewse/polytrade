"""
Live-money execution test layer.

Goal: validate REAL Polymarket execution (auth, order placement, fills, slippage,
settlement, bookkeeping, reconciliation) with the smallest possible capital —
NOT to chase returns. Sizing is tiny FIXED DOLLAR (not Kelly): the audit showed
our probability model is worse than market price, so we make no probabilistic
sizing claim.

DEFENSE IN DEPTH (a single misconfig must not place a bad/large trade):
  1. LIVE_TRADING_ENABLED=false by default.
  2. LIVE_EXECUTOR=dry_run by default (simulated fills, full bookkeeping, zero
     capital). Real orders require LIVE_EXECUTOR=polymarket AND a private key.
  3. Hard ABSOLUTE-DOLLAR caps: $2/position, $40 total risk, $4/market, $8/wallet,
     $10 daily-loss stop, $40 total-loss stop, max 10 open.
  4. LIVE_MAX_ORDERS=1 -> place exactly one order, then auto-halt for manual review.
  5. Pre-trade slippage gate (skip if the book moved > LIVE_MAX_SLIPPAGE_PCT).
  6. Idempotency: one order per (strategy, signal).
  7. FAIL CLOSED: any error in the real executor -> reject the order, never retry-
     place. The first $1-$2 order IS the live verification.

This module never stores or logs the private key. It is read from the environment
at order time only.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import LiveExecution, LiveState, Market


class ExecutionRejected(Exception):
    """A pre-trade/venue check refused this order (logged as 'rejected')."""


def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class LiveConfig:
    enabled: bool
    executor: str            # dry_run | polymarket
    strategy: str            # which paper strategy's signals to copy
    position_usd: float      # fixed $ per position
    min_stake: float         # venue minimum / dust floor
    max_total_risk: float    # cap on total open exposure
    max_positions: int
    max_per_market: float
    max_per_wallet: float
    daily_loss_stop: float   # absolute $
    total_loss_stop: float   # absolute $
    max_orders: int          # 0 = unlimited; 1 = one-order test then auto-halt
    max_slippage_pct: float
    min_edge: float          # only copy signals with at least this edge
    min_confidence: float


def get_config() -> LiveConfig:
    return LiveConfig(
        enabled=_truthy(os.getenv("LIVE_TRADING_ENABLED", "false")),
        executor=os.getenv("LIVE_EXECUTOR", "dry_run").strip().lower(),
        strategy=os.getenv("LIVE_STRATEGY", "highest_edge"),   # 'Top-Decile Edge'
        position_usd=float(os.getenv("LIVE_POSITION_USD", "2.0")),
        min_stake=float(os.getenv("LIVE_MIN_STAKE", "1.0")),
        max_total_risk=float(os.getenv("LIVE_MAX_TOTAL_RISK", "40.0")),
        max_positions=int(os.getenv("LIVE_MAX_POSITIONS", "10")),
        max_per_market=float(os.getenv("LIVE_MAX_PER_MARKET", "4.0")),
        max_per_wallet=float(os.getenv("LIVE_MAX_PER_WALLET", "8.0")),
        daily_loss_stop=float(os.getenv("LIVE_DAILY_LOSS_STOP", "10.0")),
        total_loss_stop=float(os.getenv("LIVE_TOTAL_LOSS_STOP", "40.0")),
        max_orders=int(os.getenv("LIVE_MAX_ORDERS", "1")),
        max_slippage_pct=float(os.getenv("LIVE_MAX_SLIPPAGE_PCT", "0.03")),
        min_edge=float(os.getenv("LIVE_MIN_EDGE", "0.05")),
        min_confidence=float(os.getenv("LIVE_MIN_CONFIDENCE", "65")),
    )


# ---------------------------------------------------------------------------
# Conservative sizing — tiny FIXED DOLLAR, absolute-cap clamped. PURE + tested.
# ---------------------------------------------------------------------------
def conservative_stake(cfg: LiveConfig, *, available_cash: float, total_open: float,
                       wallet_exposure: float, market_exposure: float) -> float | None:
    """Fixed $position_usd, clamped by total-risk, per-market, per-wallet caps and
    available cash. No leverage, no compounding, no Kelly. None if no room."""
    stake = min(
        cfg.position_usd,
        cfg.max_total_risk - total_open,
        cfg.max_per_market - market_exposure,
        cfg.max_per_wallet - wallet_exposure,
        available_cash,
    )
    if stake < cfg.min_stake:
        return None
    return round(stake, 2)


# ---------------------------------------------------------------------------
# State + risk gate
# ---------------------------------------------------------------------------
def get_state(db: Session) -> LiveState:
    st = db.get(LiveState, 1)
    if st is None:
        start = float(os.getenv("LIVE_STARTING_BANKROLL", "40.0"))
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


def _realized_total(db: Session) -> float:
    val = db.scalar(select(func.coalesce(func.sum(LiveExecution.realized_pnl), 0.0)).where(
        LiveExecution.status == "closed"))
    return float(val or 0.0)


def _order_count(db: Session, executor: str) -> int:
    """Count of non-rejected orders placed by the given executor (for LIVE_MAX_ORDERS)."""
    return int(db.scalar(select(func.count()).select_from(LiveExecution).where(
        LiveExecution.executor == executor, LiveExecution.status != "rejected")) or 0)


def _trip_halt(db: Session, st: LiveState, reason: str) -> None:
    st.halted = True
    st.halt_reason = reason
    st.halted_at = datetime.utcnow()
    db.commit()


def check_can_open(db: Session, cfg: LiveConfig, *, wallet: str, market_id: str) -> tuple[bool, str]:
    """All hard pre-trade gates (absolute-dollar)."""
    st = get_state(db)
    if not cfg.enabled:
        return False, "LIVE_TRADING_ENABLED is false"
    if st.halted:
        return False, f"trading halted: {st.halt_reason}"
    open_ = _open(db)
    if len(open_) >= cfg.max_positions:
        return False, f"max open positions ({cfg.max_positions}) reached"
    if cfg.max_orders > 0 and _order_count(db, cfg.executor) >= cfg.max_orders:
        _trip_halt(db, st, f"max orders ({cfg.max_orders}) reached")
        return False, "max orders reached — halted"
    now = datetime.utcnow()
    day_pnl = _realized_since(db, now.replace(hour=0, minute=0, second=0, microsecond=0))
    if day_pnl <= -cfg.daily_loss_stop:
        _trip_halt(db, st, f"daily loss stop (${cfg.daily_loss_stop:.0f}) hit")
        return False, "daily loss stop hit — halted"
    if _realized_total(db) <= -cfg.total_loss_stop:
        _trip_halt(db, st, f"total loss stop (${cfg.total_loss_stop:.0f}) hit")
        return False, "total loss stop hit — halted"
    return True, "ok"


def set_bankroll(db: Session, amount: float) -> dict:
    """Align the tracked starting/current bankroll with the ACTUAL funded balance.
    Only allowed with no executions yet (clean slate) so it can't rewrite history."""
    if db.scalar(select(func.count()).select_from(LiveExecution)):
        return {"ok": False, "error": "executions exist; cannot reset bankroll"}
    st = get_state(db)
    st.starting_bankroll = round(amount, 2)
    st.bankroll = round(amount, 2)
    db.commit()
    return {"ok": True, "starting_bankroll": st.starting_bankroll, "bankroll": st.bankroll}


def resume(db: Session) -> dict:
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
    limit_price: float
    fees: float
    order_id: str | None
    order_latency_ms: float
    confirm_latency_ms: float


class DryRunExecutor:
    """Simulated fill at the expected price — full bookkeeping, zero capital."""
    name = "dry_run"

    def place(self, *, db, market, outcome, price, size_usd, cfg) -> Fill:
        return Fill(fill_price=price, limit_price=price, fees=0.0, order_id="dryrun",
                    order_latency_ms=0.0, confirm_latency_ms=0.0)


class PolymarketExecutor:
    """REAL order submission via the official py-clob-client (verified against
    v0.34.6). Fail-closed.

    AUTH IS TWO-TIER (both required):
      * L1 — the funded wallet PRIVATE KEY, used to SIGN orders.
      * L2 — Relayer/CLOB API credentials (api_key/api_secret/api_passphrase),
        used to AUTHENTICATE posting orders. If provided via env they're used
        directly; otherwise they are derived from the L1 key as a fallback.

    Required env (read at order time, never stored/logged):
      POLYMARKET_PRIVATE_KEY      funded wallet key (operator's)            [L1, required]
      POLYMARKET_API_KEY          Relayer API key                          [L2, recommended]
      POLYMARKET_API_SECRET       Relayer API secret                       [L2]
      POLYMARKET_API_PASSPHRASE   Relayer API passphrase                   [L2]
      POLYMARKET_CLOB_HOST        default https://clob.polymarket.com
      POLYMARKET_CHAIN_ID         default 137 (Polygon)
      POLYMARKET_SIGNATURE_TYPE   0=EOA (default), 1=email/magic, 2=proxy  [VERIFY for your wallet]
      POLYMARKET_FUNDER           proxy/funder address (only if signature_type != 0)
    """
    name = "polymarket"

    def place(self, *, db, market: Market, outcome: str, price: float, size_usd: float,
              cfg: LiveConfig) -> Fill:
        key = os.getenv("POLYMARKET_PRIVATE_KEY")
        if not key:
            raise ExecutionRejected("POLYMARKET_PRIVATE_KEY not set")
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY
        except Exception as exc:  # noqa: BLE001
            raise ExecutionRejected(f"py-clob-client not installed: {exc}")

        # resolve CLOB token id from OUR stored market metadata (no API guessing)
        token_id = _token_id_for(market, outcome)
        if not token_id:
            raise ExecutionRejected(f"no token_id for outcome '{outcome}'")

        host = os.getenv("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com")
        chain_id = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))
        sig_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))
        funder = os.getenv("POLYMARKET_FUNDER") or None
        # L2 creds: use the operator's exported Relayer credentials if present,
        # else derive them from the L1 key.
        api_key = os.getenv("POLYMARKET_API_KEY")
        api_secret = os.getenv("POLYMARKET_API_SECRET")
        api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE")
        provided = ApiCreds(api_key=api_key, api_secret=api_secret,
                            api_passphrase=api_passphrase) if (api_key and api_secret and api_passphrase) else None
        try:
            client = ClobClient(host, key=key, chain_id=chain_id, creds=provided,
                                signature_type=sig_type, funder=funder)
            if provided is None:                       # fall back to derive-from-key
                client.set_api_creds(client.create_or_derive_api_creds())
            client.assert_level_2_auth()               # fail now if L2 auth is incomplete
        except ExecutionRejected:
            raise
        except Exception as exc:  # noqa: BLE001  (auth failure -> reject, never place)
            raise ExecutionRejected(f"auth/init failed: {exc}")

        # current book — best ask for a buy; tick size comes from the book summary
        try:
            book = client.get_order_book(token_id)
            asks = getattr(book, "asks", None) or []
            best_ask = min((float(a.price) for a in asks), default=None)
            tick = float(getattr(book, "tick_size", None) or 0.01)
        except Exception as exc:  # noqa: BLE001
            raise ExecutionRejected(f"order book fetch failed: {exc}")
        if best_ask is None or best_ask <= 0:
            raise ExecutionRejected("no asks / empty book")

        # SLIPPAGE GATE — refuse if the market moved away from the signal price
        slip = (best_ask - price) / price if price > 0 else 1.0
        if slip > cfg.max_slippage_pct:
            raise ExecutionRejected(f"slippage {slip*100:.1f}% > {cfg.max_slippage_pct*100:.0f}%")

        # marketable limit at the ask, rounded UP to tick so it crosses
        import math
        limit_price = min(0.99, math.ceil(best_ask / tick) * tick)
        shares = round(size_usd / limit_price, 2)
        if shares * limit_price < cfg.min_stake or shares <= 0:
            raise ExecutionRejected(f"below min order size (${shares*limit_price:.2f})")

        t0 = datetime.utcnow()
        try:
            order = client.create_order(OrderArgs(price=round(limit_price, 4), size=shares,
                                                  side=BUY, token_id=token_id))
            # FOK = fill-or-kill: fully fills marketably or is killed (no resting risk)
            resp = client.post_order(order, OrderType.FOK)
        except Exception as exc:  # noqa: BLE001  (signing/submit failure -> reject, no retry)
            raise ExecutionRejected(f"submit failed: {exc}")
        latency = (datetime.utcnow() - t0).total_seconds() * 1000.0

        ok = bool(resp.get("success", True)) if isinstance(resp, dict) else True
        status_txt = (resp.get("status") if isinstance(resp, dict) else "") or ""
        if not ok or status_txt.lower() in ("unmatched", "cancelled", "canceled"):
            raise ExecutionRejected(f"order not filled (status={status_txt or 'unknown'})")
        order_id = (resp.get("orderID") or resp.get("orderId") or resp.get("id")) if isinstance(resp, dict) else None
        # marketable FOK fills at/within the limit; record the limit as the fill
        # price (operator should confirm exact avg fill via reconcile + venue UI).
        return Fill(fill_price=limit_price, limit_price=limit_price, fees=0.0,
                    order_id=str(order_id) if order_id else None,
                    order_latency_ms=round(latency, 1), confirm_latency_ms=round(latency, 1))


def _token_id_for(market: Market, outcome: str) -> str | None:
    try:
        outs = list(market.outcomes or [])
        toks = list(market.token_ids or [])
        if outcome in outs and len(toks) == len(outs):
            return str(toks[outs.index(outcome)])
    except Exception:  # noqa: BLE001
        pass
    return None


def get_executor(cfg: LiveConfig):
    return PolymarketExecutor() if cfg.executor == "polymarket" else DryRunExecutor()


# ---------------------------------------------------------------------------
# Order pipeline (gated)
# ---------------------------------------------------------------------------
def _wallet_exposure(open_, addr):
    return sum(e.size_usd for e in open_ if e.wallet_address == addr)


def _market_exposure(open_, mid):
    return sum(e.size_usd for e in open_ if e.market_id == mid)


def process_signal(db: Session, *, strategy_key: str, wallet: str, signal_id: int | None,
                   market: Market, outcome: str, price: float, entry_reason: str) -> LiveExecution | None:
    cfg = get_config()
    idem = f"{strategy_key}:{signal_id}"
    if db.scalar(select(LiveExecution).where(LiveExecution.idempotency_key == idem)):
        return None  # duplicate-order prevention
    st = get_state(db)

    def _reject(reason, size=0.0):
        db.add(LiveExecution(idempotency_key=idem, executor=cfg.executor, strategy_key=strategy_key,
                             wallet_address=wallet, signal_id=signal_id, market_id=market.id,
                             market_question=market.question or "", outcome=outcome, side="buy",
                             expected_price=round(price, 4), size_usd=size, status="rejected",
                             entry_reason=entry_reason, exit_reason=reason[:40],
                             bankroll_before=st.bankroll))
        db.commit()

    ok, reason = check_can_open(db, cfg, wallet=wallet, market_id=market.id)
    if not ok:
        _reject(reason)
        return None
    open_ = _open(db)
    available_cash = round(st.bankroll - sum(e.size_usd for e in open_), 2)
    stake = conservative_stake(cfg, available_cash=available_cash,
                               total_open=sum(e.size_usd for e in open_),
                               wallet_exposure=_wallet_exposure(open_, wallet),
                               market_exposure=_market_exposure(open_, market.id))
    if stake is None:
        _reject("no capital room within caps")
        return None
    try:
        fill = get_executor(cfg).place(db=db, market=market, outcome=outcome, price=price,
                                       size_usd=stake, cfg=cfg)
    except ExecutionRejected as exc:
        _reject(f"exec: {exc}", size=stake)
        return None
    except Exception as exc:  # noqa: BLE001  (unexpected -> reject + halt, fail closed)
        _reject(f"error: {exc}", size=stake)
        _trip_halt(db, st, f"executor error: {str(exc)[:60]}")
        return None

    pc = max(0.01, min(0.99, fill.fill_price))
    ex = LiveExecution(
        idempotency_key=idem, executor=cfg.executor, strategy_key=strategy_key,
        wallet_address=wallet, signal_id=signal_id, market_id=market.id,
        market_question=market.question or "", outcome=outcome, side="buy",
        expected_price=round(price, 4), limit_price=round(fill.limit_price, 4),
        fill_price=round(fill.fill_price, 4), slippage=round((fill.fill_price - price) / price, 4) if price else 0.0,
        fees=round(fill.fees, 4), size_usd=stake, shares=round(stake / pc, 4),
        order_id=fill.order_id, order_latency_ms=fill.order_latency_ms,
        confirm_latency_ms=fill.confirm_latency_ms, status="open",
        entry_reason=entry_reason, bankroll_before=st.bankroll)
    db.add(ex)
    db.commit()
    # one-order test: auto-halt after the configured number of orders
    if cfg.max_orders > 0 and _order_count(db, cfg.executor) >= cfg.max_orders:
        _trip_halt(db, st, f"one-order test complete ({cfg.max_orders}) — manual resume required")
    return ex


def settle_live(db: Session) -> dict:
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
    """Worker hook. ALWAYS settles existing live positions. Places new orders only
    when enabled, copying the chosen strategy's signals (edge/confidence filtered)."""
    settle_live(db)
    cfg = get_config()
    if not cfg.enabled:
        return {"enabled": False, "placed": 0}
    from .models import PaperSignal, Wallet
    recent = db.scalars(select(PaperSignal).where(
        PaperSignal.created_at >= datetime.utcnow() - timedelta(hours=2))
        .order_by(PaperSignal.created_at.desc()).limit(limit)).all()
    done = set(db.scalars(select(LiveExecution.signal_id)).all())
    placed = 0
    for s in recent:
        if s.id in done:
            continue
        # copy only Top-Decile-Edge-like signals: strong edge + confidence
        if float(s.edge_estimate or 0) < cfg.min_edge or float(s.confidence or 0) < cfg.min_confidence:
            continue
        w = db.get(Wallet, s.wallet_id)
        m = db.get(Market, s.market_id)
        if not (w and m) or m.resolved:
            continue
        ex = process_signal(db, strategy_key=cfg.strategy, wallet=w.address, signal_id=s.id,
                            market=m, outcome=s.outcome, price=float(s.observed_price or 0.5),
                            entry_reason=f"copy {w.address[:10]} conf={s.confidence:.0f} edge={s.edge_estimate}")
        if ex:
            placed += 1
        if get_state(db).halted:    # one-order test halts mid-loop
            break
    return {"enabled": True, "placed": placed}


def reconcile(db: Session, reported_balance: float, tolerance: float = 0.50) -> dict:
    st = get_state(db)
    computed = round(st.starting_bankroll + _realized_total(db), 2)
    open_exposure = round(sum(e.size_usd for e in _open(db)), 2)
    expected_cash = round(computed - open_exposure, 2)
    drift = round(reported_balance - expected_cash, 2)
    return {"starting_bankroll": st.starting_bankroll, "computed_equity": computed,
            "open_exposure": open_exposure, "expected_cash": expected_cash,
            "reported_balance": round(reported_balance, 2), "drift": drift,
            "reconciled": abs(drift) <= tolerance}


def status(db: Session) -> dict:
    cfg = get_config()
    st = get_state(db)
    open_ = _open(db)
    now = datetime.utcnow()
    return {
        "paper_trading_default": True,
        "live_trading_enabled": cfg.enabled,
        "executor": cfg.executor,
        "real_orders_placed": _order_count(db, "polymarket"),
        "orders_this_executor": _order_count(db, cfg.executor),
        "auth": {  # two-tier: L1 private key (sign) + L2 Relayer api creds (post)
            "l1_private_key_present": bool(os.getenv("POLYMARKET_PRIVATE_KEY")),
            "l2_api_creds_present": all(os.getenv(k) for k in (
                "POLYMARKET_API_KEY", "POLYMARKET_API_SECRET", "POLYMARKET_API_PASSPHRASE")),
        },
        "strategy_copied": cfg.strategy,
        "sizing": {"method": "fixed_dollar", "position_usd": cfg.position_usd,
                   "min_stake": cfg.min_stake, "no_compounding": True, "no_leverage": True},
        "limits_usd": {"max_position": cfg.position_usd, "max_total_risk": cfg.max_total_risk,
                       "max_positions": cfg.max_positions, "max_per_market": cfg.max_per_market,
                       "max_per_wallet": cfg.max_per_wallet, "daily_loss_stop": cfg.daily_loss_stop,
                       "total_loss_stop": cfg.total_loss_stop},
        "max_orders": cfg.max_orders, "max_slippage_pct": cfg.max_slippage_pct,
        "state": {"starting_bankroll": st.starting_bankroll, "bankroll": st.bankroll,
                  "halted": st.halted, "halt_reason": st.halt_reason},
        "open_positions": len(open_), "open_exposure": round(sum(e.size_usd for e in open_), 2),
        "day_pnl": round(_realized_since(db, now.replace(hour=0, minute=0, second=0, microsecond=0)), 2),
        "total_realized": round(_realized_total(db), 2),
        "max_possible_loss": cfg.total_loss_stop,
    }


def list_executions(db: Session, limit: int = 100) -> list[dict]:
    rows = db.scalars(select(LiveExecution).order_by(LiveExecution.created_at.desc()).limit(limit)).all()
    return [{
        "id": e.id, "created_at": e.created_at.isoformat() if e.created_at else None,
        "executor": e.executor, "strategy": e.strategy_key, "wallet": e.wallet_address,
        "signal_id": e.signal_id, "market_id": e.market_id, "market_question": e.market_question,
        "outcome": e.outcome, "side": e.side, "expected_price": e.expected_price,
        "limit_price": e.limit_price, "fill_price": e.fill_price, "slippage": e.slippage,
        "fees": e.fees, "size_usd": e.size_usd, "shares": e.shares, "order_id": e.order_id,
        "order_latency_ms": e.order_latency_ms, "confirm_latency_ms": e.confirm_latency_ms,
        "status": e.status, "entry_reason": e.entry_reason, "exit_reason": e.exit_reason,
        "realized_pnl": e.realized_pnl, "bankroll_before": e.bankroll_before,
        "bankroll_after": e.bankroll_after,
    } for e in rows]
