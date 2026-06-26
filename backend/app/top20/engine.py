"""
TOP 20 engine (Phase 10 orchestration).

Wires the isolated components together — strategies (entry), probability,
sizing, exits, analytics, leaderboard, explainability — and owns all DB access.
Every component above this file is DB-free and swappable (e.g. replace the
probability estimator without touching anything here). STRICTLY PAPER ONLY.
"""
from __future__ import annotations

import hashlib
import json
import statistics
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import positions as positions_mod
from ..models import (
    Market,
    PaperSignal,
    Top20FeatureVector,
    Top20Snapshot,
    Top20Strategy,
    Top20Trade,
    Trade,
    Wallet,
    WalletCandidate,
    WalletStat,
)
from . import (
    analytics,
    benchmark,
    ensembles,
    exits,
    leaderboard as lb,
    market_intel,
    montecarlo,
    optimize,
    probability,
    replay as replay_mod,
    reputation,
    report as report_mod,
    simulate,
)
from .explain import build_entry
from .sizing import size as size_position
from .strategies import STRATEGIES, CONFIG_BY_KEY, Ctx, Shared, categorize, decide


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
def _param_hash(params: dict) -> str:
    """Stable hash of a strategy's parameters — reproducibility id (Phase 11).
    Identical params => identical hash => identical decisions (decide() is pure)."""
    return hashlib.md5(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:16]


def ensure_strategies(db: Session) -> list[Top20Strategy]:
    existing = {s.key: s for s in db.scalars(select(Top20Strategy)).all()}
    valid_keys = set(CONFIG_BY_KEY)
    changed = False
    # Prune strategies that are no longer defined (e.g. after a strategy-set
    # change) so the lab always shows exactly the current 20. Removes their
    # paper trades + snapshots too.
    for key, row in list(existing.items()):
        if key not in valid_keys:
            db.query(Top20Trade).filter(Top20Trade.strategy_id == row.id).delete()
            db.query(Top20Snapshot).filter(Top20Snapshot.strategy_id == row.id).delete()
            db.delete(row)
            changed = True
    for d in STRATEGIES:
        params = d.to_params()
        row = existing.get(d.key)
        if row is None:
            db.add(Top20Strategy(
                key=d.key, name=d.name, description=d.description, philosophy=d.philosophy,
                exit_policy=d.exit_policy, starting_bankroll=10_000.0,
                fractional_kelly=d.sizing.kelly_multiplier, params=params,
                param_hash=_param_hash(params), status="production",
            ))
            changed = True
        else:
            row.name, row.description, row.philosophy = d.name, d.description, d.philosophy
            row.exit_policy = d.exit_policy
            row.fractional_kelly = d.sizing.kelly_multiplier
            row.params = params
            row.param_hash = _param_hash(params)
    if changed:
        db.commit()
    return list(db.scalars(select(Top20Strategy).order_by(Top20Strategy.id)).all())


# ---------------------------------------------------------------------------
# Wallet metrics + cohort rankings
# ---------------------------------------------------------------------------
def _wallet_metrics(db: Session) -> dict[int, dict]:
    """Per-wallet metrics used by selectors + the probability model.

    Sharpe is a documented PROXY from persisted stats (we don't store a wallet
    return series): sharpe ~= realized_roi / ((1 - consistency) + 0.15) — higher
    consistency (steadier wins) lowers the denominator -> higher Sharpe."""
    stats = {s.wallet_id: s for s in db.scalars(select(WalletStat)).all()}
    cands = {c.wallet_id: c for c in db.scalars(select(WalletCandidate)).all()}
    out: dict[int, dict] = {}
    for wid, st in stats.items():
        cand = cands.get(wid)
        consistency = float(st.consistency or 0.0)
        roi = float(st.realized_roi or 0.0)
        sharpe = round(roi / ((1.0 - consistency) + 0.15), 3)
        spec = max((st.category_performance or {}).values(), default=0.0)
        out[wid] = {
            "win_rate": float(st.win_rate or 0.0),
            "roi": roi,
            "sharpe": sharpe,
            "copyability": float(cand.copyability_score) if cand else 0.0,
            "classification": cand.classification if cand else None,
            "recency": float(st.recency_score or 0.0),
            "specialization": float(spec),
            "activity": int(st.num_trades or 0),
            "num_settled": int(st.num_settled or 0),
        }
    return out


def _rank_by(wm: dict[int, dict], key: str, require_candidate=False,
             cands: set | None = None) -> dict[int, int]:
    items = [(wid, m[key]) for wid, m in wm.items()
             if (not require_candidate or (cands and wid in cands))]
    items.sort(key=lambda kv: kv[1], reverse=True)
    return {wid: i for i, (wid, _) in enumerate(items)}


def _build_shared(db: Session, signals: list[PaperSignal], wm: dict[int, dict]) -> Shared:
    cand_ids = {c for (c,) in db.execute(
        select(WalletCandidate.wallet_id).where(
            WalletCandidate.classification != "insufficient_data")).all()}
    edges = sorted(float(s.edge_estimate or 0.0) for s in signals)
    if edges:
        idx = int(0.90 * (len(edges) - 1))
        edge_pct_threshold = edges[idx]
    else:
        edge_pct_threshold = 0.0
    # consensus: (market, outcome) with >= 2 distinct wallets in this batch
    pair_wallets: dict[tuple, set] = {}
    for s in signals:
        pair_wallets.setdefault((s.market_id, s.outcome), set()).add(s.wallet_id)
    consensus = {k for k, ws in pair_wallets.items() if len(ws) >= 2}
    return Shared(
        rank_copyability=_rank_by(wm, "copyability", require_candidate=True, cands=cand_ids),
        rank_sharpe=_rank_by(wm, "sharpe"),
        rank_roi=_rank_by(wm, "roi"),
        rank_active=_rank_by(wm, "activity"),
        edge_pct_threshold=edge_pct_threshold,
        consensus=consensus,
    )


