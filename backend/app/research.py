"""Research Platform V1 — a self-improving paper-research layer over the BTC 5M
Reversal Lab.

100% READ-ONLY / PAPER ONLY. It reads the btc5m_* research tables and writes only
to its own research_* tables. It NEVER places real orders, changes production
rankings/eligibility/discovery, or touches live trading/bankroll/execution.

Phases:
  1 Strategy Library        -> seed_strategies(), strategy objects
  2 Independent paper trade -> paper_trade_strategy() / replay_all()
  3 Historical replay       -> _decision_contexts() (chronological, no look-ahead)
  4 Strategy mutation       -> mutate_top() (lineage; never overwrites)
  5 Strategy tournament     -> tournament() (robust scoring)
  6 Ensemble research       -> build_ensembles()
  7 Nightly review          -> nightly_review() (18 sections, stored permanently)
  8 Hypothesis engine       -> generate_hypotheses()
  9 Explainability          -> every decision carries an explanation
 10 Continuous learning     -> research_cycle() (idempotent replay; reproducible)
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import btc5m
from . import btc5m_models as bm
from . import research_models as rm

# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
DECISION_OFFSET_S = 60          # decide 60s into the 5-min market (only prior info)
START_BANKROLL = 100.0          # each strategy's independent paper bankroll
STAKE_FRACTIONS = {"fixed": 0.05, "confidence": 0.10, "volatility": 0.05}
MIN_TRADES_FOR_CHAMPION = 8


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs):
    return statistics.pstdev(xs) if len(xs) > 1 else 0.0


def _clip(v, lo, hi):
    return max(lo, min(hi, v))


# ===========================================================================
# Phase 3 — historical replay: build a causal decision context per market
# ===========================================================================
def _decision_contexts(db: Session, *, resolved_only: bool = True) -> list[dict]:
    """One causal decision context per BTC5m market, chronological. Uses ONLY the
    trades available DECISION_OFFSET_S into the market (no look-ahead). Computed
    once and shared by every strategy so paper trading is fast and identical-input."""
    markets = db.scalars(select(bm.Btc5mMarket).order_by(bm.Btc5mMarket.created_time.asc())).all()
    profitable = {p.wallet_address for p in db.scalars(
        select(bm.Btc5mWalletProfile).where(bm.Btc5mWalletProfile.profitable.is_(True))).all()}
    cluster_of = {p.wallet_address: p.cluster for p in db.scalars(select(bm.Btc5mWalletProfile)).all()}

    contexts = []
    for m in markets:
        if resolved_only and not (m.resolved and m.final_outcome is not None):
            continue
        trs = db.scalars(select(bm.Btc5mTrade).where(bm.Btc5mTrade.market_id == m.market_id)
                         .order_by(bm.Btc5mTrade.timestamp.asc())).all()
        if not trs:
            continue
        # decision time = creation + offset (fallback: first third of the trades)
        if m.created_time:
            dec_time = m.created_time + timedelta(seconds=DECISION_OFFSET_S)
            prior = [t for t in trs if t.timestamp <= dec_time]
        else:
            cut = max(1, len(trs) // 3)
            prior = trs[:cut]
            dec_time = prior[-1].timestamp
        if not prior:
            prior = trs[:1]
            dec_time = prior[-1].timestamp
        pseries = [{"yes_price": t.price if t.direction == "YES" else 1.0 - t.price,
                    "usd": t.usd_value, "side": t.side, "dir": t.direction} for t in prior]
        feats = btc5m.reconstruct_features(pseries, None, "YES", DECISION_OFFSET_S,
                                           btc5m.MARKET_LIFE_SECONDS - DECISION_OFFSET_S, m)
        # wallet consensus among PROFITABLE wallets up to the decision point
        prof_dirs = [t.direction for t in prior if t.wallet_address in profitable]
        yes = prof_dirs.count("YES")
        no = prof_dirs.count("NO")
        # per-cluster net direction up to decision point
        cl_net: dict[str, int] = {}
        for t in prior:
            cl = cluster_of.get(t.wallet_address)
            if cl:
                cl_net[cl] = cl_net.get(cl, 0) + (1 if t.direction == "YES" else -1)
        contexts.append({
            "market_id": m.market_id, "question": m.question, "decision_at": dec_time,
            "features": feats, "outcome": btc5m._yes_no(m.final_outcome),
            "consensus": {"yes": yes, "no": no, "n": len(prof_dirs),
                          "wallets": [t.wallet_address for t in prior if t.wallet_address in profitable]},
            "cluster_net": cl_net,
        })
    return contexts


# ===========================================================================
# Phase 1 + 9 — strategy decision engine (every decision explains itself)
# ===========================================================================
def _decide(archetype: str, params: dict, ctx: dict, *, champion_model=None) -> dict:
    """Return a fully-explained decision for one strategy on one market context:
    {action, direction, confidence, edge, reasons[], contributions{}}. Pure +
    deterministic — the heart of both replay and explainability (Phase 9)."""
    f = ctx["features"]
    price = f.get("market_yes_price", 0.5)
    reasons: list[str] = []
    contrib: dict[str, float] = {}
    p_yes = 0.5

    if archetype == "momentum":
        slope = f.get("trend_slope", 0.0)
        rsi = f.get("rsi", 0.5)
        ema = f.get("ema_gap", 0.0)
        s = (slope * params.get("trend_w", 8.0) + (rsi - 0.5) * params.get("rsi_w", 1.2)
             + ema * params.get("ema_w", 4.0))
        p_yes = _clip(0.5 + s, 0.02, 0.98)
        if abs(slope) > params.get("trend_min", 0.002):
            reasons.append(f"trend slope {slope:+.3f} (momentum)")
        if abs(rsi - 0.5) > params.get("rsi_min", 0.05):
            reasons.append(f"RSI {rsi:.2f}")
        contrib = {"trend_slope": slope * params.get("trend_w", 8.0),
                   "rsi": (rsi - 0.5) * params.get("rsi_w", 1.2), "ema_gap": ema * params.get("ema_w", 4.0)}

    elif archetype == "mean_reversion":
        boll = f.get("boll_pos", 0.0)
        rsi = f.get("rsi", 0.5)
        thr = params.get("boll_thr", 0.4)
        s = -boll * params.get("boll_w", 0.6) - (rsi - 0.5) * params.get("rsi_w", 0.8)
        p_yes = _clip(0.5 + s, 0.02, 0.98)
        if abs(boll) > thr:
            reasons.append(f"Bollinger position {boll:+.2f} → revert")
        contrib = {"boll_pos": -boll * params.get("boll_w", 0.6),
                   "rsi": -(rsi - 0.5) * params.get("rsi_w", 0.8)}

    elif archetype == "breakout":
        boll = f.get("boll_pos", 0.0)
        atr = f.get("atr", 0.0)
        if atr >= params.get("atr_min", 0.01) and abs(boll) >= params.get("boll_thr", 0.5):
            p_yes = _clip(0.5 + boll * params.get("boll_w", 0.5), 0.02, 0.98)
            reasons.append(f"ATR expansion {atr:.3f} + band break {boll:+.2f}")
        contrib = {"boll_pos": boll * params.get("boll_w", 0.5), "atr": atr}

    elif archetype in ("consensus", "wallet_leaders"):
        c = ctx["consensus"]
        n = c["n"]
        thr = params.get("min_wallets", 2)
        if n >= thr:
            frac = c["yes"] / n if n else 0.5
            p_yes = _clip(0.5 + (frac - 0.5) * params.get("conf_w", 1.6), 0.02, 0.98)
            reasons.append(f"{n} profitable wallets, {c['yes']}↑/{c['no']}↓")
        contrib = {"wallet_consensus": (p_yes - 0.5)}

    elif archetype == "cluster_follow":
        cl = params.get("cluster", "Momentum")
        net = ctx["cluster_net"].get(cl, 0)
        if abs(net) >= params.get("min_net", 1):
            p_yes = _clip(0.5 + net * params.get("net_w", 0.08), 0.02, 0.98)
            reasons.append(f"cluster {cl} net flow {net:+d}")
        contrib = {f"cluster_{cl}": net * params.get("net_w", 0.08)}

    elif archetype == "model_champion" and champion_model is not None:
        vec = btc5m.feature_vector(f)
        p_yes = float(champion_model.predict_proba([vec])[0])
        # top contributing features from the champion's importance, if available
        reasons.append(f"champion model P(yes)={p_yes:.2f}")
        contrib = {"model_pyes": p_yes - 0.5}

    elif archetype == "ensemble":
        votes = params.get("_member_votes", [])   # injected at replay time
        if votes:
            p_yes = _clip(_mean(votes), 0.02, 0.98)
            reasons.append(f"ensemble of {len(votes)} members")
        contrib = {"ensemble": p_yes - 0.5}

    # convert p_yes -> action with a confidence threshold
    conf = abs(p_yes - 0.5) * 2
    min_conf = params.get("min_confidence", 0.15)
    if conf < min_conf:
        return {"action": "NO_TRADE", "direction": None, "confidence": round(conf, 4),
                "edge": 0.0, "p_yes": round(p_yes, 4), "reasons": reasons or ["below confidence threshold"],
                "contributions": contrib}
    if p_yes >= 0.5:
        action, direction, edge = "BUY_YES", "YES", round(p_yes - price, 4)
    else:
        action, direction, edge = "BUY_NO", "NO", round((1 - p_yes) - (1 - price), 4)
    return {"action": action, "direction": direction, "confidence": round(conf, 4),
            "edge": edge, "p_yes": round(p_yes, 4),
            "reasons": reasons or ["signal"], "contributions": {k: round(v, 4) for k, v in contrib.items()}}


def _stake(params: dict, bankroll: float, confidence: float, atr: float) -> float:
    model = params.get("sizing", "fixed")
    frac = STAKE_FRACTIONS.get(model, 0.05)
    if model == "confidence":
        frac = frac * _clip(confidence * 1.5, 0.3, 1.5)
    elif model == "volatility":
        frac = frac / (1 + atr * 10)
    return round(max(1.0, bankroll * frac), 2)


# ===========================================================================
# Phase 2 — independent paper trading + metrics
# ===========================================================================
def _max_drawdown(equity: list[float]) -> float:
    peak = equity[0] if equity else 0.0
    mdd = 0.0
    for e in equity:
        peak = max(peak, e)
        if peak > 0:
            mdd = max(mdd, (peak - e) / peak)
    return round(mdd, 4)


def _compute_metrics(trades: list[dict], equity: list[float], market_dates: list[datetime]) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0, "roi": 0.0, "profit_factor": 0.0, "win_rate": 0.0,
                "expected_value": 0.0, "max_drawdown": 0.0, "sharpe": 0.0, "calmar": 0.0,
                "consistency": 0.0, "avg_confidence": 0.0, "avg_edge": 0.0, "avg_hold_s": 0.0,
                "avg_entry_s": DECISION_OFFSET_S, "trade_frequency": 0.0,
                "rolling_7d": 0.0, "rolling_30d": 0.0, "rolling_90d": 0.0, "final_bankroll": START_BANKROLL}
    pnls = [t["realized_pnl"] for t in trades]
    stakes = [t["size"] for t in trades]
    rets = [p / s if s else 0.0 for p, s in zip(pnls, stakes)]
    wins = sum(1 for t in trades if t["won"])
    invested = sum(stakes) or 1.0
    realized = sum(pnls)
    gw = sum(p for p in pnls if p > 0)
    gl = -sum(p for p in pnls if p < 0)
    mdd = _max_drawdown(equity)
    roi = realized / invested
    sharpe = (_mean(rets) / _std(rets)) if _std(rets) > 1e-9 else 0.0
    calmar = (roi / mdd) if mdd > 1e-6 else (roi * 5 if roi > 0 else 0.0)
    # consistency: fraction of 5-trade blocks that are net positive
    blocks = [pnls[i:i + 5] for i in range(0, n, 5)]
    consistency = sum(1 for b in blocks if sum(b) > 0) / len(blocks) if blocks else 0.0

    def _rolling(days):
        if not market_dates:
            return 0.0
        cut = max(market_dates) - timedelta(days=days)
        return round(sum(p for p, d in zip(pnls, market_dates) if d and d >= cut), 2)

    return {
        "trades": n, "wins": wins, "roi": round(roi, 4),
        "profit_factor": round(gw / gl, 3) if gl > 0 else round(gw, 3),
        "win_rate": round(wins / n, 4), "expected_value": round(realized / n, 4),
        "max_drawdown": mdd, "sharpe": round(sharpe, 4), "calmar": round(calmar, 4),
        "consistency": round(consistency, 4),
        "avg_confidence": round(_mean([t["confidence"] for t in trades]), 4),
        "avg_edge": round(_mean([t["edge"] for t in trades]), 4),
        "avg_hold_s": btc5m.MARKET_LIFE_SECONDS - DECISION_OFFSET_S,
        "avg_entry_s": DECISION_OFFSET_S, "trade_frequency": round(n / max(1, len(equity)), 3),
        "realized_pnl": round(realized, 2), "final_bankroll": round(equity[-1], 2) if equity else START_BANKROLL,
        "rolling_7d": _rolling(7), "rolling_30d": _rolling(30), "rolling_90d": _rolling(90),
    }


def _robust_score(m: dict) -> float:
    """Composite robustness score (0..100). Favors repeatable, drawdown-aware
    performance over raw ROI (Phase 5)."""
    roi_n = _clip((m["roi"] + 0.2) / 0.6, 0, 1)
    pf_n = _clip((m["profit_factor"] - 0.8) / 1.2, 0, 1)
    wr_n = _clip((m["win_rate"] - 0.4) / 0.3, 0, 1)
    calmar_n = _clip(m["calmar"] / 3.0, 0, 1)
    sharpe_n = _clip((m["sharpe"] + 0.2) / 0.8, 0, 1)
    cons = _clip(m["consistency"], 0, 1)
    trade_pen = _clip(m["trades"] / 20.0, 0.3, 1.0)
    score = (0.22 * calmar_n + 0.20 * sharpe_n + 0.18 * pf_n + 0.15 * wr_n
             + 0.12 * roi_n + 0.13 * cons) * trade_pen
    return round(score * 100, 2)


def paper_trade_strategy(db: Session, strat: rm.ResearchStrategy, contexts: list[dict], *,
                         champion_model=None, vote_cache: dict | None = None) -> dict:
    """Replay one strategy independently over the chronological contexts. Recomputes
    deterministically (clears its prior paper trades first). Stores trades, equity
    curve, metrics. Returns the per-context p_yes votes (for ensembles)."""
    db.query(rm.StrategyPaperTrade).filter(rm.StrategyPaperTrade.strategy_id == strat.id).delete(
        synchronize_session=False)
    bankroll = START_BANKROLL
    equity = []
    market_dates = []
    trades_meta: list[dict] = []
    votes: dict[str, float] = {}
    rows = []
    for ctx in contexts:
        params = dict(strat.params or {})
        if strat.is_ensemble and vote_cache is not None:
            members = params.get("member_ids", [])
            mvotes = [vote_cache.get(mid, {}).get(ctx["market_id"]) for mid in members]
            mvotes = [v for v in mvotes if v is not None]
            params["_member_votes"] = mvotes
        dec = _decide(strat.archetype, params, ctx, champion_model=champion_model)
        votes[ctx["market_id"]] = dec.get("p_yes", 0.5)
        if dec["action"] == "NO_TRADE":
            continue
        atr = ctx["features"].get("atr", 0.0)
        stake = _stake(params, bankroll, dec["confidence"], atr)
        price = ctx["features"].get("market_yes_price", 0.5)
        entry = price if dec["direction"] == "YES" else (1.0 - price)
        entry = _clip(entry, 0.02, 0.98)
        shares = stake / entry
        won = (dec["direction"] == ctx["outcome"])
        pnl = round(shares * (1.0 if won else 0.0) - stake, 4)
        bankroll = round(bankroll + pnl, 4)
        equity.append(bankroll)
        market_dates.append(ctx["decision_at"])
        trades_meta.append({"realized_pnl": pnl, "size": stake, "won": won,
                            "confidence": dec["confidence"], "edge": dec["edge"]})
        rows.append(rm.StrategyPaperTrade(
            strategy_id=strat.id, market_id=ctx["market_id"], market_question=ctx["question"],
            decision_at=ctx["decision_at"], action=dec["action"], direction=dec["direction"],
            entry_price=round(entry, 4), confidence=dec["confidence"], edge=dec["edge"],
            size=stake, shares=round(shares, 4), won=won, realized_pnl=pnl,
            bankroll_after=bankroll, explanation={"reasons": dec["reasons"],
                                                  "contributions": dec["contributions"],
                                                  "p_yes": dec.get("p_yes")}))
    db.add_all(rows)
    metrics = _compute_metrics(trades_meta, equity, market_dates)
    strat.metrics = metrics
    strat.paper_bankroll = metrics["final_bankroll"]
    strat.trades = metrics["trades"]
    strat.robust_score = _robust_score(metrics)
    strat.equity_curve = [{"t": d.isoformat() if d else None, "equity": e}
                          for d, e in zip(market_dates, equity)][-200:]
    if strat.status == "Research" and metrics["trades"] > 0:
        strat.status = "Paper Trading"
    db.add(strat)
    return votes


def replay_all(db: Session) -> dict:
    """Replay EVERY strategy independently over the shared causal contexts (Phase 2
    + 3). Two passes so ensembles can read their members' votes."""
    contexts = _decision_contexts(db)
    champ_row = btc5m.champion(db, "global")
    champ_model = None
    if champ_row is not None:
        all_tr = db.scalars(select(bm.Btc5mTrade)).all()
        X, y = btc5m._dataset_xy(all_tr)
        if len(X) >= 6 and len(set(y)) >= 2:
            champ_model = btc5m.ml.MODEL_FACTORIES.get(champ_row.name, btc5m.ml.MajorityBaseline)()
            champ_model.fit(X, y)

    strategies = db.scalars(select(rm.ResearchStrategy).where(
        rm.ResearchStrategy.status != "Archived")).all()
    base = [s for s in strategies if not s.is_ensemble]
    ens = [s for s in strategies if s.is_ensemble]
    vote_cache: dict[int, dict] = {}
    for s in base:
        vote_cache[s.id] = paper_trade_strategy(db, s, contexts, champion_model=champ_model)
    for s in ens:
        paper_trade_strategy(db, s, contexts, champion_model=champ_model, vote_cache=vote_cache)
    db.commit()
    return {"strategies_replayed": len(strategies), "contexts": len(contexts),
            "base": len(base), "ensembles": len(ens)}


