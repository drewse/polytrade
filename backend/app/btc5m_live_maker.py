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


def arm(db: Session, *, mode: str = "shadow", ttl_min: float | None = None) -> dict:
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
    sess = lm.Btc5mLiveMakerSession(mode=mode, caps={k: cfg[k] for k in (
        "per_order_usd", "max_concurrent", "max_exposure_usd", "max_experiment_capital_usd",
        "cumulative_loss_stop_usd", "session_loss_limit_usd", "queue_lifetime_s", "min_order_shares")}, status="active")
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
    log(db, "arm", {"mode": mode, "ttl_min": ttl, "caps": sess.caps}, session_id=sess.id)
    return {"ok": True, "armed": True, "mode": mode, "session_id": sess.id,
            "expires_at": st.arm_expires_at.isoformat()}


def disarm(db: Session, reason: str = "manual", *, client=None) -> dict:
    st = _state(db)
    cancelled = _cancel_all(db, client=client)
    if st.session_id:
        sess = db.get(lm.Btc5mLiveMakerSession, st.session_id)
        if sess and sess.status == "active":
            sess.status = "ended"
            sess.ended_at = datetime.utcnow()
            sess.end_reason = reason
    st.armed = False
    st.open_exposure_usd = 0.0
    db.commit()
    log(db, "disarm", {"reason": reason, "cancelled": cancelled}, session_id=st.session_id)
    return {"ok": True, "armed": False, "reason": reason, "cancelled": cancelled}


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
def _make_client(db: Session):
    cfg = get_config()
    st = _state(db)
    if st.locked or st.kill or not st.armed:
        return None
    if st.mode == "live":
        if not cfg["enabled"] or not cfg["has_key"]:
            return None
        return clob.LiveClobClient(private_key=os.environ["BTC5M_LIVE_MAKER_PRIVATE_KEY"])
    return clob.ShadowClient()


# ---------------------------------------------------------------------------
# startup reconciliation — cancel orphan open orders before trading
# ---------------------------------------------------------------------------
def reconcile_open_orders(db: Session, *, client=None) -> dict:
    """Detect + cancel any open orders left from a previous run (crash/restart) BEFORE
    trading. Cancels both exchange-side orphans and any of our DB orders still marked
    active. Live-mode only needs the exchange sweep; shadow/mock is a DB sweep."""
    cfg = get_config()
    if client is None and cfg["enabled"] and cfg["has_key"]:
        try:
            client = clob.LiveClobClient(private_key=os.environ["BTC5M_LIVE_MAKER_PRIVATE_KEY"])
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"reconcile client: {exc}", "exchange_cancelled": 0, "db_cancelled": 0}
    exch = 0
    if client is not None and hasattr(client, "open_orders"):
        try:
            for oid in client.open_orders() or []:
                client.cancel(oid)
                exch += 1
        except Exception as exc:  # noqa: BLE001
            log(db, "error", {"stage": "reconcile", "error": str(exc)})
    db_cancelled = _cancel_all(db, client=client)
    st = _state(db)
    st.open_exposure_usd = 0.0
    db.commit()
    return {"ok": True, "exchange_cancelled": exch, "db_cancelled": db_cancelled}


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
    settled = _settle_positions(db, cfg)              # realize P&L + free committed capital
    # PERMANENT cumulative loss stop — across ALL sessions
    cum = cumulative_realized_pnl(db)
    st.cumulative_realized_pnl = cum
    if -cum >= cfg["cumulative_loss_stop_usd"] - 1e-9:
        db.commit()
        lock(db, reason=f"cumulative loss ${-cum:.2f} reached ${cfg['cumulative_loss_stop_usd']} stop")
        return {"ran": True, "reconciled": reconciled, "settled": settled, "stopped": "cumulative_loss_lock"}
    posted = _maybe_post(db, client, cfg)
    st.committed_capital_usd = committed_capital(db)
    st.last_cycle_at = datetime.utcnow()
    st.last_error = None
    # session loss auto-stop
    if st.session_realized_pnl <= -cfg["session_loss_limit_usd"]:
        db.commit()
        disarm(db, reason="session_loss_limit", client=client)
        return {"ran": True, "reconciled": reconciled, "settled": settled, "posted": posted, "stopped": "session_loss_limit"}
    db.commit()
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
        res = clob.get_resolution(o.market_window_ts)
        if not res["resolved"] or res["won_yes"] is None:
            continue
        won = res["won_yes"] if o.outcome == "YES" else (not res["won_yes"])
        cost = (o.filled_shares or 0) * (o.fill_price or o.price)
        payout = (o.filled_shares or 0) if won else 0.0
        o.realized_pnl = round(payout - cost, 4)
        o.won = won
        o.position_settled = True
        st.session_realized_pnl = round(st.session_realized_pnl + o.realized_pnl, 4)
        log(db, "settle", {"won": won, "pnl": o.realized_pnl, "cost": round(cost, 4)}, order_client_id=o.client_id)
        n += 1
    if n:
        db.commit()
    return n


