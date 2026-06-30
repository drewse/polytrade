"""BTC 5M LIVE MAKER executor — capped, maker-only, DATA-COLLECTION trial.

⚠️ The only real-money path in the codebase. It is unreachable unless ALL hold:
  * BTC5M_LIVE_MAKER_ENABLED=true   (env master switch; default false)
  * a session is ARMED in mode='live'
  * a private key is present in env
  * the kill flag is clear and the arm has not expired
  * every hard risk cap passes pre-submit, and the order is maker-only (rests, never
    crosses the book)
Shadow mode reads the real book but sends NOTHING. Mock mode is offline (tests).

Purpose: measure latency / fill probability / queue lifetime / cancellation / adverse
selection / realized spread / net P&L — NOT to make money. Fully isolated from
live.py / services.py / copy-trading / bankroll.
"""
from __future__ import annotations

import math
import os
import time
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import btc5m_live_maker_clob as clob
from . import btc5m_live_maker_models as lm


def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def get_config() -> dict:
    """Caps live in env so they're auditable, not hard-coded."""
    # MAX_EXPERIMENT_CAPITAL is the SOFTWARE budget for this experiment. The executor
    # ignores the wallet's actual balance entirely and only ever risks up to this much.
    return {
        "enabled": _truthy(os.getenv("BTC5M_LIVE_MAKER_ENABLED", "false")),
        "has_key": bool(os.getenv("BTC5M_LIVE_MAKER_PRIVATE_KEY")),
        # Same wallet as production → default sig-type/funder to the already-VERIFIED
        # production values unless overridden for the experiment.
        "signature_type": int(os.getenv("BTC5M_LIVE_MAKER_SIGNATURE_TYPE") or os.getenv("POLYMARKET_SIGNATURE_TYPE") or "0"),
        "funder": (os.getenv("BTC5M_LIVE_MAKER_FUNDER") or os.getenv("POLYMARKET_FUNDER")
                   or os.getenv("RELAYER_API_KEY_ADDRESS") or None),
        "max_experiment_capital_usd": float(os.getenv("BTC5M_LIVE_MAKER_MAX_EXPERIMENT_CAPITAL", "100")),
        "cumulative_loss_stop_usd": float(os.getenv("BTC5M_LIVE_MAKER_CUMULATIVE_LOSS_STOP", "100")),
        # Polymarket's min order is ~5 shares, so the realistic per-order floor is
        # ~5 × price (≈ $2-3). Default $3 fits cheap-side (price ≤ 0.60) min orders.
        "per_order_usd": float(os.getenv("BTC5M_LIVE_MAKER_PER_ORDER_USD", "3.0")),
        "max_concurrent": int(os.getenv("BTC5M_LIVE_MAKER_MAX_CONCURRENT", "3")),
        "max_exposure_usd": float(os.getenv("BTC5M_LIVE_MAKER_MAX_EXPOSURE_USD", "8")),
        "session_loss_limit_usd": float(os.getenv("BTC5M_LIVE_MAKER_SESSION_LOSS_USD", "10")),
        "queue_lifetime_s": float(os.getenv("BTC5M_LIVE_MAKER_QUEUE_LIFETIME_S", "12")),
        "session_ttl_min": float(os.getenv("BTC5M_LIVE_MAKER_SESSION_TTL_MIN", "20")),
        "min_order_shares": float(os.getenv("BTC5M_LIVE_MAKER_MIN_SHARES", "5")),
        "markout_5s": 5.0, "markout_30s": 30.0,
    }


# ---------------------------------------------------------------------------
# capital ledger — derived from the DB, ignores wallet balance entirely
# ---------------------------------------------------------------------------
def committed_capital(db: Session) -> float:
    """USD currently AT RISK: notional resting in open orders + cost basis of filled,
    not-yet-resolved positions. Recomputed from the DB so it can never drift."""
    resting = db.scalars(select(lm.Btc5mLiveMakerOrder).where(
        lm.Btc5mLiveMakerOrder.mode != "shadow",
        lm.Btc5mLiveMakerOrder.status.in_(("acked", "resting")))).all()
    positions = db.scalars(select(lm.Btc5mLiveMakerOrder).where(
        lm.Btc5mLiveMakerOrder.mode != "shadow",
        lm.Btc5mLiveMakerOrder.status.in_(("filled", "partial")),
        lm.Btc5mLiveMakerOrder.position_settled.is_(False))).all()
    rest = sum(o.notional_usd for o in resting)
    pos = sum((o.filled_shares or 0) * (o.fill_price or o.price) for o in positions)
    return round(rest + pos, 4)


def cumulative_realized_pnl(db: Session) -> float:
    rows = db.scalars(select(lm.Btc5mLiveMakerOrder.realized_pnl).where(
        lm.Btc5mLiveMakerOrder.position_settled.is_(True))).all()
    return round(sum(p or 0.0 for p in rows), 4)


def _state(db: Session) -> lm.Btc5mLiveMakerState:
    st = db.get(lm.Btc5mLiveMakerState, 1)
    if st is None:
        st = lm.Btc5mLiveMakerState(id=1)
        db.add(st)
        db.commit()
    return st


def log(db: Session, type_: str, payload: dict, *, session_id=None, order_client_id=None) -> None:
    db.add(lm.Btc5mLiveMakerEvent(type=type_, payload=payload, session_id=session_id,
                                  order_client_id=order_client_id, mono_ns=time.monotonic_ns()))
    db.commit()


# ---------------------------------------------------------------------------
# arm / disarm / kill
# ---------------------------------------------------------------------------
def lock(db: Session, reason: str) -> dict:
    """Latch the PERMANENT lock (cumulative loss stop). Cancels all + disarms. Stays
    locked across restarts until an explicit manual reset_lock()."""
    st = _state(db)
    st.locked = True
    st.lock_reason = reason
    db.commit()
    disarm(db, reason="locked:" + reason)
    log(db, "lock", {"reason": reason, "cumulative_pnl": cumulative_realized_pnl(db)})
    return {"ok": True, "locked": True, "reason": reason}