# ---------------------------------------------------------------------------
# Evaluation (entry)
# ---------------------------------------------------------------------------
def _bankroll(db: Session, strat: Top20Strategy) -> float:
    realized = db.scalar(select(func.coalesce(func.sum(Top20Trade.realized_pnl), 0.0)).where(
        Top20Trade.strategy_id == strat.id, Top20Trade.status == "closed"))
    return strat.starting_bankroll + float(realized or 0.0)


def evaluate_signals(db: Session, settings: dict | None = None) -> dict:
    strategies = ensure_strategies(db)
    min_wm = min((s.last_signal_id for s in strategies), default=0)
    signals = db.scalars(select(PaperSignal).where(PaperSignal.id > min_wm)
                         .order_by(PaperSignal.id)).all()
    if not signals:
        return {"evaluated": 0, "entered": 0, "signals": 0}

    wm = _wallet_metrics(db)
    shared = _build_shared(db, signals, wm)
    wallets = {w.id: w for w in db.scalars(select(Wallet)).all()}
    mids = {s.market_id for s in signals}
    markets = {m.id: m for m in db.scalars(select(Market).where(Market.id.in_(mids))).all()}
    now = datetime.utcnow()

    # precompute per-signal context (shared across strategies)
    ctxs: dict[int, Ctx] = {}
    for s in signals:
        m = markets.get(s.market_id)
        w = wallets.get(s.wallet_id)
        if not (m and w) or m.resolved:
            continue
        mt = wm.get(s.wallet_id, {})
        ctxs[s.id] = Ctx(
            wallet_id=s.wallet_id, classification=mt.get("classification"),
            confidence=float(s.confidence or 0.0), edge=float(s.edge_estimate or 0.0),
            liquidity=float(m.liquidity or 0.0),
            age_min=(now - s.created_at).total_seconds() / 60.0,
            price=float(s.observed_price or 0.5), outcome=s.outcome, market_id=s.market_id,
            category=categorize(m.question, m.category),
            win_rate=mt.get("win_rate", 0.0), sharpe=mt.get("sharpe", 0.0),
            roi=mt.get("roi", 0.0), copyability=mt.get("copyability", 0.0),
            specialization=mt.get("specialization", 0.0), recency=mt.get("recency", 0.0),
            num_settled=mt.get("num_settled", 0),
        )

    total_eval = total_entered = 0
    last_id = signals[-1].id
    for strat in strategies:
        d = CONFIG_BY_KEY[strat.key]
        bankroll = _bankroll(db, strat)
        exposure: dict[str, float] = {}
        for t in db.scalars(select(Top20Trade).where(
                Top20Trade.strategy_id == strat.id, Top20Trade.status == "open")).all():
            exposure[t.market_id] = exposure.get(t.market_id, 0.0) + t.stake
        seen = set(db.scalars(select(Top20Trade.signal_id).where(
            Top20Trade.strategy_id == strat.id)).all())

        for s in signals:
            if s.id <= strat.last_signal_id:
                continue
            strat.signals_evaluated += 1
            total_eval += 1
            ctx = ctxs.get(s.id)
            if ctx is None or s.id in seen:
                continue
            admit, _why = decide(d, ctx, shared)
            if not admit:
                continue
            p = probability.estimate(probability.ProbFeatures(
                market_price=ctx.price, edge=ctx.edge, win_rate=ctx.win_rate,
                sharpe=ctx.sharpe, roi=ctx.roi, confidence=ctx.confidence,
                specialization=ctx.specialization, liquidity=ctx.liquidity,
                num_settled=ctx.num_settled))
            res = size_position(d.sizing, price=ctx.price, p=p, bankroll=bankroll,
                                market_exposure_used=exposure.get(ctx.market_id, 0.0),
                                confidence=ctx.confidence, edge=ctx.edge, quality=ctx.copyability)
            if res.stake is None:
                continue
            rank = shared.rank_copyability.get(ctx.wallet_id)
            trade = Top20Trade(
                strategy_id=strat.id, signal_id=s.id, wallet_address=wallets[s.wallet_id].address,
                market_id=ctx.market_id, market_question=markets[ctx.market_id].question or "",
                outcome=ctx.outcome, side=s.side or "buy", entry_price=round(ctx.price, 4),
                size_shares=res.shares, stake=res.stake, estimated_probability=round(p, 4),
                kelly_fraction=res.kelly_fraction, fractional_kelly_used=d.sizing.kelly_multiplier,
                sizing_reason=res.reason, entry_time=now, status="open",
                current_price=round(ctx.price, 4), entry_confidence=ctx.confidence,
                entry_edge=ctx.edge, wallet_rank=rank,
                explanation=build_entry(ctx, res, p, d.exit_policy, rank),
            )
            db.add(trade)
            db.flush()  # assign trade.id for the feature vector link
            # Phase 20: persist a labeled feature vector (label filled at settle).
            db.add(Top20FeatureVector(
                strategy_id=strat.id, strategy_key=strat.key, signal_id=s.id, trade_id=trade.id,
                decision="take",
                features={
                    "confidence": ctx.confidence, "edge": ctx.edge, "price": ctx.price,
                    "wallet_win_rate": ctx.win_rate, "wallet_sharpe": ctx.sharpe,
                    "wallet_roi": ctx.roi, "copyability": ctx.copyability,
                    "specialization": ctx.specialization, "recency": ctx.recency,
                    "num_settled": ctx.num_settled, "wallet_rank": rank,
                    "liquidity": ctx.liquidity, "category": ctx.category, "age_min": round(ctx.age_min, 1),
                    "estimated_probability": round(p, 4), "kelly_fraction": res.kelly_fraction,
                    "target_fraction": res.target_fraction, "position_size": res.stake,
                }))
            exposure[ctx.market_id] = exposure.get(ctx.market_id, 0.0) + res.stake
            seen.add(s.id)
            strat.trades_entered += 1
            total_entered += 1
        strat.last_signal_id = last_id
    db.commit()
    return {"evaluated": total_eval, "entered": total_entered, "signals": len(signals)}


