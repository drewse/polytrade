"""
Historical replay engine (Phases 21-24).

Reconstructs what the live system WOULD have done over historical Polymarket
data, chronologically, with NO look-ahead: a wallet's reputation at signal time
uses only positions that had resolved BEFORE that signal; the market outcome is
used solely to LABEL the realized result (supervised data), never inside a
decision. Resumable via a ReplayState checkpoint. STRICTLY PAPER ONLY —
deterministic, no orders, no signing.

Pipeline per historical wallet trade on a (now-)resolved market:
    point-in-time wallet reputation  ->  signal  ->  20 strategies decide
      ->  fractional-Kelly size  ->  paper entry  ->  settle at known resolution
      ->  labeled feature vector (source='replay')
"""
from __future__ import annotations

import bisect
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import positions as positions_mod
from ..models import (
    Market,
    ReplayState,
    Top20FeatureVector,
    Top20Strategy,
    Top20Trade,
    Trade,
    Wallet,
)
from . import probability
from .explain import build_entry
from .sizing import size as size_position
from .strategies import CONFIG_BY_KEY, STRATEGIES, Ctx, Shared, categorize, decide

# Replay gates (slightly looser than live to maximize the dataset; the per-
# strategy filters still apply). Documented for transparency.
MIN_PRIOR_SETTLED = 5     # wallet needs this many resolved positions before it can signal
MIN_SCORE = 55.0          # running (point-in-time) wallet score to emit a signal
MIN_LIQUIDITY = 300.0
MIN_SIZE = 15.0


def get_state(db: Session) -> ReplayState:
    st = db.get(ReplayState, 1)
    if st is None:
        st = ReplayState(id=1)
        db.add(st)
        db.commit()
    return st


# ---------------------------------------------------------------------------
# Phase 21 — bulk closed-market backfill (real metadata, no placeholders)
# ---------------------------------------------------------------------------
def backfill_closed_markets(db: Session, pages: int = 3, page_size: int = 100) -> dict:
    from ..services import upsert_market
    from ..polymarket_client import LivePolymarketClient

    st = get_state(db)
    st.status = "backfilling_markets"
    client = LivePolymarketClient()
    added = resolved = 0
    errors = []
    try:
        for _ in range(pages):
            try:
                markets = client.get_closed_markets(limit=page_size, offset=st.markets_offset)
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
                break
            if not markets:
                break
            for mdto in markets:
                upsert_market(db, mdto)
                added += 1
                if mdto.resolved:
                    resolved += 1
            st.markets_offset += page_size
            db.commit()
    finally:
        client.close()
    st.markets_backfilled += added
    st.status = "idle"
    db.commit()
    total_resolved = db.scalar(select(func.count()).select_from(Market).where(
        Market.resolved == True))  # noqa: E712
    return {"paper_only": True, "fetched": added, "resolved_in_batch": resolved,
            "offset": st.markets_offset, "total_resolved_markets": int(total_resolved or 0),
            "errors": errors}


def backfill_wallets(db: Session, max_wallets: int = 5) -> dict:
    """Reuse the live discovery+backfill to pull more wallets' historical trades."""
    from .. import services

    st = get_state(db)
    st.status = "backfilling_wallets"
    db.commit()
    settings = services.get_settings(db)
    if settings["data_mode"] != "live":
        st.status = "idle"; db.commit()
        return {"ok": False, "error": "backfill requires data_mode=live"}
    res = services.run_discovery(db, max_backfill=max_wallets)
    st.wallets_backfilled += res.get("backfilled", 0)
    st.status = "idle"
    db.commit()
    return {"paper_only": True, "discovery": res, "wallets_backfilled_total": st.wallets_backfilled}


# ---------------------------------------------------------------------------
# Point-in-time wallet reputation (no look-ahead)
# ---------------------------------------------------------------------------
def _running_score(n: int, win_rate: float, roi: float) -> float:
    def clip(x):
        return max(0.0, min(1.0, x))
    return round(100 * (0.40 * clip(0.5 + roi) + 0.35 * clip((win_rate - 0.45) / 0.35)
                        + 0.25 * clip(n / 40.0)), 1)


def _build_wallet_timelines(db: Session) -> dict[int, dict]:
    """Per wallet: settled positions sorted by RESOLUTION time, with cumulative
    arrays so a point-in-time lookup at any timestamp is O(log n)."""
    wallets = db.scalars(select(Wallet)).all()
    timelines: dict[int, dict] = {}
    for w in wallets:
        trades = db.scalars(select(Trade).where(Trade.wallet_id == w.id)).all()
        mids = {t.market_id for t in trades}
        markets = {m.id: m for m in db.scalars(select(Market).where(Market.id.in_(mids))).all()}
        settled = positions_mod.settled_positions(trades, markets)
        events = []
        for p in settled:
            m = markets.get(p.market_id)
            res_t = m.resolved_at if (m and m.resolved_at) else p.timestamp
            events.append((res_t, p.realized_pnl, p.size))
        events.sort(key=lambda e: e[0])
        times, cum_pnl, cum_cost, cum_wins = [], [], [], []
        tp = tc = tw = 0.0
        wcount = 0
        for res_t, pnl, size in events:
            tp += pnl; tc += size; wcount += (1 if pnl > 0 else 0)
            times.append(res_t); cum_pnl.append(tp); cum_cost.append(tc); cum_wins.append(wcount)
        timelines[w.id] = {"times": times, "pnl": cum_pnl, "cost": cum_cost, "wins": cum_wins,
                           "address": w.address}
    return timelines


