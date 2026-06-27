"""Shadow Portfolio — 100% READ-ONLY simulation.

Simulates what WOULD have happened if we had copied promotion-candidate wallets,
WITHOUT placing real orders. It:
  * reads historical signal/decision data (LiveSignalDecision -> PaperSignal) and
    market resolutions, and computes a hypothetical fixed-unit copy per signal;
  * writes NOTHING to LiveExecution / positions / LiveState;
  * triggers NO live orders and changes NO trading logic, eligibility, ranking,
    sizing, slippage, order mode, pause/resume/halt, or settlement of real
    positions.

Built on the (unchanged) promotion module for the candidate set, so each shadow
wallet merges with its promotion score/status. Every value is clearly marked
simulated (`simulated: True`).
"""
from __future__ import annotations

import statistics
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import live_ranking, promotion
from .models import LiveSignalDecision, Market, PaperSignal, Wallet

# A pure normalization UNIT for the simulation — NOT a real-trade size. Each
# shadow copy stakes this many simulated dollars at the signal's observed price.
SHADOW_STAKE = 1.0


def _current_price(market: Market, outcome: str) -> float | None:
    """Mark-to-market price of `outcome` from stored market prices (read-only)."""
    try:
        outs = list(market.outcomes or [])
        prices = list(market.prices or [])
        if outcome in outs and len(prices) == len(outs):
            return float(prices[outs.index(outcome)])
    except Exception:  # noqa: BLE001
        pass
    return None


def _simulate_one(*, price: float, outcome: str, market: Market, edge, confidence,
                  entry_time) -> dict | None:
    """One hypothetical fixed-unit copy. Settled if the market resolved; else open
    and marked-to-market. Returns None for un-simulatable prices."""
    p = float(price or 0)
    if p <= 0 or p >= 1:
        return None
    shares = SHADOW_STAKE / p
    base = {
        "market_id": market.id, "outcome": outcome, "entry_price": round(p, 4),
        "stake": SHADOW_STAKE, "edge": edge, "confidence": confidence,
        "entry_time": entry_time,
    }
    if market.resolved and market.resolved_outcome is not None:
        won = market.resolved_outcome == outcome
        payout = shares * (1.0 if won else 0.0)
        realized = round(payout - SHADOW_STAKE, 4)
        hold = None
        if entry_time and market.resolved_at:
            hold = max(0.0, (market.resolved_at - entry_time).total_seconds() / 3600.0)
        return {**base, "status": "closed", "won": won, "realized_pl": realized,
                "unrealized_pl": 0.0, "settle_time": market.resolved_at, "hold_hours": hold}
    cur = _current_price(market, outcome)
    unreal = round((cur / p - 1.0) * SHADOW_STAKE, 4) if cur and cur > 0 else 0.0
    return {**base, "status": "open", "won": None, "realized_pl": 0.0,
            "unrealized_pl": unreal, "settle_time": None, "hold_hours": None}


def _max_drawdown(closed_sorted) -> float:
    cum = peak = mdd = 0.0
    for t in closed_sorted:
        cum += t["realized_pl"]
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return round(mdd, 4)