# ---------------------------------------------------------------------------
# Settle / mark (exit)
# ---------------------------------------------------------------------------
def _wallet_exited(db: Session, trade: Top20Trade) -> bool:
    """For mirror exits: has the copied wallet SOLD this market+outcome since entry?"""
    w = db.scalar(select(Wallet).where(Wallet.address == trade.wallet_address))
    if not w:
        return False
    sells = db.scalar(select(func.count()).select_from(Trade).where(
        Trade.wallet_id == w.id, Trade.market_id == trade.market_id,
        Trade.outcome == trade.outcome, Trade.side == "sell",
        Trade.timestamp >= trade.entry_time))
    return bool(sells and sells > 0)


def settle_and_mark(db: Session) -> dict:
    open_trades = db.scalars(select(Top20Trade).where(Top20Trade.status == "open")).all()
    closed = marked = exited = 0
    now = datetime.utcnow()
    for t in open_trades:
        market = db.get(Market, t.market_id)
        if market is None:
            continue
        strat = db.get(Top20Strategy, t.strategy_id)
        policy = strat.exit_policy if strat else "hold"
        if market.resolved and market.resolved_outcome is not None:
            won = market.resolved_outcome == t.outcome
            _close(t, 1.0 if won else 0.0, "resolved", now)
            _label_fv(db, t, market.resolved_outcome)
            closed += 1
            continue
        price = market.price_for(t.outcome)
        if price is not None:
            t.current_price = round(float(price), 4)
            t.unrealized_pnl = round(t.size_shares * float(price) - t.stake, 2)
            marked += 1
        # early-exit policies
        unreal_ret = (t.unrealized_pnl / t.stake) if t.stake else 0.0
        holding_min = (now - t.entry_time).total_seconds() / 60.0
        wallet_exited = _wallet_exited(db, t) if exits.needs_wallet_tracking(policy) else False
        dec = exits.decide(policy, unrealized_return=unreal_ret,
                           holding_minutes=holding_min, wallet_exited=wallet_exited)
        if dec.close and t.current_price is not None:
            _close(t, t.current_price, dec.reason or policy, now)
            _label_fv(db, t, t.outcome if t.exit_price and t.exit_price >= 0.5 else None)
            closed += 1
            exited += 1
    db.commit()
    return {"closed": closed, "marked": marked, "early_exits": exited}


def _label_fv(db: Session, trade: Top20Trade, outcome: str | None) -> None:
    """Phase 20: write the realized label onto the trade's feature vector."""
    fv = db.scalar(select(Top20FeatureVector).where(Top20FeatureVector.trade_id == trade.id))
    if fv is None:
        return
    fv.label_outcome = outcome
    fv.label_realized_pnl = trade.realized_pnl
    fv.label_realized_return = round(trade.realized_pnl / trade.stake, 4) if trade.stake else 0.0
    fv.label_exit_reason = trade.exit_reason
    fv.settled = True


def _close(t: Top20Trade, exit_price: float, reason: str, now: datetime) -> None:
    t.exit_price = exit_price
    t.current_price = exit_price
    t.realized_pnl = round(t.size_shares * exit_price - t.stake, 2)
    t.unrealized_pnl = 0.0
    t.status = "closed"
    t.closed_at = now
    t.exit_reason = reason
    if t.entry_time:
        t.holding_minutes = round((now - t.entry_time).total_seconds() / 60.0, 1)


# ---------------------------------------------------------------------------
# Snapshots + metrics persistence
# ---------------------------------------------------------------------------
def _equity_curve(db: Session, strategy_id: int) -> list[float]:
    return list(db.scalars(select(Top20Snapshot.equity).where(
        Top20Snapshot.strategy_id == strategy_id).order_by(Top20Snapshot.timestamp)).all())


def _trade_path_curve(closed: list, open_: list, starting_bankroll: float) -> list[float]:
    """Equity curve built from the CHRONOLOGICAL sequence of closed trades
    (cumulative realized P&L), so drawdown reflects the actual trade path — not
    sparse wall-clock snapshots that miss the replay sequence entirely."""
    ordered = sorted(closed, key=lambda t: (t.closed_at or t.entry_time or datetime.min))
    curve = [starting_bankroll]
    cum = starting_bankroll
    for t in ordered:
        cum += t.realized_pnl
        curve.append(round(cum, 2))
    if open_:
        curve.append(round(cum + sum(t.unrealized_pnl for t in open_), 2))
    return curve


def _metrics_for(db: Session, strat: Top20Strategy) -> dict:
    trades = db.scalars(select(Top20Trade).where(Top20Trade.strategy_id == strat.id)).all()
    closed = [t for t in trades if t.status == "closed"]
    open_ = [t for t in trades if t.status == "open"]
    # Drawdown/consistency from the true trade path (cumulative realized P&L),
    # NOT from wall-clock snapshots which don't include the replay sequence.
    curve = _trade_path_curve(closed, open_, strat.starting_bankroll)
    times = [t.entry_time for t in trades if t.entry_time]
    m = analytics.compute_metrics(
        closed, open_, curve, strat.starting_bankroll,
        signals_seen=strat.signals_evaluated, signals_taken=strat.trades_entered,
        first_ts=min(times) if times else None, last_ts=max(times) if times else None)
    return m


def snapshot(db: Session) -> int:
    # One shared timestamp per cycle so the portfolio can sum across strategies
    # at each point (distinct per-row timestamps caused a jagged, false-drawdown
    # combined curve).
    now = datetime.utcnow()
    n = 0
    for strat in db.scalars(select(Top20Strategy)).all():
        trades = db.scalars(select(Top20Trade).where(Top20Trade.strategy_id == strat.id)).all()
        closed = [t for t in trades if t.status == "closed"]
        open_ = [t for t in trades if t.status == "open"]
        realized = round(sum(t.realized_pnl for t in closed), 2)
        unreal = round(sum(t.unrealized_pnl for t in open_), 2)
        bankroll = strat.starting_bankroll + realized
        db.add(Top20Snapshot(strategy_id=strat.id, timestamp=now, bankroll=round(bankroll, 2),
                             equity=round(bankroll + unreal, 2), realized_pnl=realized,
                             unrealized_pnl=unreal, open_positions=len(open_)))
        n += 1
    db.commit()
    return n


def _persist_metrics(db: Session) -> None:
    for strat in db.scalars(select(Top20Strategy)).all():
        strat.metrics = _metrics_for(db, strat)
    db.commit()