def reset_lock(db: Session) -> dict:
    """Manual reset of the permanent lock (operator action). Does NOT clear realized P&L
    history — the cumulative loss stop will re-trigger if losses remain past the stop."""
    st = _state(db)
    st.locked = False
    st.lock_reason = None
    db.commit()
    log(db, "reset_lock", {})
    return {"ok": True, "locked": False}


def arm(db: Session, *, mode: str = "shadow", ttl_min: float | None = None, max_orders: int = 0,
        queue_lifetime_s: float | None = None) -> dict:
    cfg = get_config()
    st = _state(db)
    if st.locked:
        return {"ok": False, "error": f"executor LOCKED: {st.lock_reason} — reset-lock required"}
    if mode == "live" and not cfg["enabled"]:
        return {"ok": False, "error": "live arming refused: BTC5M_LIVE_MAKER_ENABLED is false"}
    if mode == "live" and not cfg["has_key"]:
        return {"ok": False, "error": "live arming refused: no private key configured"}
    if st.kill:
        return {"ok": False, "error": "kill switch is engaged — reset it before arming"}
    # MANDATORY startup reconciliation before a LIVE session — cancel any orphan orders
    if mode == "live":
        recon = reconcile_open_orders(db)
        log(db, "reconcile", {"context": "pre-arm", **recon})
    ttl = ttl_min if ttl_min is not None else cfg["session_ttl_min"]
    caps = {k: cfg[k] for k in (
        "per_order_usd", "max_concurrent", "max_exposure_usd", "max_experiment_capital_usd",
        "cumulative_loss_stop_usd", "session_loss_limit_usd", "queue_lifetime_s", "min_order_shares")}
    # Per-session override of how long each maker quote rests before we cancel it. This is
    # the primary lever on fill probability (longer rest → more fills, more adverse selection)
    # and lets us vary it without a Railway env redeploy. Still maker-only, all caps unchanged.
    if queue_lifetime_s is not None:
        caps["queue_lifetime_s"] = float(queue_lifetime_s)
    sess = lm.Btc5mLiveMakerSession(mode=mode, max_orders=max_orders, caps=caps, status="active")
    db.add(sess)
    db.commit()
    st.armed = True
    st.mode = mode
    st.session_id = sess.id
    st.armed_at = datetime.utcnow()
    st.arm_expires_at = datetime.utcnow() + timedelta(minutes=ttl)
    st.open_exposure_usd = 0.0
    st.session_realized_pnl = 0.0
    db.commit()
    log(db, "arm", {"mode": mode, "ttl_min": ttl, "max_orders": max_orders, "caps": sess.caps}, session_id=sess.id)
    return {"ok": True, "armed": True, "mode": mode, "session_id": sess.id,
            "max_orders": max_orders, "expires_at": st.arm_expires_at.isoformat()}


def disarm(db: Session, reason: str = "manual", *, client=None) -> dict:
    st = _state(db)
    cancelled = _cancel_all(db, client=client)
    ended_session = None
    if st.session_id:
        sess = db.get(lm.Btc5mLiveMakerSession, st.session_id)
        if sess and sess.status == "active":
            sess.status = "ended"
            sess.ended_at = datetime.utcnow()
            sess.end_reason = reason
            ended_session = sess.id
    st.armed = False
    st.open_exposure_usd = 0.0
    db.commit()
    log(db, "disarm", {"reason": reason, "cancelled": cancelled}, session_id=st.session_id)
    if ended_session:                                  # auto research summary on session end
        try:
            generate_summary(db, ended_session)
        except Exception as exc:  # noqa: BLE001
            log(db, "error", {"stage": "summary", "error": str(exc)})
    return {"ok": True, "armed": False, "reason": reason, "cancelled": cancelled, "session_summary_for": ended_session}


def kill(db: Session, *, client=None) -> dict:
    st = _state(db)
    st.kill = True
    db.commit()
    out = disarm(db, reason="kill_switch", client=client)
    log(db, "kill", {})
    return {"ok": True, "killed": True, **out}


def reset_kill(db: Session) -> dict:
    st = _state(db)
    st.kill = False
    db.commit()
    return {"ok": True, "kill": False}


def _cancel_all(db: Session, *, client=None) -> int:
    actives = db.scalars(select(lm.Btc5mLiveMakerOrder)
                         .where(lm.Btc5mLiveMakerOrder.status.in_(("submitted", "acked", "resting", "partial")))).all()
    n = 0
    for o in actives:
        if client is not None and o.exchange_order_id:
            try:
                client.cancel(o.exchange_order_id)
            except Exception:  # noqa: BLE001
                pass
        o.status = "cancelled"
        o.cancel_req_at = o.cancel_req_at or datetime.utcnow()
        o.cancel_ack_at = datetime.utcnow()
        o.cancel_success = True
        n += 1
    db.commit()
    return n