# ===========================================================================
# Phase 1 — strategy library seeding / discovery
# ===========================================================================
def _add_strategy(db: Session, *, name, archetype, description, params, origin_wallets=None,
                  origin_cluster=None, parent_id=None, version=1, is_ensemble=False,
                  status="Research") -> rm.ResearchStrategy:
    s = rm.ResearchStrategy(
        name=name, archetype=archetype, description=description, params=params,
        origin_wallets=origin_wallets or [], origin_cluster=origin_cluster, parent_id=parent_id,
        version=version, is_ensemble=is_ensemble, status=status, paper_bankroll=START_BANKROLL)
    db.add(s); db.flush()
    db.add(rm.ResearchExperiment(kind=("seed" if parent_id is None else "mutation"),
                                 strategy_id=s.id, parent_id=parent_id,
                                 title=f"created {name}", detail={"archetype": archetype, "params": params}))
    return s


def seed_strategies(db: Session) -> dict:
    """Discover an initial Strategy Library from the Lab if none exists yet
    (Phase 1). Idempotent — only seeds when the library is empty."""
    if db.scalar(select(func.count()).select_from(rm.ResearchStrategy)):
        return {"seeded": 0, "note": "library already populated"}
    cons = btc5m.consensus(db)
    cls = btc5m.clusters(db)
    top_clusters = [c["cluster"] for c in cls.get("clusters", [])[:3]] or ["Momentum"]
    leaders = [l["wallet"] for l in cons.get("leaders", [])[:5]]
    groups = cons.get("consensus_groups", [])
    seeded = []
    seeded.append(_add_strategy(db, name="Momentum Baseline", archetype="momentum",
                  description="Trade in the direction of short-horizon momentum (trend slope + RSI + EMA gap).",
                  params={"trend_w": 8.0, "rsi_w": 1.2, "ema_w": 4.0, "trend_min": 0.002, "min_confidence": 0.15, "sizing": "fixed"}))
    seeded.append(_add_strategy(db, name="Mean Reversion Baseline", archetype="mean_reversion",
                  description="Fade stretched Bollinger position / RSI back toward the mean.",
                  params={"boll_w": 0.6, "rsi_w": 0.8, "boll_thr": 0.4, "min_confidence": 0.15, "sizing": "fixed"}))
    seeded.append(_add_strategy(db, name="Breakout Baseline", archetype="breakout",
                  description="Trade band breakouts confirmed by ATR expansion.",
                  params={"boll_w": 0.5, "boll_thr": 0.5, "atr_min": 0.01, "min_confidence": 0.2, "sizing": "fixed"}))
    seeded.append(_add_strategy(db, name="Consensus-2", archetype="consensus",
                  description="Follow the majority direction of >=2 profitable wallets.",
                  params={"min_wallets": 2, "conf_w": 1.6, "min_confidence": 0.15, "sizing": "confidence"},
                  origin_wallets=leaders))
    seeded.append(_add_strategy(db, name="Consensus-3", archetype="consensus",
                  description="Stronger consensus: majority of >=3 profitable wallets.",
                  params={"min_wallets": 3, "conf_w": 1.8, "min_confidence": 0.2, "sizing": "confidence"},
                  origin_wallets=leaders))
    for cl in top_clusters:
        seeded.append(_add_strategy(db, name=f"Cluster Follow — {cl}", archetype="cluster_follow",
                      description=f"Follow the net order-flow direction of the {cl} cluster.",
                      params={"cluster": cl, "net_w": 0.08, "min_net": 1, "min_confidence": 0.15, "sizing": "fixed"},
                      origin_cluster=cl))
    seeded.append(_add_strategy(db, name="Champion Model", archetype="model_champion",
                  description="Use the Lab's current champion ML model's P(yes).",
                  params={"min_confidence": 0.2, "sizing": "confidence"}))
    if leaders:
        seeded.append(_add_strategy(db, name="Wallet Leaders", archetype="wallet_leaders",
                      description="Follow the leader wallets' early consensus.",
                      params={"min_wallets": 1, "conf_w": 1.6, "min_confidence": 0.15, "sizing": "fixed"},
                      origin_wallets=leaders))
    db.commit()
    return {"seeded": len(seeded), "groups_available": len(groups)}