def run_cycle(db: Session, settings: dict | None = None) -> dict:
    ev = evaluate_signals(db, settings)
    sm = settle_and_mark(db)
    snapshot(db)
    _persist_metrics(db)
    return {**ev, **sm}


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------
def _summary(db: Session, strat: Top20Strategy) -> dict:
    m = _metrics_for(db, strat)
    return {
        "id": strat.id, "key": strat.key, "name": strat.name,
        "description": strat.description, "philosophy": strat.philosophy,
        "exit_policy": strat.exit_policy, "active": strat.active,
        "status": strat.status, "version": strat.version, "param_hash": strat.param_hash,
        "parent_key": strat.parent_key, "notes": strat.notes,
        "starting_bankroll": strat.starting_bankroll,
        "fractional_kelly": strat.fractional_kelly, "params": strat.params,
        "signals_evaluated": strat.signals_evaluated, "trades_entered": strat.trades_entered,
        "metrics": m, **m,  # flatten metrics for easy table access
        "last_trade_at": _last_trade_at(db, strat.id),
        "paper_only": True,
    }


def _last_trade_at(db: Session, strategy_id: int):
    ts = db.scalar(select(func.max(Top20Trade.entry_time)).where(
        Top20Trade.strategy_id == strategy_id))
    return ts.isoformat() if ts else None


def list_strategies(db: Session) -> list[dict]:
    ensure_strategies(db)
    return [_summary(db, s) for s in db.scalars(
        select(Top20Strategy).order_by(Top20Strategy.id)).all()]


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
        "holding_minutes": t.holding_minutes, "exit_reason": t.exit_reason,
        "wallet_rank": (t.wallet_rank + 1) if t.wallet_rank is not None else None,
        "entry_confidence": t.entry_confidence, "entry_edge": t.entry_edge,
        "explanation": t.explanation,
    }


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


def strategy_detail(db: Session, strategy_id: int, recent: int = 25) -> dict | None:
    strat = db.get(Top20Strategy, strategy_id)
    if strat is None:
        return None
    out = _summary(db, strat)
    out["recent_trades"] = [_trade_dict(t) for t in db.scalars(
        select(Top20Trade).where(Top20Trade.strategy_id == strat.id)
        .order_by(Top20Trade.entry_time.desc()).limit(recent)).all()]
    out["top_wallets"] = _top_wallets(db, strat.id)
    out["equity_curve"] = [{"t": s.timestamp.isoformat(), "equity": s.equity}
                           for s in db.scalars(select(Top20Snapshot).where(
                               Top20Snapshot.strategy_id == strat.id)
                               .order_by(Top20Snapshot.timestamp)).all()]
    return out


def list_trades(db: Session, strategy_id: int | None = None, limit: int = 100) -> list[dict]:
    q = select(Top20Trade).order_by(Top20Trade.entry_time.desc()).limit(limit)
    if strategy_id is not None:
        q = q.where(Top20Trade.strategy_id == strategy_id)
    return [_trade_dict(t) for t in db.scalars(q).all()]


def leaderboard(db: Session) -> dict:
    strategies = db.scalars(select(Top20Strategy).order_by(Top20Strategy.id)).all()
    rows = [{"id": s.id, "key": s.key, "name": s.name, "metrics": _metrics_for(db, s)}
            for s in strategies]
    ranked = lb.rank(rows)
    pair = ""
    contenders = [r for r in ranked if r["has_trades"]]
    if len(contenders) >= 2:
        pair = lb.explain_pair(contenders[0], contenders[1])
    return {"paper_only": True, "weights": lb.WEIGHTS, "ranking": ranked, "head_to_head": pair}


def explain_signal(db: Session, signal_id: int) -> dict | None:
    s = db.get(PaperSignal, signal_id)
    if s is None:
        return None
    wm = _wallet_metrics(db)
    shared = _build_shared(db, [s], wm)
    m = db.get(Market, s.market_id)
    w = db.get(Wallet, s.wallet_id)
    if not (m and w):
        return None
    mt = wm.get(s.wallet_id, {})
    now = datetime.utcnow()
    ctx = Ctx(wallet_id=s.wallet_id, classification=mt.get("classification"),
              confidence=float(s.confidence or 0), edge=float(s.edge_estimate or 0),
              liquidity=float(m.liquidity or 0), age_min=(now - s.created_at).total_seconds() / 60,
              price=float(s.observed_price or 0.5), outcome=s.outcome, market_id=s.market_id,
              category=categorize(m.question, m.category), win_rate=mt.get("win_rate", 0),
              sharpe=mt.get("sharpe", 0), roi=mt.get("roi", 0), copyability=mt.get("copyability", 0),
              specialization=mt.get("specialization", 0), recency=mt.get("recency", 0),
              num_settled=mt.get("num_settled", 0))
    decisions = []
    for d in STRATEGIES:
        admit, why = decide(d, ctx, shared)
        decisions.append({"strategy": d.name, "key": d.key,
                          "decision": "TAKE" if admit else "SKIP",
                          "reason": "passes all filters" if admit else why})
    return {
        "signal_id": signal_id, "wallet": w.address, "market_question": m.question,
        "outcome": s.outcome, "price": s.observed_price, "edge": s.edge_estimate,
        "confidence": s.confidence, "category": ctx.category, "decisions": decisions,
        "taken_by": sum(1 for d in decisions if d["decision"] == "TAKE"),
    }