# ---------------------------------------------------------------------------
# risk guard — checked before EVERY submit
# ---------------------------------------------------------------------------
def risk_check(db: Session, *, notional: float, price: float, best_ask: float | None) -> tuple[bool, str]:
    cfg = get_config()
    st = _state(db)
    if st.locked:
        return False, f"executor LOCKED: {st.lock_reason or 'cumulative loss stop'} — manual reset required"
    if st.kill:
        return False, "kill switch engaged"
    if not st.armed:
        return False, "not armed"
    if st.arm_expires_at and datetime.utcnow() > st.arm_expires_at:
        return False, "arm expired"
    if st.mode == "live" and not cfg["enabled"]:
        return False, "live blocked: ENABLED is false"
    # maker-only: must rest strictly inside the book
    if best_ask is None or price >= best_ask:
        return False, "would cross/take (maker-only)"
    if notional > cfg["per_order_usd"] + 1e-9:
        return False, f"per-order cap (${notional:.2f} > ${cfg['per_order_usd']})"
    if st.open_exposure_usd + notional > cfg["max_exposure_usd"] + 1e-9:
        return False, f"max concurrent exposure (${st.open_exposure_usd + notional:.2f} > ${cfg['max_exposure_usd']})"
    # HARD experiment budget: capital at risk can never exceed MAX_EXPERIMENT_CAPITAL,
    # computed from our own ledger — the wallet balance is irrelevant.
    committed = committed_capital(db)
    if committed + notional > cfg["max_experiment_capital_usd"] + 1e-9:
        return False, f"experiment budget (${committed + notional:.2f} > ${cfg['max_experiment_capital_usd']} MAX_EXPERIMENT_CAPITAL)"
    n_open = db.scalar(select(func.count()).select_from(lm.Btc5mLiveMakerOrder)
                       .where(lm.Btc5mLiveMakerOrder.status.in_(("submitted", "acked", "resting", "partial")))) or 0
    if n_open >= cfg["max_concurrent"]:
        return False, f"max concurrent orders ({n_open})"
    if st.session_realized_pnl <= -cfg["session_loss_limit_usd"]:
        return False, "session loss limit hit"
    return True, "ok"


# ---------------------------------------------------------------------------
# client selection — the hard live gate
# ---------------------------------------------------------------------------
def _live_client():
    """Build the authenticated live client from env (key + optional sig-type/funder)."""
    cfg = get_config()
    return clob.LiveClobClient(private_key=os.environ["BTC5M_LIVE_MAKER_PRIVATE_KEY"],
                               signature_type=cfg["signature_type"], funder=cfg["funder"])


def _make_client(db: Session):
    cfg = get_config()
    st = _state(db)
    if st.locked or st.kill or not st.armed:
        return None
    if st.mode == "live":
        if not cfg["enabled"] or not cfg["has_key"]:
            return None
        return _live_client()
    return clob.ShadowClient()


def check_connection(db: Session) -> dict:
    """READ-ONLY pre-flight: authenticate the wallet against the CLOB and read open
    orders. Places NO order and cancels NOTHING. Works with just the key (independent
    of the ENABLED switch) so we can verify auth before ever enabling live."""
    cfg = get_config()
    if not cfg["has_key"]:
        return {"ok": False, "authenticated": False, "error": "no private key configured"}
    try:
        client = _live_client()                       # __init__ authenticates (derives API creds)
        addr = client.address()
        opens = client.open_orders()
        return {"ok": True, "authenticated": True, "clob_connection": "ok",
                "wallet_address": addr, "open_orders_on_exchange": len(opens or []),
                "signature_type": cfg["signature_type"], "funder": cfg["funder"],
                "note": "read-only auth check — no order placed, nothing cancelled"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "authenticated": False, "clob_connection": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "hint": "if your Polymarket wallet is an email/Magic proxy, set BTC5M_LIVE_MAKER_SIGNATURE_TYPE=1 "
                        "and BTC5M_LIVE_MAKER_FUNDER=<proxy address>"}


# ---------------------------------------------------------------------------
# startup reconciliation — cancel orphan open orders before trading
# ---------------------------------------------------------------------------
def reconcile_open_orders(db: Session, *, client=None) -> dict:
    """Detect + cancel any open orders left from a previous run (crash/restart) BEFORE
    trading. Cancels both exchange-side orphans and any of our DB orders still marked
    active. Live-mode only needs the exchange sweep; shadow/mock is a DB sweep."""
    cfg = get_config()
    # Reconciliation only CANCELS (safe direction), so it may run with just the key,
    # independent of the ENABLED switch — to sweep orphans before we ever go live.
    if client is None and cfg["has_key"]:
        try:
            client = _live_client()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"reconcile client: {exc}", "exchange_cancelled": 0, "db_cancelled": 0}
    # SHARED-WALLET SAFETY: the production copy-trader uses the same wallet. We cancel
    # ONLY orders WE created (ids in our DB) — never the wallet's other open orders.
    our_ids = {o for (o,) in db.execute(select(lm.Btc5mLiveMakerOrder.exchange_order_id)
               .where(lm.Btc5mLiveMakerOrder.exchange_order_id.isnot(None))).all()}
    exch = 0
    if client is not None and hasattr(client, "open_orders"):
        try:
            for oid in client.open_orders() or []:
                if oid in our_ids:                       # ours only — leave production orders alone
                    client.cancel(oid)
                    exch += 1
        except Exception as exc:  # noqa: BLE001
            log(db, "error", {"stage": "reconcile", "error": str(exc)})
    db_cancelled = _cancel_all(db, client=client)
    st = _state(db)
    st.open_exposure_usd = 0.0
    db.commit()
    return {"ok": True, "exchange_cancelled_ours": exch, "db_cancelled": db_cancelled,
            "note": "only orders created by this experiment are cancelled — production wallet orders untouched"}