# ===========================================================================
# Phase 4 — strategy mutation (lineage; never overwrites)
# ===========================================================================
_MUTABLE = {
    "momentum": [("trend_w", 2.0), ("rsi_w", 0.4), ("ema_w", 1.5), ("trend_min", 0.001), ("min_confidence", 0.05)],
    "mean_reversion": [("boll_w", 0.15), ("rsi_w", 0.2), ("boll_thr", 0.1), ("min_confidence", 0.05)],
    "breakout": [("boll_w", 0.15), ("boll_thr", 0.1), ("atr_min", 0.005), ("min_confidence", 0.05)],
    "consensus": [("min_wallets", 1), ("conf_w", 0.3), ("min_confidence", 0.05)],
    "wallet_leaders": [("conf_w", 0.3), ("min_confidence", 0.05)],
    "cluster_follow": [("net_w", 0.03), ("min_net", 1), ("min_confidence", 0.05)],
    "model_champion": [("min_confidence", 0.05)],
}


def mutate_top(db: Session, *, top_k: int = 4, per_parent: int = 2) -> dict:
    """Create child strategies by perturbing ONE parameter of the strongest
    strategies (Phase 4). Children are new rows with lineage — nothing is
    overwritten. Sizing-model swaps are also explored."""
    parents = db.scalars(select(rm.ResearchStrategy)
                         .where(rm.ResearchStrategy.is_ensemble.is_(False),
                                rm.ResearchStrategy.status.in_(("Paper Trading", "Candidate", "Champion")))
                         .order_by(rm.ResearchStrategy.robust_score.desc()).limit(top_k)).all()
    created = []
    for p in parents:
        knobs = _MUTABLE.get(p.archetype, [])
        for i in range(per_parent):
            params = dict(p.params or {})
            if knobs:
                key, step = knobs[(p.version + i) % len(knobs)]
                cur = params.get(key, step)
                direction = 1 if (p.id + i) % 2 == 0 else -1
                newv = round(cur + direction * step, 4)
                if key in ("min_wallets", "min_net"):
                    newv = max(1, int(newv))
                params[key] = newv
                mut_desc = f"{key} {cur}→{newv}"
            else:
                params["sizing"] = "confidence" if params.get("sizing") != "confidence" else "fixed"
                mut_desc = f"sizing→{params['sizing']}"
            child = _add_strategy(db, name=f"{p.name} v{p.version + 1}.{i + 1}", archetype=p.archetype,
                                  description=f"Mutation of #{p.id} ({mut_desc}).", params=params,
                                  origin_wallets=p.origin_wallets, origin_cluster=p.origin_cluster,
                                  parent_id=p.id, version=p.version + 1)
            created.append({"id": child.id, "parent": p.id, "mutation": mut_desc})
    db.commit()
    return {"mutations_created": len(created), "children": created}