def portfolio(db: Session) -> dict:
    strategies = db.scalars(select(Top20Strategy)).all()
    total_start = sum(s.starting_bankroll for s in strategies)
    open_trades = db.scalars(select(Top20Trade).where(Top20Trade.status == "open")).all()
    closed_trades = db.scalars(select(Top20Trade).where(Top20Trade.status == "closed")).all()
    realized = round(sum(t.realized_pnl for t in closed_trades), 2)
    unreal = round(sum(t.unrealized_pnl for t in open_trades), 2)
    equity = round(total_start + realized + unreal, 2)
    open_exposure = round(sum(t.stake for t in open_trades), 2)

    def _bucket(attr):
        b: dict[str, float] = {}
        for t in open_trades:
            key = getattr(t, attr) or "—"
            b[key] = round(b.get(key, 0.0) + t.stake, 2)
        return dict(sorted(b.items(), key=lambda kv: kv[1], reverse=True)[:12])

    # category exposure via inference
    cat_exp: dict[str, float] = {}
    for t in open_trades:
        m = db.get(Market, t.market_id)
        cat = categorize(t.market_question, m.category if m else None)
        cat_exp[cat] = round(cat_exp.get(cat, 0.0) + t.stake, 2)

    # combined equity curve — forward-fill each strategy's last equity onto a
    # shared timeline, starting every strategy at its bankroll. This avoids the
    # jagged sum-by-exact-timestamp curve that produced false ~75% drawdowns.
    from itertools import groupby
    last_eq = {s.id: s.starting_bankroll for s in strategies}
    rows = db.execute(select(Top20Snapshot.timestamp, Top20Snapshot.strategy_id,
                             Top20Snapshot.equity).order_by(Top20Snapshot.timestamp)).all()
    curve = []
    for ts, group in groupby(rows, key=lambda r: r[0]):
        for _, sid, eq in group:
            last_eq[sid] = eq
        curve.append({"t": ts.isoformat(), "equity": round(sum(last_eq.values()), 2)})
    equities = [c["equity"] for c in curve]
    rets = [equities[i] / equities[i-1] - 1 for i in range(1, len(equities)) if equities[i-1]]
    rolling = rets[-30:]
    rolling_sharpe = round(analytics.sharpe(rolling), 4) if len(rolling) >= 2 else 0.0
    rolling_vol = round(statistics.pstdev(rolling), 5) if len(rolling) >= 2 else 0.0

    return {
        "paper_only": True,
        "starting_capital": round(total_start, 2),
        "equity": equity, "realized_pnl": realized, "unrealized_pnl": unreal,
        "total_pnl": round(realized + unreal, 2),
        "open_positions": len(open_trades), "closed_positions": len(closed_trades),
        "open_exposure": open_exposure,
        "capital_utilization": round(open_exposure / total_start, 4) if total_start else 0.0,
        "max_drawdown": analytics.max_drawdown(equities),
        "rolling_sharpe": rolling_sharpe, "rolling_volatility": rolling_vol,
        "exposure_by_category": cat_exp,
        "exposure_by_wallet": _bucket("wallet_address"),
        "exposure_by_market": _bucket("market_question"),
        "equity_curve": curve,
    }


def wallet_profile(db: Session, address: str) -> dict | None:
    w = db.scalar(select(Wallet).where(Wallet.address == address))
    if w is None:
        return None
    stat = db.get(WalletStat, w.id)
    cand = db.get(WalletCandidate, w.id)
    trades = db.scalars(select(Trade).where(Trade.wallet_id == w.id)).all()
    mids = {t.market_id for t in trades}
    markets = {m.id: m for m in db.scalars(select(Market).where(Market.id.in_(mids))).all()}
    settled = positions_mod.settled_positions(trades, markets)
    settled.sort(key=lambda p: p.timestamp)
    pnls = [p.realized_pnl for p in settled]
    rets = [p.realized_pnl / p.size for p in settled if p.size]
    # equity / drawdown from cumulative settled pnl
    cum = 0.0
    curve = []
    for p in settled:
        cum += p.realized_pnl
        curve.append({"t": p.timestamp.isoformat(), "pnl": round(cum, 2)})
    equities = [10_000 + c["pnl"] for c in curve] or [10_000]
    # category breakdown
    cat: dict[str, dict] = {}
    for p in settled:
        c = categorize(p.market.question if p.market else None,
                       p.market.category if p.market else None)
        a = cat.setdefault(c, {"category": c, "trades": 0, "pnl": 0.0})
        a["trades"] += 1
        a["pnl"] += p.realized_pnl
    cats = [{**v, "pnl": round(v["pnl"], 2)} for v in cat.values()]
    cats.sort(key=lambda x: x["pnl"], reverse=True)

    def _window(days):
        cutoff = datetime.utcnow() - timedelta(days=days)
        ps = [p for p in settled if p.timestamp >= cutoff]
        return {"settled": len(ps), "pnl": round(sum(x.realized_pnl for x in ps), 2)}

    return {
        "address": w.address, "label": w.label, "copy_enabled": w.copy_enabled,
        "copyability": cand.copyability_score if cand else None,
        "classification": cand.classification if cand else "insufficient_data",
        "roi": stat.realized_roi if stat else 0.0,
        "win_rate": stat.win_rate if stat else 0.0,
        "sharpe": _wallet_metrics(db).get(w.id, {}).get("sharpe", 0.0),
        "profit_factor": analytics.profit_factor(pnls),
        "avg_position_size": round(sum(p.size for p in settled) / len(settled), 2) if settled else 0.0,
        "num_settled": len(settled), "num_trades": stat.num_trades if stat else len(trades),
        "best_categories": [c for c in cats if c["pnl"] > 0][:3],
        "worst_categories": [c for c in cats if c["pnl"] < 0][-3:],
        "category_breakdown": cats,
        "max_drawdown": analytics.max_drawdown(equities),
        "sharpe_of_settled": analytics.sharpe(rets),
        "equity_curve": curve,
        "recent_7d": _window(7), "recent_30d": _window(30),
        "lifetime": {"settled": len(settled), "pnl": round(sum(pnls), 2)},
        "reputation": reputation.compute(settled),   # Phase 15 (decay-weighted)
        "paper_only": True,
    }