# ---------------------------------------------------------------------------
# one cycle: reconcile open orders → mark-outs → maybe post one new quote
# ---------------------------------------------------------------------------
def run_cycle(db: Session, *, client=None) -> dict:
    st = _state(db)
    if st.locked:
        return {"ran": False, "skipped": "locked"}
    if st.kill:
        return {"ran": False, "skipped": "kill"}
    if not st.armed:
        return {"ran": False, "skipped": "disarmed"}
    if st.arm_expires_at and datetime.utcnow() > st.arm_expires_at:
        disarm(db, reason="expired", client=client)
        return {"ran": False, "skipped": "expired"}
    own_client = client is None
    if own_client:
        client = _make_client(db)
    if client is None:
        return {"ran": False, "skipped": "no client (gated)"}

    cfg = get_config()
    reconciled = _reconcile(db, client, cfg)
    # realize P&L + free committed capital + PERMANENT cumulative-loss lock (shared path)
    sweep = settle_open_positions(db)
    settled = sweep["settled"]
    if sweep["locked"]:
        return {"ran": True, "reconciled": reconciled, "settled": settled, "stopped": "cumulative_loss_lock"}
    posted = _maybe_post(db, client, cfg)
    st.committed_capital_usd = committed_capital(db)
    st.last_cycle_at = datetime.utcnow()
    st.last_error = None
    db.commit()
    # session loss auto-stop
    if st.session_realized_pnl <= -cfg["session_loss_limit_usd"]:
        disarm(db, reason="session_loss_limit", client=client)
        return {"ran": True, "reconciled": reconciled, "settled": settled, "posted": posted, "stopped": "session_loss_limit"}
    # SINGLE-ORDER auto-disarm: once the order cap is reached AND that order has reached a
    # terminal state (filled / cancelled / rejected / error), disarm immediately.
    sess = db.get(lm.Btc5mLiveMakerSession, st.session_id) if st.session_id else None
    if sess and sess.max_orders and sess.orders >= sess.max_orders:
        live_n = db.scalar(select(func.count()).select_from(lm.Btc5mLiveMakerOrder).where(
            lm.Btc5mLiveMakerOrder.session_id == sess.id,
            lm.Btc5mLiveMakerOrder.status.in_(("intended", "submitted", "acked", "resting", "partial")))) or 0
        if live_n == 0:                                # the single order is no longer working
            disarm(db, reason="single_order_complete", client=client)
            return {"ran": True, "reconciled": reconciled, "settled": settled, "posted": posted,
                    "stopped": "single_order_complete"}
    return {"ran": True, "reconciled": reconciled, "settled": settled, "posted": posted, "mode": st.mode}


def _settle_positions(db: Session, cfg: dict) -> int:
    """Settle filled positions whose 5-minute market has resolved: realize P&L, free the
    committed capital, accumulate session + cumulative P&L."""
    import time as _time
    now_ts = int(_time.time())
    pend = db.scalars(select(lm.Btc5mLiveMakerOrder).where(
        lm.Btc5mLiveMakerOrder.mode != "shadow",
        lm.Btc5mLiveMakerOrder.status.in_(("filled", "partial")),
        lm.Btc5mLiveMakerOrder.position_settled.is_(False))).all()
    st = _state(db)
    n = 0
    for o in pend:
        if not o.market_window_ts or now_ts < o.market_window_ts + 300:
            continue                                  # not resolved yet (window + 5m)
        res = clob.get_resolution(o.market_window_ts, o.market_id)
        if not res["resolved"] or res["won_yes"] is None:
            continue
        won = res["won_yes"] if o.outcome == "YES" else (not res["won_yes"])
        cost = (o.filled_shares or 0) * (o.fill_price or o.price)
        payout = (o.filled_shares or 0) if won else 0.0
        o.realized_pnl = round(payout - cost, 4)
        o.won = won
        o.markout_settlement = round((1.0 if (res["won_yes"]) else 0.0) - (o.fill_price or o.price), 4)
        o.counterfactual = _counterfactual(o, won)
        o.position_settled = True
        st.session_realized_pnl = round(st.session_realized_pnl + o.realized_pnl, 4)
        log(db, "settle", {"won": won, "pnl": o.realized_pnl, "cost": round(cost, 4),
                           "counterfactual": o.counterfactual}, order_client_id=o.client_id)
        n += 1
    if n:
        db.commit()
    return n


def settle_open_positions(db: Session) -> dict:
    """Settlement sweep that is INDEPENDENT of the armed loop. Resolves any filled
    position whose market has closed, refreshes the DB-derived ledger on the state row,
    and latches the permanent cumulative-loss lock if breached. Safe to call any time
    (disarmed, between sessions, or on demand) — already-settled positions are skipped.

    This decouples P&L realisation from arming: a 5-minute position that resolves AFTER
    a session disarms still settles, frees its committed capital, and counts toward the
    cumulative-loss guard."""
    cfg = get_config()
    settled = _settle_positions(db, cfg)
    st = _state(db)
    cum = cumulative_realized_pnl(db)
    st.cumulative_realized_pnl = cum
    st.committed_capital_usd = committed_capital(db)
    db.commit()
    if -cum >= cfg["cumulative_loss_stop_usd"] - 1e-9 and not st.locked:
        lock(db, reason=f"cumulative loss ${-cum:.2f} reached ${cfg['cumulative_loss_stop_usd']} stop")
        return {"settled": settled, "cumulative_realized_pnl": cum,
                "committed_capital_usd": st.committed_capital_usd, "locked": True}
    return {"settled": settled, "cumulative_realized_pnl": cum,
            "committed_capital_usd": st.committed_capital_usd, "locked": st.locked}