# ===========================================================================
# Phase 6 — ensemble research
# ===========================================================================
def build_ensembles(db: Session) -> dict:
    """Build/refresh ensemble strategies that combine member votes (Phase 6).
    Idempotent: refreshes member lists rather than duplicating ensembles."""
    by_arch: dict[str, list] = {}
    for s in db.scalars(select(rm.ResearchStrategy).where(rm.ResearchStrategy.is_ensemble.is_(False))).all():
        by_arch.setdefault(s.archetype, []).append(s)
    top = db.scalars(select(rm.ResearchStrategy).where(rm.ResearchStrategy.is_ensemble.is_(False))
                     .order_by(rm.ResearchStrategy.robust_score.desc()).limit(3)).all()

    def ids(*archs):
        out = []
        for a in archs:
            out += [s.id for s in by_arch.get(a, [])]
        return out

    specs = [
        ("Consensus Ensemble", ids("consensus", "wallet_leaders"), "Vote of consensus & wallet-leader strategies."),
        ("Momentum Cluster Ensemble", ids("momentum", "cluster_follow"), "Momentum + cluster-flow strategies."),
        ("Mean Reversion Cluster Ensemble", ids("mean_reversion", "breakout"), "Reversion + breakout strategies."),
        ("Wallet Leaders Ensemble", ids("wallet_leaders", "consensus"), "Leader-following strategies."),
        ("Hybrid Ensemble", ids("momentum", "mean_reversion", "consensus"), "Cross-style hybrid."),
        ("Meta Ensemble", [s.id for s in top], "Vote of the current top-3 strategies by robust score."),
    ]
    n = 0
    for name, member_ids, desc in specs:
        member_ids = [m for m in member_ids if m]
        if not member_ids:
            continue
        existing = db.scalar(select(rm.ResearchStrategy).where(rm.ResearchStrategy.name == name))
        params = {"member_ids": member_ids, "min_confidence": 0.12, "sizing": "fixed"}
        if existing:
            existing.params = params
            db.add(existing)
        else:
            _add_strategy(db, name=name, archetype="ensemble", description=desc, params=params,
                          is_ensemble=True, status="Paper Trading")
            n += 1
    db.commit()
    return {"ensembles_built": n, "ensembles_total": len(specs)}


