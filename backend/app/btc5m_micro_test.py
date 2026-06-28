"""BTC 5M Micro-Test Mode — opt-in, minimum-size live test of ONE BTC 5M momentum
strategy (single-wallet copy), fully isolated from general live copy trading.

ISOLATION GUARANTEES (by construction):
  * Writes ONLY to btc5m_micro_test_* tables. Never to LiveExecution / LiveState.
  * Never calls production sizing (conservative_stake/dynamic_stake), ranking,
    eligibility, discovery, or wallet approval. Fixed 5-share sizing only.
  * Production accounting (live.settle_live / bankroll / open-position counts)
    cannot see micro-test trades — they live in a separate table.
  * Reuses ONLY the safe execution primitive `live.get_executor(...).place(...)`
    (limit-at-reference, venue min/tick, TTL/cancel-unfilled, fill reconciliation,
    venue-error capture). That primitive places an order and returns a result; it
    writes nothing, so recording stays here.
  * Default DISABLED + DISARMED. Requires an explicit enable (env) AND a manual
    arm. A stop latch requires a manual re-arm.
  * Still respects global safety: if global live trading is halted/paused, the
    micro-test does not act; it also honours venue cash and its own stops.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import btc5m, live
from . import btc5m_micro_test_models as mt
from . import btc5m_models as bm
from .models import Market, Trade

MARKET_LIFE_SECONDS = 300

# execution outcomes that count as a hard execution/venue error -> stop the test.
# A clean no-fill (unfilled_cancelled) is NOT an error: it just didn't fill.
_ERROR_OUTCOMES = {"submit_error", "cancel_error", "error", "sdk_missing",
                   "geoblocked", "stale_client_schema"}


def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _addr_list(raw: str) -> list[str]:
    return [a.strip().lower() for a in (raw or "").replace(";", ",").split(",") if a.strip()]


def _cfg() -> dict:
    return {
        "enabled": _truthy(os.getenv("BTC5M_MICRO_TEST_ENABLED", "false")),
        "primary_wallet": (os.getenv("BTC5M_MICRO_TEST_PRIMARY_WALLET", "") or "").strip().lower(),
        "backup_wallets": _addr_list(os.getenv("BTC5M_MICRO_TEST_BACKUP_WALLETS", "")),
        "fixed_shares": float(os.getenv("BTC5M_MICRO_TEST_FIXED_SHARES", "5")),
        "max_entry_price": float(os.getenv("BTC5M_MICRO_TEST_MAX_ENTRY_PRICE", "0.60")),
        "max_concurrent": int(os.getenv("BTC5M_MICRO_TEST_MAX_CONCURRENT", "1")),
        "daily_loss_stop": float(os.getenv("BTC5M_MICRO_TEST_DAILY_LOSS_STOP", "10")),
        "total_loss_stop": float(os.getenv("BTC5M_MICRO_TEST_TOTAL_LOSS_STOP", "15")),
        "min_seconds_remaining": float(os.getenv("BTC5M_MICRO_TEST_MIN_SECONDS_REMAINING", "30")),
        "allowed_regimes": [r.strip() for r in os.getenv(
            "BTC5M_MICRO_TEST_ALLOWED_REGIMES", "Hybrid,Liquidity Spike").split(",") if r.strip()],
        "require_confidence": _truthy(os.getenv("BTC5M_MICRO_TEST_REQUIRE_CONFIDENCE", "false")),
        "min_confidence": float(os.getenv("BTC5M_MICRO_TEST_MIN_CONFIDENCE", "0.85")),
        "max_trades": int(os.getenv("BTC5M_MICRO_TEST_MAX_TRADES", "20")),
        "signal_max_age_min": float(os.getenv("BTC5M_MICRO_TEST_SIGNAL_MAX_AGE_MIN", "10")),
    }


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------
def get_mt_state(db: Session) -> mt.Btc5mMicroTestState:
    st = db.get(mt.Btc5mMicroTestState, 1)
    if st is None:
        st = mt.Btc5mMicroTestState(id=1, armed=False, stopped=False)
        db.add(st)
        db.commit()
    return st


def arm(db: Session, *, by: str | None = None) -> dict:
    """Explicit arm action. Clears any prior stop latch (manual re-arm)."""
    cfg = _cfg()
    if not cfg["enabled"]:
        return {"ok": False, "error": "BTC5M_MICRO_TEST_ENABLED is false — enable in env first"}
    if not cfg["primary_wallet"]:
        return {"ok": False, "error": "BTC5M_MICRO_TEST_PRIMARY_WALLET not set"}
    st = get_mt_state(db)
    st.armed = True
    st.stopped = False
    st.stop_reason = None
    st.armed_by = by or "operator"
    st.armed_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "armed": True, "by": st.armed_by}


def disarm(db: Session) -> dict:
    st = get_mt_state(db)
    st.armed = False
    db.commit()
    return {"ok": True, "armed": False}


def _stop(db: Session, st: mt.Btc5mMicroTestState, reason: str) -> None:
    st.stopped = True
    st.armed = False
    st.stop_reason = reason
    db.commit()


# ---------------------------------------------------------------------------
# accounting (ISOLATED — never touches LiveState / production bankroll)
# ---------------------------------------------------------------------------
def _trades(db: Session) -> list[mt.Btc5mMicroTestTrade]:
    return list(db.scalars(select(mt.Btc5mMicroTestTrade)).all())


def _accounting(db: Session) -> dict:
    rows = _trades(db)
    closed = [t for t in rows if t.status == "closed"]
    open_ = [t for t in rows if t.status == "open"]
    realized = round(sum(t.realized_pnl or 0.0 for t in closed), 2)
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    day_realized = round(sum(t.realized_pnl or 0.0 for t in closed if (t.closed_at or today) >= today), 2)
    wins = sum(1 for t in closed if t.won)
    paper_realized = round(sum(t.paper_realized_pnl or 0.0 for t in closed), 2)
    return {
        "settled_trades": len(closed),
        "open_positions": len(open_),
        "realized_pnl": realized,
        "day_realized_pnl": day_realized,
        "paper_realized_pnl": paper_realized,
        "win_rate": round(wins / len(closed), 4) if closed else 0.0,
        "wins": wins,
    }


# ---------------------------------------------------------------------------
# signal source — most recent qualifying primary/backup BUY in an OPEN BTC5M
# market that we have not already mirrored. Read-only over the indexed btc5m
# trades; never creates a signal on its own.
# ---------------------------------------------------------------------------
def _seconds_remaining(db: Session, market_id: str, now: datetime) -> float | None:
    bmk = db.get(bm.Btc5mMarket, market_id)
    expiry = bmk.expiry if (bmk and bmk.expiry) else None
    if expiry is None:
        m = db.get(Market, market_id)
        if m and m.created_at:
            expiry = m.created_at + timedelta(seconds=MARKET_LIFE_SECONDS)
    if expiry is None:
        return None
    return (expiry - now).total_seconds()


def _candidate_signals(db: Session, cfg: dict, now: datetime) -> list[dict]:
    """Build the ordered candidate list (primary first, then backups, newest
    first). Each candidate carries everything the gates + executor need."""
    watch = {cfg["primary_wallet"]: "primary"}
    for b in cfg["backup_wallets"]:
        watch.setdefault(b, "backup")
    if not watch:
        return []
    age_cut = now - timedelta(minutes=cfg["signal_max_age_min"])
    rows = db.scalars(
        select(bm.Btc5mTrade)
        .where(func.lower(bm.Btc5mTrade.wallet_address).in_(list(watch.keys())))
        .where(bm.Btc5mTrade.side == "buy")
        .order_by(bm.Btc5mTrade.timestamp.desc())).all()
    seen_markets: set[str] = set()
    out = []
    for bt in rows:
        mid = bt.market_id
        if mid in seen_markets:
            continue
        seen_markets.add(mid)
        if bt.timestamp and bt.timestamp < age_cut:
            continue
        m = db.get(Market, mid)
        if m is None or m.resolved:                       # only OPEN markets
            continue
        # exact market outcome string from the source production trade
        outcome = None
        if bt.source_trade_id is not None:
            src = db.get(Trade, bt.source_trade_id)
            outcome = src.outcome if src else None
        if not outcome:
            outcome = _outcome_for_direction(m, bt.direction)
        role = watch.get((bt.wallet_address or "").lower(), "backup")
        out.append({
            "btc5m_trade_id": bt.id, "market_id": mid, "market": m, "outcome": outcome,
            "direction": bt.direction, "reference_price": float(bt.price),
            "wallet": bt.wallet_address, "role": role, "timestamp": bt.timestamp,
        })
    # primary before backup, then newest first
    out.sort(key=lambda c: (0 if c["role"] == "primary" else 1, -(c["timestamp"] or now).timestamp()))
    return out


def _outcome_for_direction(market: Market, direction: str) -> str | None:
    for o in (market.outcomes or []):
        if btc5m._yes_no(o) == direction:
            return o
    outs = list(market.outcomes or [])
    return outs[0] if outs else direction


def _confidence_for(db: Session, market_id: str) -> float | None:
    """Champion-model confidence for this market's reconstructed state (only used
    when REQUIRE_CONFIDENCE is on). Best-effort, read-only."""
    try:
        champ = btc5m.champion(db, "global")
        if champ is None:
            return None
        feats = btc5m._market_state_features(db, market_id)
        if not feats:
            return None
        all_trades = db.scalars(select(bm.Btc5mTrade)).all()
        X, y = btc5m._dataset_xy(all_trades)
        if len(X) < 6 or len(set(y)) < 2:
            return None
        model = btc5m._load_model(champ)
        model.fit(X, y)
        p_yes = model.predict_proba([btc5m.feature_vector(feats)])[0]
        return round(abs(p_yes - 0.5) * 2, 4)
    except Exception:  # noqa: BLE001  (confidence is advisory; never break the test)
        return None


def _evaluate_gates(db: Session, cfg: dict, cand: dict, now: datetime) -> tuple[bool, str, dict]:
    """Per-signal hard gates (besides global/account stops handled by caller).
    Returns (ok, reason, extras). Pure read-only checks."""
    extras: dict = {"regime": None, "confidence": None}
    # BTC5M market only (defensive — candidates already come from the btc5m set)
    m = cand["market"]
    if not btc5m.is_btc5m_market(m.question, getattr(m, "slug", None), getattr(m, "category", None)):
        return False, "not a BTC 5M market", extras
    # wallet must be primary/backup (defensive)
    if cand["wallet"].lower() not in ({cfg["primary_wallet"]} | set(cfg["backup_wallets"])):
        return False, "wallet not in watch set", extras
    # price ceiling
    if cand["reference_price"] > cfg["max_entry_price"]:
        return False, f"entry price {cand['reference_price']:.3f} > max {cfg['max_entry_price']:.2f}", extras
    # time remaining
    secs = _seconds_remaining(db, cand["market_id"], now)
    if secs is not None and secs < cfg["min_seconds_remaining"]:
        return False, f"only {secs:.0f}s remaining < min {cfg['min_seconds_remaining']:.0f}s", extras
    # regime filter (best-effort — allow when regime is unknown / unavailable)
    regime = None
    try:
        from . import market_intel
        regime = market_intel._regime_map(db).get(cand["market_id"])
    except Exception:  # noqa: BLE001  (MI not yet run / table absent -> regime unknown)
        regime = None
    extras["regime"] = regime
    if regime is not None and cfg["allowed_regimes"] and regime not in cfg["allowed_regimes"]:
        return False, f"regime '{regime}' not in allowed {cfg['allowed_regimes']}", extras
    # confidence gate (optional)
    if cfg["require_confidence"]:
        conf = _confidence_for(db, cand["market_id"])
        extras["confidence"] = conf
        if conf is None or conf < cfg["min_confidence"]:
            return False, f"confidence {conf} < min {cfg['min_confidence']}", extras
    return True, "ok", extras


# ---------------------------------------------------------------------------
# run-once pipeline
# ---------------------------------------------------------------------------
def _already_mirrored(db: Session, market_id: str) -> bool:
    return db.scalar(select(func.count()).select_from(mt.Btc5mMicroTestTrade)
                     .where(mt.Btc5mMicroTestTrade.market_id == market_id)) > 0


def _record_rejection(db: Session, st: mt.Btc5mMicroTestState, reason: str) -> None:
    st.last_rejection = f"{datetime.utcnow().isoformat()} — {reason}"
    db.commit()


def run_once(db: Session, *, place: bool = False, now: datetime | None = None,
             executor=None) -> dict:
    """One micro-test cycle. place=False => PAPER simulation (records a paper
    trade, no venue call). place=True => real execution via the shared safe path
    (DryRunExecutor or PolymarketExecutor per LIVE_EXECUTOR). Returns a summary."""
    now = now or datetime.utcnow()
    cfg = _cfg()
    if not cfg["enabled"]:
        return {"ran": False, "reason": "BTC5M micro-test disabled (BTC5M_MICRO_TEST_ENABLED=false)"}
    st = get_mt_state(db)
    if st.stopped:
        return {"ran": False, "reason": f"stopped — requires manual re-arm ({st.stop_reason})"}
    if not st.armed:
        return {"ran": False, "reason": "not armed — explicit arm required"}
    if not cfg["primary_wallet"]:
        return {"ran": False, "reason": "no primary wallet configured"}

    # respect GLOBAL safety: if general live trading is halted/paused, do nothing
    gstate = live.get_state(db)
    if gstate.halted:
        return {"ran": False, "reason": f"global trading halted/paused: {gstate.halt_reason}"}

    acct = _accounting(db)
    # auto-stop conditions (checked BEFORE acting)
    if acct["settled_trades"] >= cfg["max_trades"]:
        _stop(db, st, f"reached {cfg['max_trades']} settled test trades")
        return {"ran": False, "reason": st.stop_reason, "stopped": True}
    if acct["realized_pnl"] <= -cfg["total_loss_stop"]:
        _stop(db, st, f"total test loss stop (${cfg['total_loss_stop']:.0f}) hit")
        return {"ran": False, "reason": st.stop_reason, "stopped": True}
    if acct["day_realized_pnl"] <= -cfg["daily_loss_stop"]:
        _stop(db, st, f"daily test loss stop (${cfg['daily_loss_stop']:.0f}) hit")
        return {"ran": False, "reason": st.stop_reason, "stopped": True}
    # concurrency cap (skip, do not stop)
    if acct["open_positions"] >= cfg["max_concurrent"]:
        return {"ran": False, "reason": f"max concurrent test positions ({cfg['max_concurrent']}) open"}

    # find the first qualifying, not-yet-mirrored signal
    chosen = None
    last_reason = "no qualifying primary/backup signal in an open BTC 5M market"
    for cand in _candidate_signals(db, cfg, now):
        if _already_mirrored(db, cand["market_id"]):
            continue
        ok, reason, extras = _evaluate_gates(db, cfg, cand, now)
        if not ok:
            last_reason = f"{cand['market_id'][:10]}…: {reason}"
            continue
        chosen = (cand, extras)
        break
    if chosen is None:
        _record_rejection(db, st, last_reason)
        return {"ran": False, "reason": last_reason}

    cand, extras = chosen
    st.last_signal = (f"{now.isoformat()} — {cand['role']} {cand['wallet'][:10]}… "
                      f"{cand['direction']} @ {cand['reference_price']:.3f} ({cand['market_id'][:10]}…)")
    db.commit()

    # FIXED 5-share sizing — never scaled, never dynamic. stake = price * shares.
    shares = cfg["fixed_shares"]
    ref = cand["reference_price"]
    stake = round(ref * shares, 2)
    idem = f"{mt.STRATEGY_MODE}:{cand['market_id']}"
    expected_max_loss = stake

    # account safety: respect available venue cash (best-effort, real path only)
    if place:
        lcfg = live.get_config()
        if lcfg.executor == "polymarket":
            vb = live.venue_balance(executor="polymarket")
            cash = vb.get("available_usdc")
            if cash is not None and cash < stake:
                _record_rejection(db, st, f"insufficient venue cash ${cash} < stake ${stake}")
                return {"ran": False, "reason": f"insufficient venue cash (${cash} < ${stake})"}

    base = dict(idempotency_key=idem, market_id=cand["market_id"],
                market_question=(cand["market"].question or ""), outcome=cand["outcome"],
                direction=cand["direction"], wallet_triggered=cand["wallet"],
                wallet_role=cand["role"], regime=extras.get("regime"),
                confidence=extras.get("confidence"), reference_price=round(ref, 4),
                shares=shares, size_usd=stake,
                entry_reason=(f"copy {cand['role']} {cand['wallet'][:10]}… {cand['direction']} "
                              f"@ {ref:.3f}; 5-share micro-test"))

    if not place:
        # PAPER simulation — no venue call. Paper fill at the reference price.
        row = mt.Btc5mMicroTestTrade(**base, executor="paper", limit_price=round(ref, 4),
                                     fill_price=round(ref, 4), status="open",
                                     fill_outcome="paper", paper_fill_price=round(ref, 4))
        db.add(row)
        db.commit()
        return {"ran": True, "mode": "paper", "trade_id": row.id, "market_id": cand["market_id"],
                "direction": cand["direction"], "wallet": cand["wallet"], "role": cand["role"],
                "stake": stake, "shares": shares, "expected_max_loss": expected_max_loss}

    # REAL path — reuse the shared safe execution primitive only.
    lcfg = live.get_config()
    ex = executor or live.get_executor(lcfg)
    try:
        result = ex.place(db=db, market=cand["market"], outcome=cand["outcome"],
                          price=ref, size_usd=stake, cfg=lcfg)
    except live.ExecutionRejected as exc:
        outcome = exc.outcome or "rejected"
        is_error = bool(exc.venue_error) or outcome in _ERROR_OUTCOMES
        row = mt.Btc5mMicroTestTrade(**base, executor=lcfg.executor, status="rejected",
                                     fill_outcome=outcome, venue_error=exc.venue_error,
                                     rejection_reason=str(exc)[:200], paper_fill_price=round(ref, 4))
        db.add(row)
        db.commit()
        if is_error:                                   # any execution/venue error stops the test
            _stop(db, st, f"execution error: {outcome} — {str(exc)[:80]}")
            return {"ran": True, "mode": "live", "placed": False, "stopped": True,
                    "reason": f"execution error ({outcome})", "trade_id": row.id}
        return {"ran": True, "mode": "live", "placed": False, "reason": f"no fill: {outcome}",
                "trade_id": row.id}
    except Exception as exc:  # noqa: BLE001  (unexpected -> record + stop, fail closed)
        row = mt.Btc5mMicroTestTrade(**base, executor=lcfg.executor, status="rejected",
                                     fill_outcome="error", venue_error=live._full_err(exc),
                                     rejection_reason=str(exc)[:200])
        db.add(row)
        _stop(db, st, f"unexpected execution error: {str(exc)[:80]}")
        db.commit()
        return {"ran": True, "mode": "live", "placed": False, "stopped": True,
                "reason": "unexpected execution error", "trade_id": row.id}

    # filled / partially filled -> open a micro-test position for the actual fill
    filled_usd = round(result.filled_usd, 2)
    row = mt.Btc5mMicroTestTrade(
        **{**base, "size_usd": filled_usd, "shares": round(result.filled_shares, 4)},
        executor=lcfg.executor, limit_price=round(result.limit_price, 4),
        fill_price=round(result.fill_price, 4), fees=round(result.fees, 4),
        slippage=round((result.fill_price - ref) / ref, 4) if ref else 0.0,
        status="open", fill_outcome=result.outcome, order_id=result.order_id,
        tick_size=result.tick_size, min_order_size=result.min_order_size,
        venue_error=result.venue_error, paper_fill_price=round(ref, 4))
    db.add(row)
    db.commit()
    return {"ran": True, "mode": "live", "placed": True, "trade_id": row.id,
            "market_id": cand["market_id"], "direction": cand["direction"],
            "fill_price": result.fill_price, "shares": result.filled_shares,
            "stake": filled_usd, "expected_max_loss": filled_usd}


# ---------------------------------------------------------------------------
# settlement — ISOLATED. Settles micro-test positions against resolved markets
# and books P/L only within this module. NEVER touches LiveState/bankroll.
# ---------------------------------------------------------------------------
def settle(db: Session) -> dict:
    closed = 0
    now = datetime.utcnow()
    for t in db.scalars(select(mt.Btc5mMicroTestTrade)
                        .where(mt.Btc5mMicroTestTrade.status == "open")).all():
        m = db.get(Market, t.market_id)
        if not (m and m.resolved and m.resolved_outcome is not None):
            continue
        won = (m.resolved_outcome == t.outcome)
        payout = t.shares * (1.0 if won else 0.0)
        t.realized_pnl = round(payout - t.size_usd - (t.fees or 0.0), 2)
        t.won = won
        # paper twin P/L (binary payoff at the paper fill / reference price)
        pp = t.paper_fill_price if t.paper_fill_price is not None else t.reference_price
        t.paper_realized_pnl = round((1.0 - pp) if won else -pp, 4) * t.shares if pp else None
        t.status = "closed"
        t.closed_at = now
        t.settled_at = m.resolved_at or now
        closed += 1
    db.commit()
    # opportunistically apply auto-stop if a loss stop is now breached
    st = get_mt_state(db)
    if not st.stopped:
        cfg = _cfg()
        acct = _accounting(db)
        if acct["settled_trades"] >= cfg["max_trades"]:
            _stop(db, st, f"reached {cfg['max_trades']} settled test trades")
        elif acct["realized_pnl"] <= -cfg["total_loss_stop"]:
            _stop(db, st, f"total test loss stop (${cfg['total_loss_stop']:.0f}) hit")
        elif acct["day_realized_pnl"] <= -cfg["daily_loss_stop"]:
            _stop(db, st, f"daily test loss stop (${cfg['daily_loss_stop']:.0f}) hit")
    return {"closed": closed}


# ---------------------------------------------------------------------------
# dashboard status
# ---------------------------------------------------------------------------
def _trade_dict(t: mt.Btc5mMicroTestTrade) -> dict:
    return {"id": t.id, "created_at": t.created_at.isoformat() if t.created_at else None,
            "market_id": t.market_id, "market": t.market_question, "outcome": t.outcome,
            "direction": t.direction, "wallet": t.wallet_triggered, "role": t.wallet_role,
            "regime": t.regime, "confidence": t.confidence, "reference_price": t.reference_price,
            "fill_price": t.fill_price, "shares": t.shares, "size_usd": t.size_usd,
            "slippage": t.slippage, "status": t.status, "fill_outcome": t.fill_outcome,
            "order_id": t.order_id, "venue_error": t.venue_error,
            "rejection_reason": t.rejection_reason, "realized_pnl": t.realized_pnl,
            "paper_realized_pnl": t.paper_realized_pnl, "won": t.won, "executor": t.executor}


def status(db: Session) -> dict:
    cfg = _cfg()
    st = get_mt_state(db)
    acct = _accounting(db)
    rows = sorted(_trades(db), key=lambda t: t.created_at or datetime.min, reverse=True)
    active = [t for t in rows if t.status == "open"]
    loss_used = max(0.0, -acct["realized_pnl"])
    paper = acct["paper_realized_pnl"]
    return {
        "enabled": cfg["enabled"],
        "armed": st.armed,
        "stopped": st.stopped,
        "stop_reason": st.stop_reason,
        "armed_by": st.armed_by,
        "armed_at": st.armed_at.isoformat() if st.armed_at else None,
        "config": {
            "primary_wallet": cfg["primary_wallet"],
            "backup_wallets": cfg["backup_wallets"],
            "fixed_shares": cfg["fixed_shares"],
            "max_entry_price": cfg["max_entry_price"],
            "max_concurrent": cfg["max_concurrent"],
            "daily_loss_stop": cfg["daily_loss_stop"],
            "total_loss_stop": cfg["total_loss_stop"],
            "min_seconds_remaining": cfg["min_seconds_remaining"],
            "allowed_regimes": cfg["allowed_regimes"],
            "require_confidence": cfg["require_confidence"],
            "min_confidence": cfg["min_confidence"],
            "max_trades": cfg["max_trades"],
            "expected_max_loss_per_trade": round(cfg["max_entry_price"] * cfg["fixed_shares"], 2),
        },
        "active_position": _trade_dict(active[0]) if active else None,
        "test_trades": acct["settled_trades"],
        "open_positions": acct["open_positions"],
        "win_rate": acct["win_rate"],
        "realized_pnl": acct["realized_pnl"],
        "unrealized_pnl": _unrealized(db, active),
        "paper_realized_pnl": paper,
        "paper_vs_live_delta": round(acct["realized_pnl"] - paper, 2),
        "max_loss_remaining": round(cfg["total_loss_stop"] - loss_used, 2),
        "day_loss_remaining": round(cfg["daily_loss_stop"] - max(0.0, -acct["day_realized_pnl"]), 2),
        "trades_remaining": max(0, cfg["max_trades"] - acct["settled_trades"]),
        "last_signal": st.last_signal,
        "last_rejection": st.last_rejection,
        "recent_trades": [_trade_dict(t) for t in rows[:25]],
        "safety": ("isolated micro-test — separate table + accounting; reuses only the safe "
                   "execution path; never affects production copy, ranking, sizing, or bankroll"),
    }


def _unrealized(db: Session, active: list[mt.Btc5mMicroTestTrade]) -> float:
    """Mark open test positions: a binary contract is worth its current YES/NO
    price; absent a live quote we mark at the entry/fill price (cost) -> 0 P/L."""
    tot = 0.0
    for t in active:
        mark = (t.fill_price if t.fill_price is not None else t.reference_price) or 0.0
        tot += mark * t.shares - t.size_usd
    return round(tot, 2)