# ---------------------------------------------------------------------------
# Phase 12/13 — labeled dataset + optimization + walk-forward
# ---------------------------------------------------------------------------
def _build_samples(db: Session) -> list:
    """Signals whose market has RESOLVED, joined with wallet/market features —
    the labeled dataset (outcome known) used for optimization. No future info:
    the outcome only labels the sample; it never enters a decision."""
    wm = _wallet_metrics(db)
    sigs = db.scalars(select(PaperSignal)).all()
    mids = {s.market_id for s in sigs}
    markets = {m.id: m for m in db.scalars(select(Market).where(Market.id.in_(mids))).all()}
    out = []
    for s in sigs:
        m = markets.get(s.market_id)
        if not m or not m.resolved or m.resolved_outcome is None:
            continue
        mt = wm.get(s.wallet_id, {})
        out.append(simulate.Sample(
            created_at=s.created_at, market_id=s.market_id, outcome=s.outcome,
            resolved_outcome=m.resolved_outcome, price=float(s.observed_price or 0.5),
            confidence=float(s.confidence or 0), edge=float(s.edge_estimate or 0),
            liquidity=float(m.liquidity or 0), category=categorize(m.question, m.category),
            win_rate=mt.get("win_rate", 0), sharpe=mt.get("sharpe", 0), roi=mt.get("roi", 0),
            copyability=mt.get("copyability", 0), classification=mt.get("classification"),
            specialization=mt.get("specialization", 0), recency=mt.get("recency", 0),
            num_settled=mt.get("num_settled", 0), wallet_id=s.wallet_id))
    return out


def optimize_param(db: Session, param: str) -> dict:
    return optimize.optimize(_build_samples(db), param)


def walk_forward_param(db: Session, param: str, windows: int = 4) -> dict:
    return optimize.walk_forward(_build_samples(db), param, windows=windows)


# ---------------------------------------------------------------------------
# Phase 14 — Monte Carlo risk
# ---------------------------------------------------------------------------
def monte_carlo(db: Session, strategy_id: int, sims: int = 2000, seed: int = 42) -> dict:
    strat = db.get(Top20Strategy, strategy_id)
    if strat is None:
        return {"error": "strategy not found"}
    pnls = [t.realized_pnl for t in db.scalars(select(Top20Trade).where(
        Top20Trade.strategy_id == strategy_id, Top20Trade.status == "closed")).all()]
    res = montecarlo.simulate(pnls, strat.starting_bankroll, sims=sims, seed=seed)
    return {"strategy": strat.name, "key": strat.key, **res}


# ---------------------------------------------------------------------------
# Phase 16 — market intelligence
# ---------------------------------------------------------------------------
def market_intelligence(db: Session) -> dict:
    sigs = db.scalars(select(PaperSignal)).all()
    mids = {s.market_id for s in sigs}
    markets = {m.id: m for m in db.scalars(select(Market).where(Market.id.in_(mids))).all()}
    records = []
    for s in sigs:
        m = markets.get(s.market_id)
        if not m or not m.resolved or m.resolved_outcome is None:
            continue
        won = 1.0 if m.resolved_outcome == s.outcome else 0.0
        price = float(s.observed_price or 0.5)
        ttr = None
        if m.resolved_at and s.created_at:
            ttr = max(0.0, (m.resolved_at - s.created_at).total_seconds() / 3600.0)
        records.append({"category": categorize(m.question, m.category),
                        "edge": float(s.edge_estimate or 0), "won": won, "price": price,
                        "realized_return": round((won - price) / max(0.01, price), 4),
                        "ttr_hours": ttr})
    return market_intel.compute(records)


# ---------------------------------------------------------------------------
# Phase 17 — ensembles
# ---------------------------------------------------------------------------
def ensemble_view(db: Session) -> dict:
    rows = []
    for strat in db.scalars(select(Top20Strategy).order_by(Top20Strategy.id)).all():
        closed = db.scalars(select(Top20Trade).where(
            Top20Trade.strategy_id == strat.id, Top20Trade.status == "closed")
            .order_by(Top20Trade.closed_at)).all()
        rets = [t.realized_pnl / t.stake for t in closed if t.stake]
        m = _metrics_for(db, strat)
        rows.append({"key": strat.key, "name": strat.name, "returns": rets, "metrics": m})
    return ensembles.compute(rows)


# ---------------------------------------------------------------------------
# Phase 19 — daily research report (Markdown)
# ---------------------------------------------------------------------------
def research_report(db: Session, date: str = "") -> dict:
    lbd = leaderboard(db)
    ranked = [r for r in lbd["ranking"] if r["has_trades"]]
    best = ranked[0] if ranked else {}
    summaries = list_strategies(db)
    by_pnl = sorted(summaries, key=lambda s: s["total_pnl"], reverse=True)
    imp = ({"name": by_pnl[0]["name"], "delta": by_pnl[0]["total_pnl"]}
           if by_pnl and by_pnl[0]["total_pnl"] > 0 else None)
    reg = ({"name": by_pnl[-1]["name"], "delta": by_pnl[-1]["total_pnl"]}
           if by_pnl and by_pnl[-1]["total_pnl"] < 0 else None)
    dd = max(summaries, key=lambda s: s["max_drawdown"], default=None)
    closed = db.scalars(select(Top20Trade).where(Top20Trade.status == "closed")).all()
    mp = max(closed, key=lambda t: t.realized_pnl, default=None)
    ws = min(closed, key=lambda t: t.realized_pnl, default=None)
    exits_recent = db.scalars(select(Top20Trade).where(Top20Trade.status == "closed")
                              .order_by(Top20Trade.closed_at.desc()).limit(5)).all()
    cands = db.scalars(select(WalletCandidate).where(
        WalletCandidate.classification != "insufficient_data")
        .order_by(WalletCandidate.copyability_score.desc()).limit(5)).all()
    new_wallets = [{"address": db.get(Wallet, c.wallet_id).address,
                    "copyability": c.copyability_score, "classification": c.classification}
                   for c in cands if db.get(Wallet, c.wallet_id)]
    mi = market_intelligence(db)
    rec = recommend_retirements(db)
    port = portfolio(db)
    best_metrics = dict(best.get("metrics", {})) if best else {}
    best_metrics.update({"name": best.get("name", "—"), "score": best.get("score", 0),
                         "sharpe": best.get("metrics", {}).get("sharpe", 0)} if best else {})
    ctx = {
        "date": date, "best_strategy": best_metrics, "best_reason": best.get("reason", "") if best else "",
        "biggest_improvement": imp, "largest_regression": reg,
        "largest_drawdown": {"name": dd["name"], "dd": dd["max_drawdown"]} if dd else None,
        "new_top_wallets": new_wallets,
        "category_performance": mi.get("categories", [])[:4],
        "most_profitable_signal": {"market": mp.market_question, "pnl": mp.realized_pnl} if mp else None,
        "worst_signal": {"market": ws.market_question, "pnl": ws.realized_pnl} if ws else None,
        "recent_exits": [{"market": e.market_question, "reason": e.exit_reason or "resolved",
                          "pnl": e.realized_pnl} for e in exits_recent],
        "parameter_changes": [f"Retire recommended: {r['name']} ({r['reason']})"
                              for r in rec["recommendations"]] or ["No lifecycle changes."],
        "open_risk": {"open_exposure": port["open_exposure"],
                      "capital_utilization": port["capital_utilization"],
                      "open_positions": port["open_positions"]},
    }
    return {"paper_only": True, "markdown": report_mod.generate(ctx)}