# ===========================================================================
# Phase 5 — strategy tournament + champion selection
# ===========================================================================
def tournament(db: Session) -> dict:
    """Rank all strategies by robust score (not raw ROI) and promote a champion.
    Demotes the prior champion; logs champion changes (Phase 5)."""
    strategies = db.scalars(select(rm.ResearchStrategy).where(
        rm.ResearchStrategy.status != "Archived").order_by(rm.ResearchStrategy.robust_score.desc())).all()
    eligible = [s for s in strategies if s.trades >= MIN_TRADES_FOR_CHAMPION]
    prev_champ = db.scalar(select(rm.ResearchStrategy).where(rm.ResearchStrategy.is_champion.is_(True)))
    new_champ = eligible[0] if eligible else None

    changed = False
    if new_champ and (prev_champ is None or new_champ.id != prev_champ.id):
        changed = True
        if prev_champ:
            prev_champ.is_champion = False
            prev_champ.status = "Candidate"
            db.add(prev_champ)
        new_champ.is_champion = True
        new_champ.status = "Champion"
        db.add(new_champ)
        db.add(rm.ResearchExperiment(kind="champion", strategy_id=new_champ.id,
               parent_id=prev_champ.id if prev_champ else None,
               title=f"new champion: {new_champ.name}",
               detail={"robust_score": new_champ.robust_score,
                       "prev": (prev_champ.name if prev_champ else None),
                       "prev_score": (prev_champ.robust_score if prev_champ else None)}))
    elif new_champ:
        new_champ.is_champion = True
        new_champ.status = "Champion"
        db.add(new_champ)

    # mark strong non-champion strategies as Candidate; weak ones Retired
    for s in strategies:
        if s.is_champion:
            continue
        if s.trades >= MIN_TRADES_FOR_CHAMPION and s.robust_score >= 35:
            if s.status not in ("Candidate", "Champion"):
                s.status = "Candidate"
        elif s.trades >= MIN_TRADES_FOR_CHAMPION and s.robust_score < 12:
            s.status = "Retired"
        db.add(s)
    db.commit()
    lb = [{"id": s.id, "name": s.name, "archetype": s.archetype, "status": s.status,
           "robust_score": s.robust_score, "is_champion": s.is_champion,
           **{k: (s.metrics or {}).get(k) for k in ("roi", "profit_factor", "win_rate",
              "expected_value", "max_drawdown", "sharpe", "calmar", "consistency", "trades")}}
          for s in strategies]
    return {"champion": (new_champ.name if new_champ else None),
            "champion_changed": changed, "ranked": len(strategies),
            "eligible": len(eligible), "leaderboard": lb}