def _counterfactual(o: lm.Btc5mLiveMakerOrder, won: bool) -> dict:
    """Would a bid ONE TICK higher (more aggressive) or lower (deeper) have been better?
    Uses the market's public trade stream to decide whether each price would have filled
    within our quote window, and what its P&L would have been. won is for OUR side."""
    tick = 0.01
    shares = o.size_shares
    win_val = 1.0 if won else 0.0          # payout per share for OUR side
    res = {"actual": {"price": o.price, "filled": bool(o.filled_shares), "pnl": o.realized_pnl}}
    try:
        if not o.market_window_ts or not o.quote_at:
            raise ValueError("no window/quote time")
        trades = clob.market_trades(o.market_id)
        start = o.quote_at.timestamp()
        end = start + (o.queue_lifetime_ms or 12000) / 1000.0
        # our YES bid fills if a trade prints at/through the bid price within the window
        win_trades = [t for t in trades if start <= t["ts"] <= end + 2]   # +2s slack

        def would_fill(bid_price):
            # YES bid fills when someone sells YES at <= our price
            tgt = bid_price if o.outcome == "YES" else (1.0 - bid_price)
            up = o.outcome != "YES"
            return any((t["yes_price"] >= tgt) if up else (t["yes_price"] <= tgt) for t in win_trades)

        for label, px in (("one_tick_higher", round(o.price + tick, 3)), ("one_tick_lower", round(o.price - tick, 3))):
            px = max(0.01, min(0.99, px))
            f = would_fill(px)
            res[label] = {"price": px, "would_fill": f,
                          "pnl": round(shares * win_val - shares * px, 4) if f else 0.0}
        ranked = [(k, v) for k, v in res.items() if v.get("filled") or v.get("would_fill")]
        ranked.sort(key=lambda kv: -(kv[1]["pnl"] or 0))
        res["best_choice"] = ranked[0][0] if ranked else "none_filled"
        res["actual_was_best"] = (res["best_choice"] == "actual")
    except Exception as exc:  # noqa: BLE001
        res["error"] = str(exc)
    return res


def _reconcile(db: Session, client, cfg: dict) -> int:
    from sqlalchemy import or_
    n = 0
    # active orders, PLUS filled orders that still owe a mark-out (adverse selection)
    actives = db.scalars(select(lm.Btc5mLiveMakerOrder).where(or_(
        lm.Btc5mLiveMakerOrder.status.in_(("acked", "resting", "partial")),
        (lm.Btc5mLiveMakerOrder.status == "filled") & (lm.Btc5mLiveMakerOrder.mid_30s.is_(None))))).all()
    now = datetime.utcnow()
    st = _state(db)
    # honour the active session's per-session queue-lifetime override (falls back to env cfg)
    queue_lifetime_s = cfg["queue_lifetime_s"]
    if st.session_id:
        sess = db.get(lm.Btc5mLiveMakerSession, st.session_id)
        if sess and isinstance(sess.caps, dict):
            queue_lifetime_s = sess.caps.get("queue_lifetime_s", queue_lifetime_s)
    for o in actives:
        # poll fills (real/mock only — shadow has no exchange order)
        if o.status in ("acked", "resting", "partial") and o.exchange_order_id:
            try:
                s = client.get_order(o.exchange_order_id)
            except Exception as exc:  # noqa: BLE001
                o.error = f"poll: {exc}"; s = {"ok": False}
            filled = float(s.get("filled_size", 0) or 0)
            if filled > o.filled_shares:
                if o.first_fill_at is None:
                    o.first_fill_at = now
                    o.fill_latency_ms = (now - o.submit_at).total_seconds() * 1000 if o.submit_at else None
                    bk = clob.get_book(o.token_id)
                    o.mid_at_fill = bk.get("mid")
                    o.fill_price = s.get("fill_price", o.price)
                    o.realized_spread = round((o.mid_at_fill - o.fill_price), 5) if (o.mid_at_fill is not None and o.fill_price is not None) else None
                    log(db, "fill", {"filled": filled, "price": o.fill_price, "mid": o.mid_at_fill},
                        session_id=o.session_id, order_client_id=o.client_id)
                o.filled_shares = filled
                o.partial = 0 < filled < o.size_shares
                o.status = "filled" if filled >= o.size_shares - 1e-9 else "partial"
                n += 1
        # mark-outs after fill (adverse selection)
        if o.first_fill_at is not None:
            age = (now - o.first_fill_at).total_seconds()
            if o.mid_5s is None and age >= cfg["markout_5s"]:
                o.mid_5s = clob.get_book(o.token_id).get("mid")
                o.adverse_5s = round((o.mid_5s - o.fill_price), 5) if (o.mid_5s is not None and o.fill_price is not None) else None
                log(db, "markout", {"horizon": "5s", "mid": o.mid_5s, "adverse": o.adverse_5s}, order_client_id=o.client_id)
            if o.mid_30s is None and age >= cfg["markout_30s"]:
                o.mid_30s = clob.get_book(o.token_id).get("mid")
                o.adverse_30s = round((o.mid_30s - o.fill_price), 5) if (o.mid_30s is not None and o.fill_price is not None) else None
                log(db, "markout", {"horizon": "30s", "mid": o.mid_30s, "adverse": o.adverse_30s}, order_client_id=o.client_id)
        # cancel after queue lifetime if not (fully) filled
        if o.status in ("acked", "resting", "partial") and o.quote_at:
            if (now - o.quote_at).total_seconds() >= queue_lifetime_s:
                _cancel_order(db, client, o, st)
                n += 1
    db.commit()
    return n


def _cancel_order(db, client, o, st):
    o.cancel_req_at = datetime.utcnow()
    res = {"ok": True, "cancelled": True, "latency_ms": 0.0}
    if o.exchange_order_id:
        try:
            res = client.cancel(o.exchange_order_id)
        except Exception as exc:  # noqa: BLE001
            o.error = f"cancel: {exc}"; res = {"cancelled": False}
    o.cancel_ack_at = datetime.utcnow()
    o.cancel_latency_ms = res.get("latency_ms")
    o.cancel_success = bool(res.get("cancelled", True))
    o.queue_lifetime_ms = (o.cancel_ack_at - o.quote_at).total_seconds() * 1000 if o.quote_at else None
    # terminal status after the remainder is cancelled: a partial keeps its filled
    # position (settles at resolution); a fully-unfilled order is simply cancelled.
    o.status = "filled" if (o.filled_shares and o.filled_shares > 0) else "cancelled"
    # free the resting exposure
    if o.mode != "shadow":
        st.open_exposure_usd = max(0.0, st.open_exposure_usd - o.notional_usd * (1 - o.filled_shares / o.size_shares))
    log(db, "cancel_ack", {"success": o.cancel_success, "lifetime_ms": o.queue_lifetime_ms}, order_client_id=o.client_id)