def forward_test(db: Session) -> dict:
    """Phase 9: split each strategy's closed trades chronologically into
    train / validation / forward windows (60/20/20 by time) and report metrics
    per window. Decisions were made at entry time, so no future info leaks back.
    Read-only — does not modify state."""
    strategies = db.scalars(select(Top20Strategy).order_by(Top20Strategy.id)).all()
    out = []
    for strat in strategies:
        closed = db.scalars(select(Top20Trade).where(
            Top20Trade.strategy_id == strat.id, Top20Trade.status == "closed")
            .order_by(Top20Trade.entry_time)).all()
        n = len(closed)
        segs = {"train": closed[:int(n*0.6)], "validation": closed[int(n*0.6):int(n*0.8)],
                "forward": closed[int(n*0.8):]}
        seg_metrics = {}
        for name, ts in segs.items():
            pnls = [t.realized_pnl for t in ts]
            rets = [t.realized_pnl / t.stake for t in ts if t.stake]
            seg_metrics[name] = {
                "trades": len(ts), "pnl": round(sum(pnls), 2),
                "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 4) if pnls else 0.0,
                "sharpe": analytics.sharpe(rets), "expectancy": analytics.expectancy(pnls),
            }
        out.append({"id": strat.id, "key": strat.key, "name": strat.name,
                    "total_closed": n, "segments": seg_metrics})
    return {"paper_only": True, "split": "60% train / 20% validation / 20% forward (chronological)",
            "strategies": out}


from ..models import Top20FeatureVector as _FV  # noqa: E402


# ---------------------------------------------------------------------------
# Phase 29 — probability benchmark over the labeled dataset
# ---------------------------------------------------------------------------
def probability_benchmark(db: Session) -> dict:
    fvs = db.scalars(select(_FV).where(_FV.settled == True)).all()  # noqa: E712
    samples = []
    for fv in fvs:
        f = fv.features or {}
        if fv.label_realized_return is None:
            continue
        price = float(f.get("price", 0.5))
        edge = float(f.get("edge", 0.0))
        samples.append({
            "y": 1 if fv.label_realized_return > 0 else 0,
            "current": float(f.get("estimated_probability", price)),
            "market": price,
            "wallet": float(f.get("wallet_win_rate", price)),
            "edge": max(0.01, min(0.99, price + edge)),
        })
    return benchmark.compute(samples)


# ---------------------------------------------------------------------------
# Phase 27 — strategy drift (monthly degradation)
# ---------------------------------------------------------------------------
def strategy_drift(db: Session) -> dict:
    trades = db.scalars(select(Top20Trade).where(Top20Trade.status == "closed")).all()
    months: dict[str, list] = {}
    for t in trades:
        if not t.closed_at:
            continue
        months.setdefault(t.closed_at.strftime("%Y-%m"), []).append(t)
    series = []
    for mo in sorted(months):
        ts = months[mo]
        rets = [t.realized_pnl / t.stake for t in ts if t.stake]
        pnls = [t.realized_pnl for t in ts]
        series.append({
            "month": mo, "trades": len(ts),
            "sharpe": analytics.sharpe(rets),
            "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 4) if pnls else 0.0,
            "avg_edge": round(sum(t.entry_edge for t in ts) / len(ts), 4),
            "avg_kelly": round(sum(t.kelly_fraction for t in ts) / len(ts), 4),
        })
    # decay verdict: linear trend of monthly Sharpe
    decay = "insufficient history"
    if len(series) >= 3:
        sh = [m["sharpe"] for m in series]
        slope = (sh[-1] - sh[0]) / (len(sh) - 1)
        decay = ("degrading" if slope < -0.1 else "improving" if slope > 0.1 else "stable")
    return {"paper_only": True, "months": series, "decay": decay,
            "note": "Per-month Sharpe/win/edge/Kelly across all paper trades (live + replay)."}


# ---------------------------------------------------------------------------
# Phase 28 — market regimes + per-regime strategy performance
# ---------------------------------------------------------------------------
def _classify_regime(markets: list) -> str:
    if not markets:
        return "unknown"
    cats: dict[str, int] = {}
    liq = 0.0
    for m in markets:
        cats[categorize(m.question, m.category)] = cats.get(categorize(m.question, m.category), 0) + 1
        liq += float(m.liquidity or 0)
    dom = max(cats, key=cats.get)
    avg_liq = liq / len(markets)
    liq_tag = "high-liquidity" if avg_liq >= 50_000 else "low-liquidity"
    return f"{dom}-heavy / {liq_tag}"


def market_regimes(db: Session) -> dict:
    resolved = db.scalars(select(Market).where(Market.resolved == True)).all()  # noqa: E712
    by_month: dict[str, list] = {}
    for m in resolved:
        if m.resolved_at:
            by_month.setdefault(m.resolved_at.strftime("%Y-%m"), []).append(m)
    regimes = {mo: _classify_regime(ms) for mo, ms in by_month.items()}
    # per-regime strategy performance from closed trades
    trades = db.scalars(select(Top20Trade).where(Top20Trade.status == "closed")).all()
    regime_perf: dict[str, dict] = {}
    for t in trades:
        if not t.closed_at:
            continue
        reg = regimes.get(t.closed_at.strftime("%Y-%m"), "unknown")
        rp = regime_perf.setdefault(reg, {})
        s = db.get(Top20Strategy, t.strategy_id)
        key = s.name if s else str(t.strategy_id)
        rp.setdefault(key, {"pnl": 0.0, "trades": 0})
        rp[key]["pnl"] += t.realized_pnl
        rp[key]["trades"] += 1
    out = []
    for reg, strat_map in regime_perf.items():
        best = max(strat_map.items(), key=lambda kv: kv[1]["pnl"], default=None)
        out.append({"regime": reg, "trades": sum(v["trades"] for v in strat_map.values()),
                    "best_strategy": best[0] if best else None,
                    "best_pnl": round(best[1]["pnl"], 2) if best else 0.0})
    return {"paper_only": True, "monthly_regimes": regimes, "regime_performance": out}