# ===========================================================================
# Phase 8 — automatic hypothesis engine
# ===========================================================================
def _feature_map(db: Session) -> dict:
    return {c["market_id"]: c["features"] for c in _decision_contexts(db)}


def _bucket_winrate(trades, fmap, feature):
    """Split a strategy's trades at the median of `feature` and compare win rate."""
    vals = [(t, fmap.get(t.market_id, {}).get(feature)) for t in trades]
    vals = [(t, v) for t, v in vals if v is not None and t.won is not None]
    if len(vals) < 8:
        return None
    med = statistics.median(v for _, v in vals)
    lo = [t for t, v in vals if v <= med]
    hi = [t for t, v in vals if v > med]
    if len(lo) < 3 or len(hi) < 3:
        return None
    lo_wr = sum(1 for t in lo if t.won) / len(lo)
    hi_wr = sum(1 for t in hi if t.won) / len(hi)
    return {"feature": feature, "median": round(med, 4), "low_wr": round(lo_wr, 3),
            "high_wr": round(hi_wr, 3), "n_low": len(lo), "n_high": len(hi),
            "delta": round(hi_wr - lo_wr, 3)}


def _status_from_delta(ev: dict) -> str:
    n = min(ev["n_low"], ev["n_high"])
    d = abs(ev["delta"])
    if n >= 10 and d >= 0.15:
        return "Confirmed"
    if n >= 6 and d >= 0.08:
        return "Testing"
    if d < 0.03:
        return "Rejected"
    return "Inconclusive"


def generate_hypotheses(db: Session) -> dict:
    """Automatically generate + evaluate research hypotheses from paper-trade
    evidence (Phase 8). Stores each with status + evidence."""
    fmap = _feature_map(db)
    created = []

    def _trades(arch=None, sid=None):
        q = select(rm.StrategyPaperTrade)
        if sid:
            q = q.where(rm.StrategyPaperTrade.strategy_id == sid)
        rows = db.scalars(q).all()
        if arch:
            ids = {s.id for s in db.scalars(select(rm.ResearchStrategy).where(
                rm.ResearchStrategy.archetype == arch)).all()}
            rows = [r for r in rows if r.strategy_id in ids]
        return rows

    # H1: momentum after high ATR
    ev = _bucket_winrate(_trades(arch="momentum"), fmap, "atr")
    if ev:
        better = "after HIGH ATR" if ev["delta"] > 0 else "after LOW ATR"
        created.append(("Momentum strategies win more " + better,
                        "momentum", ev, _status_from_delta(ev)))
    # H2: consensus-3 vs consensus-2 (compare robust scores)
    c2 = db.scalar(select(rm.ResearchStrategy).where(rm.ResearchStrategy.name == "Consensus-3"))
    c3 = db.scalar(select(rm.ResearchStrategy).where(rm.ResearchStrategy.name == "Consensus-2"))
    if c2 and c3 and c2.trades >= 5 and c3.trades >= 5:
        delta = round(c2.robust_score - c3.robust_score, 2)
        ev2 = {"consensus3_score": c2.robust_score, "consensus2_score": c3.robust_score, "delta": delta}
        st = "Confirmed" if delta > 5 else "Rejected" if delta < -5 else "Inconclusive"
        created.append(("Consensus of 3 wallets beats consensus of 2", "consensus", ev2, st))
    # H3: any strategy — entries in low prior-volume markets
    champ = db.scalar(select(rm.ResearchStrategy).where(rm.ResearchStrategy.is_champion.is_(True)))
    if champ:
        ev3 = _bucket_winrate(_trades(sid=champ.id), fmap, "volume_prior_norm")
        if ev3:
            better = "in HIGH-volume" if ev3["delta"] > 0 else "in LOW-volume"
            created.append((f"Champion '{champ.name}' performs better {better} markets",
                            "volume", ev3, _status_from_delta(ev3)))
    # H4: trend-following after strong trend slope
    ev4 = _bucket_winrate(_trades(arch="momentum"), fmap, "trend_slope")
    if ev4:
        created.append(("Momentum edge grows with trend-slope magnitude", "trend", ev4, _status_from_delta(ev4)))

    rows = []
    for text, cat, ev, status in created:
        rows.append(rm.ResearchHypothesis(text=text, category=cat, status=status,
                                          evidence=ev, tested_at=datetime.utcnow()))
    db.add_all(rows)
    db.commit()
    return {"generated": len(rows),
            "confirmed": sum(1 for _, _, _, s in created if s == "Confirmed"),
            "rejected": sum(1 for _, _, _, s in created if s == "Rejected")}