def _maybe_post(db: Session, client, cfg: dict) -> dict | None:
    st = _state(db)
    sess = db.get(lm.Btc5mLiveMakerSession, st.session_id) if st.session_id else None
    # NO RE-ENTRY once the session's order cap is reached (smoke test = 1)
    if sess and sess.max_orders and sess.orders >= sess.max_orders:
        return None
    n_open = db.scalar(select(func.count()).select_from(lm.Btc5mLiveMakerOrder)
                       .where(lm.Btc5mLiveMakerOrder.status.in_(("acked", "resting", "partial")))) or 0
    if n_open >= cfg["max_concurrent"]:
        return None
    markets = clob.open_btc5m_markets(limit=10)
    if not markets:
        return None
    # pick a market we don't already have a live resting order in
    busy = {o for (o,) in db.execute(select(lm.Btc5mLiveMakerOrder.market_id)
            .where(lm.Btc5mLiveMakerOrder.status.in_(("acked", "resting", "partial")))).all()}
    detected = datetime.utcnow()
    now_ts = int(time.time())
    skipped: list[dict] = []                            # competing candidates we passed over
    for mk in markets:
        if mk["market_id"] in busy:
            skipped.append({"market_id": mk["market_id"], "reason": "already quoting this market"}); continue
        if not mk["token_ids"]:
            skipped.append({"market_id": mk["market_id"], "reason": "no token ids"}); continue
        token = mk["token_ids"][0]                      # YES token; join-best-bid measurement
        bk = clob.get_book(token)
        if not bk["ok"] or bk["best_bid"] is None or bk["best_ask"] is None or bk["best_bid"] >= bk["best_ask"]:
            skipped.append({"market_id": mk["market_id"], "reason": "no two-sided book / crossed"}); continue
        price = round(bk["best_bid"], 3)                # JOIN best bid (first session)
        shares = math.floor(cfg["per_order_usd"] / max(price, 0.02))
        if shares < cfg["min_order_shares"]:            # venue min doesn't fit the per-order cap
            skipped.append({"market_id": mk["market_id"], "reason": f"min {cfg['min_order_shares']} sh @ {price} > ${cfg['per_order_usd']} cap"}); continue
        notional = round(shares * price, 4)
        ok, reason = risk_check(db, notional=notional, price=price, best_ask=bk["best_ask"])
        if not ok:
            log(db, "reject", {"reason": reason, "market": mk["market_id"], "price": price, "notional": notional})
            skipped.append({"market_id": mk["market_id"], "reason": reason}); continue
        spread = round(bk["best_ask"] - bk["best_bid"], 4)
        edge = round((bk["mid"] - price), 4) if bk["mid"] is not None else None
        decision = {
            "title": mk.get("question"), "window_ts": mk.get("window_ts"),
            "secs_to_resolution": (mk["window_ts"] + 300 - now_ts) if mk.get("window_ts") else None,
            "best_bid": bk["best_bid"], "best_ask": bk["best_ask"], "mid": bk["mid"], "spread": spread,
            "resting_shares_at_level": bk.get("bid_size"), "estimated_edge": edge,
            "selection_reason": (f"freshest open BTC-5m window with a two-sided book (spread {spread}, "
                                 f"edge {edge}); chosen over {len(skipped)} skipped candidate(s)"),
            "skipped_candidates": skipped[:6],
        }
        return _submit(db, client, mk, token, price, shares, notional, bk, detected, st, decision=decision, edge=edge)
    return None


def _submit(db, client, mk, token, price, shares, notional, bk, detected, st, *, decision=None, edge=None) -> dict:
    cid = uuid.uuid4().hex[:16]
    quote_at = datetime.utcnow()
    o = lm.Btc5mLiveMakerOrder(
        session_id=st.session_id, client_id=cid, market_id=mk["market_id"], token_id=token,
        outcome="YES", side="BUY", price=price, size_shares=shares, notional_usd=notional,
        mode=st.mode, status="intended", detected_at=detected, quote_at=quote_at, mid_at_quote=bk["mid"],
        market_window_ts=mk.get("window_ts"), decision=decision or {}, estimated_edge=edge)
    db.add(o)
    db.commit()
    log(db, "quote", {"market": mk["market_id"], "price": price, "shares": shares, "notional": notional,
                      "best_bid": bk["best_bid"], "best_ask": bk["best_ask"], "mid": bk["mid"]},
        session_id=st.session_id, order_client_id=cid)
    t0 = time.monotonic()
    try:
        res = client.post_limit(token_id=token, side="BUY", price=price, size=shares)
    except Exception as exc:  # noqa: BLE001
        o.status = "error"; o.error = f"submit: {exc}"; db.commit()
        log(db, "error", {"stage": "submit", "error": str(exc)}, order_client_id=cid)
        return {"client_id": cid, "status": "error"}
    o.submit_at = quote_at
    o.submit_latency_ms = (time.monotonic() - t0) * 1000
    o.ack_at = datetime.utcnow()
    o.ack_latency_ms = res.get("latency_ms")
    o.exchange_order_id = res.get("order_id")
    o.status = "shadow" if st.mode == "shadow" else (res.get("status") or "acked")
    if st.mode != "shadow" and o.status not in ("rejected", "error"):
        st.deployed_usd += notional
        st.open_exposure_usd += notional
    sess = db.get(lm.Btc5mLiveMakerSession, st.session_id)
    if sess:
        sess.orders += 1
    db.commit()
    log(db, "submit", {"status": o.status, "order_id": o.exchange_order_id,
                       "submit_ms": o.submit_latency_ms, "ack_ms": o.ack_latency_ms,
                       "would_place": res.get("would_place")}, order_client_id=cid)
    return {"client_id": cid, "status": o.status, "order_id": o.exchange_order_id, "price": price, "notional": notional}