def _reconcile(db: Session, client, cfg: dict) -> int:
    from sqlalchemy import or_
    n = 0
    # active orders, PLUS filled orders that still owe a mark-out (adverse selection)
    actives = db.scalars(select(lm.Btc5mLiveMakerOrder).where(or_(
        lm.Btc5mLiveMakerOrder.status.in_(("acked", "resting", "partial")),
        (lm.Btc5mLiveMakerOrder.status == "filled") & (lm.Btc5mLiveMakerOrder.mid_30s.is_(None))))).all()
    now = datetime.utcnow()
    st = _state(db)
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
        # cancel after queue lifetime if not (fully) filled
        if o.status in ("acked", "resting", "partial") and o.quote_at:
            if (now - o.quote_at).total_seconds() >= cfg["queue_lifetime_s"]:
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
    if o.status != "partial":
        o.status = "cancelled"
    # free the resting exposure
    if o.mode != "shadow":
        st.open_exposure_usd = max(0.0, st.open_exposure_usd - o.notional_usd * (1 - o.filled_shares / o.size_shares))
    log(db, "cancel_ack", {"success": o.cancel_success, "lifetime_ms": o.queue_lifetime_ms}, order_client_id=o.client_id)


def _maybe_post(db: Session, client, cfg: dict) -> dict | None:
    st = _state(db)
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
    for mk in markets:
        if mk["market_id"] in busy or not mk["token_ids"]:
            continue
        token = mk["token_ids"][0]                      # YES token; join-best-bid measurement
        bk = clob.get_book(token)
        if not bk["ok"] or bk["best_bid"] is None or bk["best_ask"] is None or bk["best_bid"] >= bk["best_ask"]:
            continue
        price = round(bk["best_bid"], 3)                # JOIN best bid (first session)
        shares = math.floor(cfg["per_order_usd"] / max(price, 0.02))
        if shares < cfg["min_order_shares"]:            # venue min doesn't fit the per-order cap
            continue                                     # at this price -> skip, don't reject-loop
        notional = round(shares * price, 4)
        ok, reason = risk_check(db, notional=notional, price=price, best_ask=bk["best_ask"])
        if not ok:
            log(db, "reject", {"reason": reason, "market": mk["market_id"], "price": price, "notional": notional})
            continue
        return _submit(db, client, mk, token, price, shares, notional, bk, detected, st)
    return None


def _submit(db, client, mk, token, price, shares, notional, bk, detected, st) -> dict:
    cid = uuid.uuid4().hex[:16]
    quote_at = datetime.utcnow()
    o = lm.Btc5mLiveMakerOrder(
        session_id=st.session_id, client_id=cid, market_id=mk["market_id"], token_id=token,
        outcome="YES", side="BUY", price=price, size_shares=shares, notional_usd=notional,
        mode=st.mode, status="intended", detected_at=detected, quote_at=quote_at, mid_at_quote=bk["mid"],
        market_window_ts=mk.get("window_ts"))
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