# ===========================================================================
# Phase 7 — nightly research review (18 sections, stored permanently)
# ===========================================================================
def nightly_review(db: Session) -> dict:
    """Generate + permanently store the nightly research review (18 sections)."""
    prev = db.scalar(select(rm.NightlyReview).order_by(rm.NightlyReview.created_at.desc()))
    psnap = (prev.report or {}).get("_snapshot", {}) if prev else {}

    n_markets = db.scalar(select(func.count()).select_from(bm.Btc5mMarket)) or 0
    n_wallets = db.scalar(select(func.count()).select_from(bm.Btc5mWalletProfile)) or 0
    n_profitable = db.scalar(select(func.count()).select_from(bm.Btc5mWalletProfile)
                             .where(bm.Btc5mWalletProfile.profitable.is_(True))) or 0
    strategies = db.scalars(select(rm.ResearchStrategy)).all()
    n_strats = len(strategies)
    n_retired = sum(1 for s in strategies if s.status == "Retired")
    champ = next((s for s in strategies if s.is_champion), None)
    cons = btc5m.consensus(db)
    champ_row = btc5m.champion(db, "global")
    feats = (champ_row.feature_importance[:6] if champ_row else [])
    prev_scores = psnap.get("strategy_scores", {})

    # mutations this period = children created after the previous review
    cut = prev.created_at if prev else datetime(2000, 1, 1)
    children = [s for s in strategies if s.parent_id and s.created_at >= cut]
    best_mut = sorted(children, key=lambda s: -s.robust_score)[:3]
    worst_mut = sorted(children, key=lambda s: s.robust_score)[:3]
    degraded = [{"name": s.name, "from": prev_scores.get(str(s.id)), "to": s.robust_score}
                for s in strategies if str(s.id) in prev_scores
                and s.robust_score < prev_scores[str(s.id)] - 5]
    ensembles = [s for s in strategies if s.is_ensemble]
    best_base = max((s for s in strategies if not s.is_ensemble), key=lambda s: s.robust_score, default=None)
    best_ens = max(ensembles, key=lambda s: s.robust_score, default=None)
    unexpected = []
    if best_ens and best_base and best_ens.robust_score > best_base.robust_score + 3:
        unexpected.append(f"Ensemble '{best_ens.name}' ({best_ens.robust_score}) beats the best single strategy '{best_base.name}' ({best_base.robust_score}).")
    for s in best_mut:
        parent = next((p for p in strategies if p.id == s.parent_id), None)
        if parent and s.robust_score > parent.robust_score + 5:
            unexpected.append(f"Mutation '{s.name}' improved on parent '{parent.name}' by {round(s.robust_score - parent.robust_score, 1)} pts.")

    hyps = db.scalars(select(rm.ResearchHypothesis).order_by(rm.ResearchHypothesis.created_at.desc()).limit(12)).all()
    top_hyps = [h.text for h in hyps if h.status in ("Confirmed", "Testing")][:5]

    report = {
        "1_new_markets_indexed": n_markets - psnap.get("markets", 0),
        "2_new_wallets_discovered": n_wallets - psnap.get("wallets", 0),
        "3_wallets_promoted": max(0, n_profitable - psnap.get("profitable", 0)),
        "4_wallets_demoted": max(0, psnap.get("profitable", 0) - n_profitable),
        "5_strategies_created": n_strats - psnap.get("strategies", 0),
        "6_strategies_retired": n_retired - psnap.get("retired", 0),
        "7_champion_strategy": champ.name if champ else None,
        "8_champion_changed": (champ.name if champ else None) != psnap.get("champion"),
        "9_performance_degradation": degraded[:5],
        "10_new_consensus_groups": len(cons.get("consensus_groups", [])) - psnap.get("consensus_groups", 0),
        "11_new_market_patterns": [f.get("feature") for f in feats],
        "12_feature_importance_changes": _feat_delta(feats, psnap.get("features", [])),
        "13_best_mutations": [{"name": s.name, "robust_score": s.robust_score} for s in best_mut],
        "14_worst_mutations": [{"name": s.name, "robust_score": s.robust_score} for s in worst_mut],
        "15_unexpected_discoveries": unexpected,
        "16_research_recommendations": _recommendations(strategies, champ, ensembles),
        "17_suggested_experiments": _experiments(strategies),
        "18_top_hypotheses_tomorrow": top_hyps,
        "_snapshot": {"markets": n_markets, "wallets": n_wallets, "profitable": n_profitable,
                      "strategies": n_strats, "retired": n_retired,
                      "champion": champ.name if champ else None,
                      "consensus_groups": len(cons.get("consensus_groups", [])),
                      "features": feats, "strategy_scores": {str(s.id): s.robust_score for s in strategies}},
    }
    summary = (f"{report['5_strategies_created']} new strategies, champion="
               f"{report['7_champion_strategy']}, {len(children)} mutations, "
               f"{report['1_new_markets_indexed']} new markets, {len(top_hyps)} live hypotheses.")
    row = rm.NightlyReview(summary=summary, report=report)
    db.add(row)
    db.add(rm.ResearchExperiment(kind="cycle", title="nightly review", detail={"summary": summary}))
    db.commit()
    return {"id": row.id, "summary": summary, "report": report}


def _feat_delta(now_feats, prev_feats):
    prev = {f.get("feature"): f.get("importance") for f in (prev_feats or [])}
    out = []
    for f in now_feats:
        name = f.get("feature")
        d = round((f.get("importance") or 0) - (prev.get(name) or 0), 4)
        if abs(d) >= 0.02:
            out.append({"feature": name, "delta": d})
    return out


def _recommendations(strategies, champ, ensembles):
    recs = []
    if champ:
        recs.append(f"Promote more mutations around the champion '{champ.name}' (archetype {champ.archetype}).")
    weak = [s for s in strategies if s.trades >= MIN_TRADES_FOR_CHAMPION and s.robust_score < 15]
    if weak:
        recs.append(f"Retire {len(weak)} persistently weak strategies to focus compute.")
    if ensembles and champ and not champ.is_ensemble:
        recs.append("Investigate why ensembles trail the best single strategy — try weighting members by robust score.")
    return recs[:6]


def _experiments(strategies):
    exps = ["Sweep min_confidence on the top momentum strategy.",
            "Test volatility-scaled sizing vs fixed on the champion.",
            "Add a consensus-4 strategy and compare to consensus-3."]
    return exps


# ===========================================================================
# Phase 10 — continuous-learning orchestrator (reproducible; never overwrites)
# ===========================================================================
def research_cycle(db: Session, *, limit_markets: int | None = 120, train: bool = True,
                   mutate: bool = True) -> dict:
    """One full research cycle: refresh the Lab, seed/discover strategies, mutate,
    build ensembles, replay+paper-trade independently, run the tournament, generate
    hypotheses, and store a permanent nightly review. Reproducible — replay is
    deterministic and mutations/reviews are append-only."""
    lab = btc5m.refresh(db, limit_markets=limit_markets, train=train)
    seeded = seed_strategies(db)
    replay_all(db)                       # 1st pass: score seeds so mutation has eligible parents
    muts = mutate_top(db) if mutate else {"mutations_created": 0}
    ens = build_ensembles(db)
    replay = replay_all(db)              # 2nd pass: replay children + ensembles, rescore all
    tour = tournament(db)
    hyps = generate_hypotheses(db)
    review = nightly_review(db)
    db.add(rm.ResearchExperiment(kind="cycle", title="research cycle",
           detail={"lab": {k: lab.get(k) for k in ("dataset", "fingerprint", "champion")},
                   "seeded": seeded, "mutations": muts, "ensembles": ens,
                   "replay": replay, "tournament": {k: tour[k] for k in ("champion", "champion_changed", "ranked")}}))
    db.commit()
    return {"lab": {"champion": lab.get("champion"), "dataset": lab.get("dataset")},
            "seeded": seeded, "mutations": muts, "ensembles": ens, "replay": replay,
            "champion": tour["champion"], "champion_changed": tour["champion_changed"],
            "strategies_ranked": tour["ranked"], "hypotheses": hyps,
            "nightly_review_id": review["id"], "nightly_summary": review["summary"]}