# ---------------------------------------------------------------------------
# metrics + status (read-only)
# ---------------------------------------------------------------------------
def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 3) if xs else None


def metrics(db: Session, *, session_id: int | None = None) -> dict:
    q = select(lm.Btc5mLiveMakerOrder)
    if session_id:
        q = q.where(lm.Btc5mLiveMakerOrder.session_id == session_id)
    orders = db.scalars(q).all()
    real = [o for o in orders if o.mode != "shadow"]
    filled = [o for o in real if o.filled_shares > 0]
    n = len(real)
    return {
        "orders": len(orders), "real_orders": n, "shadow_orders": len(orders) - n,
        "fills": len(filled), "fill_probability": round(len(filled) / n, 4) if n else None,
        "partial_fills": sum(1 for o in real if o.partial),
        "avg_submit_latency_ms": _avg([o.submit_latency_ms for o in real]),
        "avg_ack_latency_ms": _avg([o.ack_latency_ms for o in real]),
        "avg_fill_latency_ms": _avg([o.fill_latency_ms for o in filled]),
        "avg_cancel_latency_ms": _avg([o.cancel_latency_ms for o in real]),
        "avg_queue_lifetime_ms": _avg([o.queue_lifetime_ms for o in real]),
        "cancel_success_rate": round(_safe_rate([o.cancel_success for o in real if o.cancel_req_at]), 4),
        "avg_realized_spread": _avg([o.realized_spread for o in filled]),
        "avg_adverse_5s": _avg([o.adverse_5s for o in filled]),
        "fees_usd": round(sum(o.fees_usd for o in real), 4),
        "net_pnl_usd": round(sum((o.realized_pnl or 0.0) for o in real) - sum(o.fees_usd for o in real), 4),
        "note": "fill metrics are from REAL (live/mock) orders only; shadow orders measure quoting, not fills",
    }


def _safe_rate(bools):
    bs = [1 if b else 0 for b in bools if b is not None]
    return sum(bs) / len(bs) if bs else 0.0


def generate_summary(db: Session, session_id: int) -> dict:
    """Auto research summary for a session — aggregates + best/worst quote distances +
    observed patterns + suggested parameter changes for the next session."""
    orders = db.scalars(select(lm.Btc5mLiveMakerOrder).where(
        lm.Btc5mLiveMakerOrder.session_id == session_id, lm.Btc5mLiveMakerOrder.mode != "shadow")).all()
    m = metrics(db, session_id=session_id)
    filled = [o for o in orders if o.filled_shares and o.filled_shares > 0]
    settled = [o for o in filled if o.position_settled]

    # best/worst quote distance (edge = mid − price): bucket by distance, score by settled P&L
    def bucket(e):
        if e is None:
            return "?"
        return "<0.005" if e < 0.005 else ("0.005-0.01" if e < 0.01 else ("0.01-0.02" if e < 0.02 else ">=0.02"))
    dist: dict = {}
    for o in settled:
        b = bucket(o.estimated_edge)
        dist.setdefault(b, []).append(o.realized_pnl or 0.0)
    by_distance = {b: {"n": len(v), "avg_pnl": round(_avg(v) or 0.0, 4)} for b, v in dist.items()}
    ranked = sorted(by_distance.items(), key=lambda kv: -(kv[1]["avg_pnl"]))
    best_dist = ranked[0][0] if ranked else None
    worst_dist = ranked[-1][0] if ranked else None

    # counterfactual: how often would ±1 tick have beaten the actual fill?
    cf = [o.counterfactual for o in settled if o.counterfactual]
    higher_better = sum(1 for c in cf if c.get("best_choice") == "one_tick_higher")
    lower_better = sum(1 for c in cf if c.get("best_choice") == "one_tick_lower")
    actual_best = sum(1 for c in cf if c.get("actual_was_best"))

    # patterns + suggestions (rule-based; honest about small samples)
    patterns, suggestions = [], []
    fr = m["fill_probability"]
    if fr is not None and fr < 0.05:
        patterns.append(f"very low fill rate ({fr:.1%})")
        suggestions.append("raise quote 1 tick (improve_bid) and/or lengthen queue lifetime to lift fills")
    if (m["avg_adverse_5s"] or 0) < -0.005:
        patterns.append(f"negative 5s mark-out ({m['avg_adverse_5s']}) — fills are adversely selected")
        suggestions.append("shorten queue lifetime / quote one tick deeper to reduce adverse fills")
    if higher_better > max(actual_best, lower_better):
        suggestions.append(f"one-tick-higher beat actual on {higher_better}/{len(cf)} fills → test improve_bid")
    elif lower_better > max(actual_best, higher_better):
        suggestions.append(f"one-tick-lower beat actual on {lower_better}/{len(cf)} fills → test quoting deeper")
    if (m["avg_realized_spread"] or 0) < 0:
        suggestions.append("realized spread negative — reduce aggression / widen entry")
    if not settled:
        patterns.append("no settled fills yet — collect more before drawing conclusions")
    if best_dist:
        patterns.append(f"best quote-distance bucket: {best_dist}; worst: {worst_dist}")

    summary = {
        "session_id": session_id, "generated_at": datetime.utcnow().isoformat(),
        "orders_posted": m["real_orders"], "fills": m["fills"], "fill_rate": fr,
        "avg_queue_lifetime_ms": m["avg_queue_lifetime_ms"],
        "avg_submit_latency_ms": m["avg_submit_latency_ms"], "avg_ack_latency_ms": m["avg_ack_latency_ms"],
        "avg_fill_latency_ms": m["avg_fill_latency_ms"], "cancel_success_rate": m["cancel_success_rate"],
        "avg_realized_spread": m["avg_realized_spread"], "avg_adverse_5s": m["avg_adverse_5s"],
        "net_pnl_usd": m["net_pnl_usd"], "settled_fills": len(settled),
        "quote_distance_performance": by_distance, "best_quote_distance": best_dist, "worst_quote_distance": worst_dist,
        "counterfactual": {"actual_best": actual_best, "one_tick_higher_better": higher_better,
                           "one_tick_lower_better": lower_better, "n": len(cf)},
        "patterns": patterns, "suggested_parameter_changes": suggestions,
        "note": "research dataset for continuous improvement — not a profit report; small samples ⇒ treat as directional",
    }
    sess = db.get(lm.Btc5mLiveMakerSession, session_id)
    if sess:
        sess.summary = summary
        sess.fills = m["fills"]
        sess.realized_pnl = m["net_pnl_usd"]
        db.commit()
    return summary