def _point_in_time(tl: dict, when: datetime) -> dict:
    """Wallet stats considering only positions resolved strictly before `when`."""
    times = tl["times"]
    i = bisect.bisect_left(times, when)   # positions [0, i) resolved before `when`
    if i == 0:
        return {"n": 0, "win_rate": 0.0, "roi": 0.0, "score": 0.0}
    pnl = tl["pnl"][i - 1]
    cost = tl["cost"][i - 1] or 1.0
    wins = tl["wins"][i - 1]
    n = i
    win_rate = wins / n
    roi = pnl / cost
    return {"n": n, "win_rate": round(win_rate, 4), "roi": round(roi, 4),
            "score": _running_score(n, win_rate, roi)}


# ---------------------------------------------------------------------------
# Phase 23/24 — chronological replay producing labeled feature vectors
# ---------------------------------------------------------------------------
def run(db: Session, max_trades: int = 400) -> dict:
    st = get_state(db)
    st.status = "replaying"
    db.commit()

    timelines = _build_wallet_timelines(db)
    strategies = {s.key: s for s in db.scalars(select(Top20Strategy)).all()}
    if not strategies:
        from .engine import ensure_strategies
        ensure_strategies(db)
        strategies = {s.key: s for s in db.scalars(select(Top20Strategy)).all()}

    # candidate trades: on resolved, liquid markets, traded before resolution,
    # not yet replayed (id > checkpoint). Process in id batches (point-in-time
    # rep makes order irrelevant to correctness).
    rows = db.execute(
        select(Trade, Market).join(Market, Trade.market_id == Market.id)
        .where(Trade.id > st.last_event_id, Market.resolved == True,  # noqa: E712
               Market.resolved_outcome.is_not(None), Market.liquidity >= MIN_LIQUIDITY,
               Trade.size >= MIN_SIZE)
        .order_by(Trade.id).limit(max_trades)
    ).all()

    signals = entered = 0
    last_id = st.last_event_id
    for trade, market in rows:
        last_id = max(last_id, trade.id)
        if market.resolved_at and trade.timestamp and market.resolved_at <= trade.timestamp:
            continue  # trade after resolution -> skip (shouldn't happen)
        tl = timelines.get(trade.wallet_id)
        if tl is None:
            continue
        rep = _point_in_time(tl, trade.timestamp)
        if rep["n"] < MIN_PRIOR_SETTLED or rep["score"] < MIN_SCORE:
            continue
        signals += 1
        st.signals_generated += 1
        # point-in-time rank: how many wallets had a higher running score then
        rank = sum(1 for wid, otl in timelines.items()
                   if wid != trade.wallet_id and _point_in_time(otl, trade.timestamp)["score"] > rep["score"])
        price = float(trade.price or 0.5)
        edge = round(rep["win_rate"] - price, 4)
        category = categorize(market.question, market.category)
        ctx = Ctx(wallet_id=trade.wallet_id, classification=_class(rep["score"]),
                  confidence=rep["score"], edge=edge, liquidity=float(market.liquidity or 0),
                  age_min=0.0, price=price, outcome=trade.outcome, market_id=market.id,
                  category=category, win_rate=rep["win_rate"], sharpe=rep["roi"] / 0.3,
                  roi=rep["roi"], copyability=rep["score"], specialization=0.0,
                  recency=1.0, num_settled=rep["n"])
        shared = Shared(rank_copyability={trade.wallet_id: rank}, rank_sharpe={trade.wallet_id: rank},
                        rank_roi={trade.wallet_id: rank}, rank_active={trade.wallet_id: rank},
                        edge_pct_threshold=0.0, consensus=set())
        won = market.resolved_outcome == trade.outcome
        for d in STRATEGIES:
            strat = strategies.get(d.key)
            if strat is None:
                continue
            admit, _why = decide(d, ctx, shared)
            if not admit:
                continue
            p = probability.estimate(probability.ProbFeatures(
                market_price=price, edge=edge, win_rate=rep["win_rate"], sharpe=ctx.sharpe,
                roi=rep["roi"], confidence=rep["score"], specialization=0.0,
                liquidity=ctx.liquidity, num_settled=rep["n"]))
            res = size_position(d.sizing, price=price, p=p, bankroll=strat.starting_bankroll,
                                market_exposure_used=0.0, confidence=rep["score"], edge=edge,
                                quality=rep["score"])
            if res.stake is None:
                continue
            exit_price = 1.0 if won else 0.0
            realized = round(res.shares * exit_price - res.stake, 2)
            hold_min = round((market.resolved_at - trade.timestamp).total_seconds() / 60.0, 1) \
                if market.resolved_at else None
            t = Top20Trade(
                strategy_id=strat.id, signal_id=None, wallet_address=tl["address"],
                market_id=market.id, market_question=market.question or "", outcome=trade.outcome,
                side="buy", entry_price=round(price, 4), size_shares=res.shares, stake=res.stake,
                estimated_probability=round(p, 4), kelly_fraction=res.kelly_fraction,
                fractional_kelly_used=d.sizing.kelly_multiplier, sizing_reason=res.reason,
                entry_time=trade.timestamp, status="closed", source="replay",
                current_price=exit_price, exit_price=exit_price, realized_pnl=realized,
                unrealized_pnl=0.0, closed_at=market.resolved_at, holding_minutes=hold_min,
                exit_reason="resolved", entry_confidence=rep["score"], entry_edge=edge,
                wallet_rank=rank, explanation=build_entry(ctx, res, p, d.exit_policy, rank))
            db.add(t)
            db.flush()
            db.add(Top20FeatureVector(
                strategy_id=strat.id, strategy_key=strat.key, signal_id=None, trade_id=t.id,
                decision="take", source="replay", settled=True,
                label_outcome=market.resolved_outcome,
                label_realized_return=round(realized / res.stake, 4) if res.stake else 0.0,
                label_realized_pnl=realized, label_exit_reason="resolved",
                features={
                    "confidence": rep["score"], "edge": edge, "price": price,
                    "wallet_win_rate": rep["win_rate"], "wallet_sharpe": round(ctx.sharpe, 3),
                    "wallet_roi": rep["roi"], "copyability": rep["score"], "wallet_rank": rank,
                    "num_settled": rep["n"], "liquidity": ctx.liquidity, "category": category,
                    "estimated_probability": round(p, 4), "kelly_fraction": res.kelly_fraction,
                    "position_size": res.stake, "entry": round(price, 4), "exit": exit_price,
                    "holding_minutes": hold_min, "realized_return": round(realized / res.stake, 4) if res.stake else 0.0,
                    "drawdown_contribution": round(min(0.0, realized / res.stake), 4) if res.stake else 0.0,
                    "resolution_result": "win" if won else "loss",
                }))
            entered += 1
            st.feature_vectors += 1
        st.events_processed += 1
        if entered and entered % 200 == 0:
            db.commit()

    st.last_event_id = last_id
    st.status = "idle"
    db.commit()
    return {"paper_only": True, "signals_generated": signals, "feature_vectors_added": entered,
            "checkpoint_trade_id": last_id, "total_feature_vectors": st.feature_vectors}