# ===========================================================================
# Read APIs for the frontend
# ===========================================================================
def _strat_dict(s: rm.ResearchStrategy, *, full: bool = False) -> dict:
    d = {"id": s.id, "name": s.name, "archetype": s.archetype, "status": s.status,
         "version": s.version, "parent_id": s.parent_id, "is_champion": s.is_champion,
         "is_ensemble": s.is_ensemble, "origin_cluster": s.origin_cluster,
         "origin_wallets": s.origin_wallets, "robust_score": s.robust_score,
         "trades": s.trades, "paper_bankroll": s.paper_bankroll,
         "created_at": s.created_at.isoformat() if s.created_at else None,
         "metrics": s.metrics or {}}
    if full:
        d["description"] = s.description
        d["params"] = s.params
        d["equity_curve"] = s.equity_curve
    return d


def strategy_library(db: Session, *, status: str | None = None, limit: int = 300) -> list[dict]:
    q = select(rm.ResearchStrategy).order_by(rm.ResearchStrategy.robust_score.desc())
    if status:
        q = q.where(rm.ResearchStrategy.status == status)
    return [_strat_dict(s) for s in db.scalars(q.limit(limit)).all()]


def strategy_detail(db: Session, strategy_id: int, *, trades_limit: int = 100) -> dict | None:
    s = db.get(rm.ResearchStrategy, strategy_id)
    if not s:
        return None
    trades = db.scalars(select(rm.StrategyPaperTrade)
                        .where(rm.StrategyPaperTrade.strategy_id == strategy_id)
                        .order_by(rm.StrategyPaperTrade.decision_at.desc()).limit(trades_limit)).all()
    children = db.scalars(select(rm.ResearchStrategy).where(rm.ResearchStrategy.parent_id == strategy_id)).all()
    return {"strategy": _strat_dict(s, full=True),
            "lineage": {"parent_id": s.parent_id, "children": [{"id": c.id, "name": c.name,
                        "robust_score": c.robust_score} for c in children]},
            "paper_trades": [{"id": t.id, "market_id": t.market_id, "market": t.market_question,
                              "decision_at": t.decision_at.isoformat() if t.decision_at else None,
                              "action": t.action, "direction": t.direction, "entry_price": t.entry_price,
                              "confidence": t.confidence, "edge": t.edge, "size": t.size,
                              "won": t.won, "realized_pnl": t.realized_pnl,
                              "bankroll_after": t.bankroll_after, "explanation": t.explanation}
                             for t in trades]}


def champion_board(db: Session) -> dict:
    champ = db.scalar(select(rm.ResearchStrategy).where(rm.ResearchStrategy.is_champion.is_(True)))
    history = db.scalars(select(rm.ResearchExperiment).where(rm.ResearchExperiment.kind == "champion")
                         .order_by(rm.ResearchExperiment.created_at.desc()).limit(20)).all()
    return {"champion": _strat_dict(champ, full=True) if champ else None,
            "history": [{"title": h.title, "detail": h.detail,
                         "created_at": h.created_at.isoformat() if h.created_at else None} for h in history]}


def hypotheses(db: Session, *, limit: int = 60) -> list[dict]:
    rows = db.scalars(select(rm.ResearchHypothesis)
                      .order_by(rm.ResearchHypothesis.created_at.desc()).limit(limit)).all()
    return [{"id": h.id, "text": h.text, "category": h.category, "status": h.status,
             "evidence": h.evidence, "created_at": h.created_at.isoformat() if h.created_at else None}
            for h in rows]


def nightly_reviews(db: Session, *, limit: int = 30) -> list[dict]:
    rows = db.scalars(select(rm.NightlyReview)
                      .order_by(rm.NightlyReview.created_at.desc()).limit(limit)).all()
    return [{"id": r.id, "summary": r.summary, "report": r.report,
             "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]


def experiments(db: Session, *, limit: int = 80) -> list[dict]:
    rows = db.scalars(select(rm.ResearchExperiment)
                      .order_by(rm.ResearchExperiment.created_at.desc()).limit(limit)).all()
    return [{"id": e.id, "kind": e.kind, "strategy_id": e.strategy_id, "parent_id": e.parent_id,
             "title": e.title, "detail": e.detail,
             "created_at": e.created_at.isoformat() if e.created_at else None} for e in rows]


def dashboard(db: Session) -> dict:
    strategies = db.scalars(select(rm.ResearchStrategy)).all()
    by_status: dict[str, int] = {}
    for s in strategies:
        by_status[s.status] = by_status.get(s.status, 0) + 1
    champ = next((s for s in strategies if s.is_champion), None)
    n_trades = db.scalar(select(func.count()).select_from(rm.StrategyPaperTrade)) or 0
    top = sorted(strategies, key=lambda s: -s.robust_score)[:8]
    hyps = db.scalars(select(rm.ResearchHypothesis)).all()
    last_review = db.scalar(select(rm.NightlyReview).order_by(rm.NightlyReview.created_at.desc()))
    return {
        "total_strategies": len(strategies),
        "by_status": by_status,
        "ensembles": sum(1 for s in strategies if s.is_ensemble),
        "paper_trades": n_trades,
        "champion": _strat_dict(champ, full=True) if champ else None,
        "top_strategies": [_strat_dict(s) for s in top],
        "hypotheses_total": len(hyps),
        "hypotheses_confirmed": sum(1 for h in hyps if h.status == "Confirmed"),
        "last_review": {"summary": last_review.summary,
                        "created_at": last_review.created_at.isoformat()} if last_review else None,
        "safety": "paper research only — never places orders or changes production/live trading",
    }