def _aggregate_wallet(trades, now) -> dict:
    closed = [t for t in trades if t["status"] == "closed"]
    open_ = [t for t in trades if t["status"] == "open"]
    closed_sorted = sorted(closed, key=lambda t: t["settle_time"] or datetime.min)
    realized = round(sum(t["realized_pl"] for t in closed), 4)
    unreal = round(sum(t["unrealized_pl"] for t in open_), 4)
    staked = round(len(trades) * SHADOW_STAKE, 4)
    wins = sum(1 for t in closed if t["won"])

    # market grouping for best/worst
    by_mkt: dict[str, float] = {}
    for t in trades:
        by_mkt[t["market_id"]] = round(by_mkt.get(t["market_id"], 0.0) + t["realized_pl"] + t["unrealized_pl"], 4)
    best_mkt = max(by_mkt.items(), key=lambda kv: kv[1], default=(None, 0.0))
    worst_mkt = min(by_mkt.items(), key=lambda kv: kv[1], default=(None, 0.0))

    def window_return(days):
        ws = [t for t in closed if t["settle_time"] and (now - t["settle_time"]).days < days]
        pl = round(sum(t["realized_pl"] for t in ws), 4)
        stk = len(ws) * SHADOW_STAKE
        return {"realized_pl": pl, "return_pct": round(pl / stk, 4) if stk else 0.0, "trades": len(ws)}

    edges = [float(t["edge"]) for t in trades if t["edge"] is not None]
    confs = [float(t["confidence"]) for t in trades if t["confidence"] is not None]
    holds = [t["hold_hours"] for t in closed if t["hold_hours"] is not None]
    entries = [t["entry_time"] for t in trades if t["entry_time"]]

    return {
        "simulated": True,
        "shadow_trades": len(trades),
        "simulated_wins": wins,
        "simulated_losses": len(closed) - wins,
        "open_positions": len(open_),
        "realized_pl": realized,
        "unrealized_pl": unreal,
        "total_pl": round(realized + unreal, 4),
        "staked": staked,
        "return_pct": round((realized + unreal) / staked, 4) if staked else 0.0,
        "win_rate": round(wins / len(closed), 4) if closed else None,
        "max_drawdown": _max_drawdown(closed_sorted),
        "avg_hold_hours": round(statistics.mean(holds), 1) if holds else None,
        "avg_edge": round(statistics.mean(edges), 4) if edges else 0.0,
        "avg_confidence": round(statistics.mean(confs), 1) if confs else 0.0,
        "best_market": best_mkt[0], "best_market_pl": best_mkt[1],
        "worst_market": worst_mkt[0], "worst_market_pl": worst_mkt[1],
        "return_7d": window_return(7), "return_30d": window_return(30),
        "return_all": {"realized_pl": realized, "return_pct": round(realized / staked, 4) if staked else 0.0,
                       "trades": len(closed)},
        "last_simulated_trade": max(entries).isoformat() if entries else None,
    }


def _aggregate_group(wallet_rows) -> dict:
    """Pooled shadow portfolio over a set of wallet result rows."""
    trades = sum(r["shadow_trades"] for r in wallet_rows)
    closed = sum(r["simulated_wins"] + r["simulated_losses"] for r in wallet_rows)
    wins = sum(r["simulated_wins"] for r in wallet_rows)
    realized = round(sum(r["realized_pl"] for r in wallet_rows), 4)
    unreal = round(sum(r["unrealized_pl"] for r in wallet_rows), 4)
    staked = round(sum(r["staked"] for r in wallet_rows), 4)
    return {
        "simulated": True, "wallets": len(wallet_rows), "shadow_trades": trades,
        "open_positions": sum(r["open_positions"] for r in wallet_rows),
        "realized_pl": realized, "unrealized_pl": unreal,
        "total_pl": round(realized + unreal, 4),
        "staked": staked, "return_pct": round((realized + unreal) / staked, 4) if staked else 0.0,
        "win_rate": round(wins / closed, 4) if closed else None,
        "max_drawdown": round(sum(r["max_drawdown"] for r in wallet_rows), 4),
    }