def session_summary(db: Session, *, session_id: int | None = None) -> dict:
    if session_id is None:
        sess = db.scalar(select(lm.Btc5mLiveMakerSession).order_by(lm.Btc5mLiveMakerSession.id.desc()))
        session_id = sess.id if sess else None
    if session_id is None:
        return {"summary": None, "note": "no sessions yet"}
    return {"summary": generate_summary(db, session_id)}


def status(db: Session) -> dict:
    cfg = get_config()
    st = _state(db)
    expired = bool(st.arm_expires_at and datetime.utcnow() > st.arm_expires_at)
    n_open = db.scalar(select(func.count()).select_from(lm.Btc5mLiveMakerOrder)
                       .where(lm.Btc5mLiveMakerOrder.status.in_(("acked", "resting", "partial")))) or 0
    committed = committed_capital(db)
    cum = cumulative_realized_pnl(db)
    budget = cfg["max_experiment_capital_usd"]
    return {
        "enabled": cfg["enabled"], "has_key": cfg["has_key"], "armed": st.armed and not expired,
        "mode": st.mode, "kill": st.kill, "locked": st.locked, "lock_reason": st.lock_reason,
        "session_id": st.session_id,
        "arm_expires_at": st.arm_expires_at.isoformat() if st.arm_expires_at else None, "expired": expired,
        "caps": {k: cfg[k] for k in ("per_order_usd", "max_concurrent", "max_exposure_usd",
                                     "session_loss_limit_usd", "queue_lifetime_s")},
        "experiment_budget": {"max_experiment_capital_usd": budget, "committed_capital_usd": committed,
                              "remaining_usd": round(budget - committed, 4),
                              "cumulative_realized_pnl": cum, "cumulative_loss_stop_usd": cfg["cumulative_loss_stop_usd"],
                              "loss_remaining_to_lock_usd": round(cfg["cumulative_loss_stop_usd"] + min(0.0, cum), 4)},
        "open_exposure_usd": round(st.open_exposure_usd, 4),
        "open_orders": n_open, "session_realized_pnl": round(st.session_realized_pnl, 4),
        "last_cycle_at": st.last_cycle_at.isoformat() if st.last_cycle_at else None,
        "last_error": st.last_error, "metrics": metrics(db, session_id=st.session_id),
        "live_path_reachable": bool(cfg["enabled"] and st.armed and st.mode == "live" and cfg["has_key"]
                                    and not st.kill and not st.locked and not expired),
        "safety": ("BTC 5M live-maker trial — DATA COLLECTION only; maker-only, $%g experiment budget (software-"
                   "enforced, ignores wallet balance), permanent $%g cumulative-loss lock, default-off; no order "
                   "is sent unless ENABLED + armed(live) + key + caps pass" % (budget, cfg["cumulative_loss_stop_usd"])),
    }


def events(db: Session, *, limit: int = 100) -> dict:
    rows = db.scalars(select(lm.Btc5mLiveMakerEvent).order_by(lm.Btc5mLiveMakerEvent.id.desc()).limit(limit)).all()
    return {"events": [{"ts": e.ts.isoformat(), "type": e.type, "session_id": e.session_id,
                        "order_client_id": e.order_client_id, "payload": e.payload} for e in rows]}


def orders(db: Session, *, limit: int = 60) -> dict:
    """Decision-level record for every order — the research dataset."""
    rows = db.scalars(select(lm.Btc5mLiveMakerOrder).order_by(lm.Btc5mLiveMakerOrder.id.desc()).limit(limit)).all()
    def row(o):
        d = o.decision or {}
        return {"client_id": o.client_id, "market_id": o.market_id, "mode": o.mode, "status": o.status,
                "title": d.get("title"), "secs_to_resolution": d.get("secs_to_resolution"),
                "best_bid": d.get("best_bid"), "best_ask": d.get("best_ask"), "mid": o.mid_at_quote,
                "spread": d.get("spread"), "price": o.price, "resting_shares_at_level": d.get("resting_shares_at_level"),
                "estimated_edge": o.estimated_edge, "selection_reason": d.get("selection_reason"),
                "skipped_candidates": d.get("skipped_candidates"),
                "queue_lifetime_ms": o.queue_lifetime_ms, "filled": bool(o.filled_shares), "fill_price": o.fill_price,
                "adverse_5s": o.adverse_5s, "adverse_30s": o.adverse_30s, "markout_settlement": o.markout_settlement,
                "realized_pnl": o.realized_pnl, "won": o.won, "counterfactual": o.counterfactual,
                "submit_latency_ms": o.submit_latency_ms, "ack_latency_ms": o.ack_latency_ms}
    return {"orders": [row(o) for o in rows]}