# ---------------------------------------------------------------------------
# Phase 26 — wallet reputation evolution through time (no look-ahead)
# ---------------------------------------------------------------------------
def wallet_evolution(db: Session, address: str) -> dict | None:
    w = db.scalar(select(Wallet).where(Wallet.address == address))
    if w is None:
        return None
    tl = replay_mod._build_wallet_timelines(db).get(w.id)
    if not tl or not tl["times"]:
        return {"address": address, "points": [], "paper_only": True}
    points = []
    for i, t in enumerate(tl["times"]):
        n = i + 1
        roi = tl["pnl"][i] / (tl["cost"][i] or 1)
        win = tl["wins"][i] / n
        points.append({"t": t.isoformat(), "n_settled": n, "roi": round(roi, 4),
                       "win_rate": round(win, 4), "score": replay_mod._running_score(n, win, roi)})
    return {"paper_only": True, "address": address, "points": points}


# ---------------------------------------------------------------------------
# Phase 21-24 — replay control wrappers
# ---------------------------------------------------------------------------
def replay_status(db: Session) -> dict:
    return replay_mod.status(db)


def replay_backfill_markets(db: Session, pages: int = 3) -> dict:
    return replay_mod.backfill_closed_markets(db, pages=pages)


def replay_backfill_wallets(db: Session, max_wallets: int = 5) -> dict:
    return replay_mod.backfill_wallets(db, max_wallets=max_wallets)


def replay_run(db: Session, max_trades: int = 400) -> dict:
    return replay_mod.run(db, max_trades=max_trades)


def replay_reset(db: Session) -> dict:
    return replay_mod.reset(db)


def reset_paper(db: Session) -> dict:
    n_trades = db.query(Top20Trade).delete()
    n_snaps = db.query(Top20Snapshot).delete()
    n_fv = db.query(Top20FeatureVector).delete()
    for strat in db.scalars(select(Top20Strategy)).all():
        strat.signals_evaluated = 0
        strat.trades_entered = 0
        strat.last_signal_id = 0
        strat.metrics = {}
    db.commit()
    return {"trades_deleted": int(n_trades or 0), "snapshots_deleted": int(n_snaps or 0),
            "feature_vectors_deleted": int(n_fv or 0)}


# ---------------------------------------------------------------------------
# Phase 18 — strategy lifecycle / retirement (status only; never delete history)
# ---------------------------------------------------------------------------
MIN_SAMPLE_FOR_LIFECYCLE = 20   # statistically meaningful sample before judging


def recommend_retirements(db: Session) -> dict:
    """Recommend retiring strategies that consistently underperform after a
    meaningful sample. Returns recommendations + a suggested promotion path.
    Does not mutate anything — recommendations only."""
    rows = [(s, _metrics_for(db, s)) for s in db.scalars(select(Top20Strategy)).all()]
    scored = [(s, m) for s, m in rows if (m.get("closed_positions") or 0) >= MIN_SAMPLE_FOR_LIFECYCLE]
    recs = []
    if scored:
        median_exp = statistics.median([m["expectancy"] for _, m in scored])
        for s, m in scored:
            neg = m["expectancy"] < 0 and m["sharpe"] < 0 and m["profit_factor"] < 1.0
            if neg and s.status != "retired":
                recs.append({"key": s.key, "name": s.name, "action": "retire",
                             "reason": f"expectancy {m['expectancy']:+.2f}, Sharpe {m['sharpe']:.2f}, "
                                       f"PF {m['profit_factor']:.2f} over {m['closed_positions']} closed",
                             "median_expectancy": round(median_exp, 2)})
    return {"paper_only": True, "min_sample": MIN_SAMPLE_FOR_LIFECYCLE,
            "evaluated": len(scored), "recommendations": recs}


def set_status(db: Session, key: str, status: str) -> dict | None:
    if status not in ("experimental", "candidate", "production", "retired"):
        return None
    s = db.scalar(select(Top20Strategy).where(Top20Strategy.key == key))
    if s is None:
        return None
    s.status = status
    if status == "retired":
        s.active = False
    db.commit()
    return {"key": key, "status": status, "active": s.active}


# ---------------------------------------------------------------------------
# Phase 20 — feature-vector dataset read access
# ---------------------------------------------------------------------------
def feature_vectors(db: Session, limit: int = 200, settled_only: bool = False) -> dict:
    q = select(Top20FeatureVector).order_by(Top20FeatureVector.created_at.desc()).limit(limit)
    if settled_only:
        q = q.where(Top20FeatureVector.settled == True)  # noqa: E712
    rows = db.scalars(q).all()
    total = db.scalar(select(func.count()).select_from(Top20FeatureVector))
    labeled = db.scalar(select(func.count()).select_from(Top20FeatureVector).where(
        Top20FeatureVector.settled == True))  # noqa: E712
    return {
        "paper_only": True, "total": int(total or 0), "labeled": int(labeled or 0),
        "note": "Supervised dataset for a future probability model — no ML trained yet.",
        "rows": [{
            "id": r.id, "created_at": r.created_at.isoformat() if r.created_at else None,
            "strategy_key": r.strategy_key, "signal_id": r.signal_id, "decision": r.decision,
            "features": r.features, "settled": r.settled, "label_outcome": r.label_outcome,
            "label_realized_return": r.label_realized_return, "label_realized_pnl": r.label_realized_pnl,
            "label_exit_reason": r.label_exit_reason,
        } for r in rows],
    }