def _class(score: float) -> str:
    if score >= 79:
        return "elite_candidate"
    if score >= 60:
        return "good_candidate"
    if score >= 40:
        return "watchlist"
    return "ignore"


def status(db: Session) -> dict:
    st = get_state(db)
    fv_total = db.scalar(select(func.count()).select_from(Top20FeatureVector))
    fv_labeled = db.scalar(select(func.count()).select_from(Top20FeatureVector).where(
        Top20FeatureVector.settled == True))  # noqa: E712
    fv_replay = db.scalar(select(func.count()).select_from(Top20FeatureVector).where(
        Top20FeatureVector.source == "replay"))
    resolved_markets = db.scalar(select(func.count()).select_from(Market).where(
        Market.resolved == True))  # noqa: E712
    replay_trades = db.scalar(select(func.count()).select_from(Top20Trade).where(
        Top20Trade.source == "replay"))
    wallets = db.scalar(select(func.count()).select_from(Wallet))
    return {
        "paper_only": True, "status": st.status,
        "markets_backfilled": st.markets_backfilled, "markets_offset": st.markets_offset,
        "resolved_markets": int(resolved_markets or 0),
        "wallets": int(wallets or 0), "wallets_backfilled": st.wallets_backfilled,
        "events_processed": st.events_processed, "signals_generated": st.signals_generated,
        "checkpoint_trade_id": st.last_event_id,
        "feature_vectors_total": int(fv_total or 0), "feature_vectors_labeled": int(fv_labeled or 0),
        "feature_vectors_replay": int(fv_replay or 0), "replay_paper_trades": int(replay_trades or 0),
        "targets": {"markets": 5000, "feature_vectors": 10000, "resolved_positions": 1000},
    }


def reset(db: Session) -> dict:
    db.query(Top20FeatureVector).filter(Top20FeatureVector.source == "replay").delete()
    db.query(Top20Trade).filter(Top20Trade.source == "replay").delete()
    st = get_state(db)
    st.last_event_id = 0; st.events_processed = 0; st.signals_generated = 0; st.feature_vectors = 0
    st.status = "idle"
    db.commit()
    return {"paper_only": True, "reset": True}