def shadow_portfolio(db: Session, *, min_signals: int = 2, limit: int = 200) -> dict:
    """Read-only shadow portfolio for promotion candidates."""
    now = datetime.utcnow()

    # candidate set + promotion score/status (read-only; production excluded there)
    promo = promotion.promotion_candidates(db, min_signals=min_signals, limit=100000)
    cand_by_addr = {c["wallet"]: c for c in promo["candidates"]}
    if not cand_by_addr:
        return {"simulated": True, "wallets": [], "aggregates": {},
                "stake_unit": SHADOW_STAKE, "thresholds": promo["thresholds"],
                "note": "Simulated only — no real orders, executions, or positions are affected."}

    # candidate decisions -> signals -> markets (the hypothetical copy opportunities)
    decs = db.scalars(select(LiveSignalDecision).where(
        LiveSignalDecision.category == "wallet_not_eligible",
        LiveSignalDecision.wallet_address.in_(set(cand_by_addr)))).all()
    sig_ids = {d.signal_id for d in decs if d.signal_id is not None}
    sigs = {s.id: s for s in db.scalars(select(PaperSignal).where(PaperSignal.id.in_(sig_ids)))} if sig_ids else {}
    mids = {s.market_id for s in sigs.values()}
    markets = {m.id: m for m in db.scalars(select(Market).where(Market.id.in_(mids)))} if mids else {}

    per_wallet: dict[str, list] = {}
    for d in decs:
        s = sigs.get(d.signal_id)
        if not s:
            continue
        m = markets.get(s.market_id)
        if not m:
            continue
        t = _simulate_one(price=s.observed_price, outcome=s.outcome, market=m,
                          edge=d.edge, confidence=d.confidence,
                          entry_time=d.created_at or s.created_at)
        if t:
            per_wallet.setdefault(d.wallet_address, []).append(t)

    rows = []
    for addr, trades in per_wallet.items():
        cand = cand_by_addr.get(addr, {})
        agg = _aggregate_wallet(trades, now)
        rows.append({
            "wallet": addr,
            "status": cand.get("status"),
            "promotion_score": cand.get("promotion_score"),
            "roi": cand.get("roi"), "profit_factor": cand.get("profit_factor"),
            "reason_rejected": cand.get("reason_rejected"),
            **agg,
        })
    rows.sort(key=lambda r: (r["total_pl"], r["shadow_trades"]), reverse=True)

    by_status = {st: [r for r in rows if r["status"] == st] for st in ("strong", "near", "watch")}
    aggregates = {
        "all_candidates": _aggregate_group(rows),
        "strong": _aggregate_group(by_status["strong"]),
        "near": _aggregate_group(by_status["near"]),
        "watch": _aggregate_group(by_status["watch"]),
        "production_baseline": _production_baseline(db, now),
    }
    best = max(rows, key=lambda r: r["total_pl"], default=None)
    worst = min(rows, key=lambda r: r["total_pl"], default=None)
    return {
        "simulated": True,
        "stake_unit": SHADOW_STAKE,
        "wallets": rows[:limit],
        "aggregates": aggregates,
        "best_candidate": {"wallet": best["wallet"], "total_pl": best["total_pl"]} if best else None,
        "worst_candidate": {"wallet": worst["wallet"], "total_pl": worst["total_pl"]} if worst else None,
        "thresholds": promo["thresholds"],
        "note": "Simulated only — no real orders, executions, or positions are affected.",
    }


def _production_baseline(db: Session, now) -> dict:
    """Read-only baseline: simulate the SAME fixed-unit copy over the production-
    eligible wallets' own signals, for comparison. Never touches real trades."""
    eligible = live_ranking.eligible_addresses(db)
    if not eligible:
        return _aggregate_group([])
    wallets = {w.id: w.address for w in db.scalars(
        select(Wallet).where(Wallet.address.in_(eligible)))}
    if not wallets:
        return _aggregate_group([])
    sigs = db.scalars(select(PaperSignal).where(PaperSignal.wallet_id.in_(wallets))).all()
    mids = {s.market_id for s in sigs}
    markets = {m.id: m for m in db.scalars(select(Market).where(Market.id.in_(mids)))} if mids else {}
    per_wallet: dict[str, list] = {}
    for s in sigs:
        m = markets.get(s.market_id)
        if not m:
            continue
        t = _simulate_one(price=s.observed_price, outcome=s.outcome, market=m,
                          edge=s.edge_estimate, confidence=s.confidence, entry_time=s.created_at)
        if t:
            per_wallet.setdefault(wallets[s.wallet_id], []).append(t)
    return _aggregate_group([_aggregate_wallet(ts, now) for ts in per_wallet.values()])
