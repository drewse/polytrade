"""Paper Challenger Framework V1 — isolated A/B research layer for the BTC 5M
Reversal Lab.

Every production "would-buy" opportunity becomes an immutable experiment in which
paper-only challengers (timing / sizing / confidence / consensus / strategy
variants) compete against the production decision. Each challenger keeps its own
INDEPENDENT paper portfolio. The framework learns better timing/sizing/confidence/
consensus/strategy via statistically-significant A/B testing — and NEVER touches
live trading, execution, eligibility, rankings, bankroll, copy trading, production
strategies, or risk controls.

100% READ-ONLY w.r.t. production: reads btc5m_*/research_*/mi_* and writes only
pc_* tables.
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import btc5m
from . import btc5m_models as bm
from . import challenger_models as cm
from . import market_intel
from . import research

BASE_STAKE = 5.0           # base paper stake per acted opportunity (sizing variants scale it)
START_BANKROLL = 100.0
MIN_SAMPLE = 20            # don't declare a winner before this many paired trades
CONF_THRESHOLDS = [70, 72, 75, 78, 80, 85, 90]


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs):
    return statistics.pstdev(xs) if len(xs) > 1 else 0.0


def _clip(v, lo, hi):
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# Canonical challenger set
# ---------------------------------------------------------------------------
def _challenger_specs() -> list[dict]:
    specs = [
        {"key": "production", "name": "Production (immediate · full size · conf≥70 · 2-wallet)",
         "kind": "production", "params": {}, "is_production": True},
        # timing
        {"key": "timing_+5", "name": "Entry +5s", "kind": "timing", "params": {"shift_s": 5}},
        {"key": "timing_+10", "name": "Entry +10s", "kind": "timing", "params": {"shift_s": 10}},
        {"key": "timing_+20", "name": "Entry +20s", "kind": "timing", "params": {"shift_s": 20}},
        {"key": "timing_-5", "name": "Entry -5s (replay only)", "kind": "timing", "params": {"shift_s": -5}},
        # sizing
        {"key": "size_half", "name": "Half position", "kind": "sizing", "params": {"mode": "mult", "mult": 0.5}},
        {"key": "size_150", "name": "150% position", "kind": "sizing", "params": {"mode": "mult", "mult": 1.5}},
        {"key": "size_double", "name": "Double position", "kind": "sizing", "params": {"mode": "mult", "mult": 2.0}},
        {"key": "size_confidence", "name": "Confidence-weighted size", "kind": "sizing", "params": {"mode": "confidence"}},
        {"key": "size_kelly", "name": "Kelly-style fraction (research)", "kind": "sizing", "params": {"mode": "kelly"}},
        {"key": "size_sharecap", "name": "Share-cap only", "kind": "sizing", "params": {"mode": "sharecap"}},
        # consensus
        {"key": "cons_none", "name": "No consensus (1+ wallet)", "kind": "consensus", "params": {"mode": "none"}},
        {"key": "cons_2", "name": "2-wallet consensus", "kind": "consensus", "params": {"mode": "2"}},
        {"key": "cons_3", "name": "3-wallet consensus", "kind": "consensus", "params": {"mode": "3"}},
        {"key": "cons_weighted", "name": "Weighted consensus", "kind": "consensus", "params": {"mode": "weighted"}},
        {"key": "cons_originality", "name": "Originality-weighted consensus", "kind": "consensus", "params": {"mode": "originality_weighted"}},
        {"key": "cons_leader", "name": "Leader-only consensus", "kind": "consensus", "params": {"mode": "leader_only"}},
        # strategy challengers
        {"key": "strat_wallet_copy", "name": "Wallet Copy", "kind": "strategy", "params": {"archetype": "consensus"}},
        {"key": "strat_momentum", "name": "Momentum", "kind": "strategy", "params": {"archetype": "momentum"}},
        {"key": "strat_mean_reversion", "name": "Mean Reversion", "kind": "strategy", "params": {"archetype": "mean_reversion"}},
        {"key": "strat_consensus_alpha", "name": "Consensus Alpha (3+)", "kind": "strategy", "params": {"archetype": "consensus", "min_wallets": 3}},
        {"key": "strat_meta_ensemble", "name": "Meta Ensemble", "kind": "strategy", "params": {"archetype": "meta"}},
        {"key": "strat_counterfactual", "name": "Counterfactual Entry (+5s)", "kind": "strategy", "params": {"archetype": "counterfactual"}},
    ]
    for c in CONF_THRESHOLDS:
        specs.append({"key": f"conf_{c}", "name": f"Confidence ≥ {c}", "kind": "confidence", "params": {"min_conf": c}})
    return specs


def seed_challengers(db: Session) -> int:
    existing = {c.key for c in db.scalars(select(cm.PcChallenger)).all()}
    n = 0
    for s in _challenger_specs():
        if s["key"] in existing:
            continue
        db.add(cm.PcChallenger(key=s["key"], name=s["name"], kind=s["kind"], params=s["params"],
                               is_production=s.get("is_production", False), paper_bankroll=START_BANKROLL))
        n += 1
    db.commit()
    return n


# ---------------------------------------------------------------------------
# Per-market evaluation
# ---------------------------------------------------------------------------
def _price_path(db: Session, market_id: str):
    ts = db.scalars(select(bm.Btc5mTrade).where(bm.Btc5mTrade.market_id == market_id)
                    .order_by(bm.Btc5mTrade.timestamp.asc())).all()
    return ([t.seconds_from_creation or 0 for t in ts], [market_intel._implied_yes(t) for t in ts])


def _production_signal(ctx) -> dict | None:
    c = ctx["consensus"]
    n = c["n"]
    if n == 0:
        return None
    direction = "YES" if c["yes"] >= c["no"] else "NO"
    conf = round(max(c["yes"], c["no"]) / n * 100.0, 1)
    return {"dir": direction, "conf": conf, "n": n}


def _NO(conf=0.0):
    return {"acts": False, "action": "NO_TRADE", "direction": None, "entry_price": 0.0,
            "confidence": conf, "size": 0.0, "shares": 0.0, "won": None, "pnl": 0.0}


def _evaluate(ch: dict, ctx, base, path, leaders: set) -> dict:
    """Evaluate one challenger on one market context. Pure + deterministic."""
    kind, p = ch["kind"], ch["params"]
    feats = ctx["features"]
    outcome = ctx["outcome"]
    base_secs = research.DECISION_OFFSET_S
    base_price = feats.get("market_yes_price", 0.5)

    def _trade(direction, shift=0, stake=BASE_STAKE, conf=0.0):
        y = market_intel._price_at(path[0], path[1], base_secs + shift) if path[0] else base_price
        pdir = _clip(y if direction == "YES" else 1.0 - y, 0.02, 0.98)
        shares = stake / pdir
        won = (direction == outcome)
        pnl = round(shares * (1.0 if won else 0.0) - stake, 4)
        return {"acts": True, "action": "BUY_" + direction, "direction": direction,
                "entry_price": round(pdir, 4), "confidence": round(conf, 1), "size": round(stake, 2),
                "shares": round(shares, 2), "won": won, "pnl": pnl}

    if kind in ("production", "timing", "sizing", "confidence", "consensus"):
        if base is None:
            return _NO()
        d, conf, n = base["dir"], base["conf"], base["n"]
        wallets = set(ctx["consensus"].get("wallets", []))
        if kind == "confidence" and conf < p["min_conf"]:
            return _NO(conf)
        if kind == "consensus":
            mode = p["mode"]
            if (mode == "none" and n < 1) or (mode == "2" and n < 2) or (mode == "3" and n < 3):
                return _NO(conf)
            if mode == "weighted" and conf < 66:
                return _NO(conf)
            if mode == "originality_weighted" and (conf < 66 or not (wallets & leaders)):
                return _NO(conf)
            if mode == "leader_only" and not (wallets & leaders):
                return _NO(conf)
        else:                                  # production / timing / sizing share the base gate
            if n < 2 or conf < 70:
                return _NO(conf)
        stake, shift = BASE_STAKE, 0
        if kind == "timing":
            shift = p["shift_s"]
        if kind == "sizing":
            mode = p["mode"]
            if mode == "mult":
                stake = BASE_STAKE * p["mult"]
            elif mode == "confidence":
                stake = BASE_STAKE * _clip(conf / 100.0 * 1.5, 0.3, 1.5)
            elif mode == "kelly":
                pwin = conf / 100.0
                price = base_price if d == "YES" else 1.0 - base_price
                edge = pwin - _clip(price, 0.02, 0.98)
                stake = BASE_STAKE * _clip(edge * 4, 0.1, 2.0)      # research-only kelly-ish fraction
            elif mode == "sharecap":
                stake = _clip(base_price if d == "YES" else 1.0 - base_price, 0.02, 0.98) * 20
        return _trade(d, shift, stake, conf)

    if kind == "strategy":
        arch = p["archetype"]
        if arch == "counterfactual":
            if base is None or base["n"] < 2 or base["conf"] < 70:
                return _NO()
            return _trade(base["dir"], shift=5, conf=base["conf"])
        if arch == "meta":
            votes = []
            for a in ("momentum", "mean_reversion", "consensus"):
                dec = research._decide(a, {"min_confidence": 0.1}, ctx)
                if dec["action"] != "NO_TRADE":
                    votes.append(dec.get("p_yes", 0.5))
            if not votes:
                return _NO()
            pmean = _mean(votes)
            dconf = abs(pmean - 0.5) * 2
            if dconf < 0.1:
                return _NO()
            return _trade("YES" if pmean >= 0.5 else "NO", conf=round(dconf * 100, 1))
        params = {"min_confidence": 0.1}
        if arch == "consensus" and p.get("min_wallets"):
            params["min_wallets"] = p["min_wallets"]
        dec = research._decide(arch, params, ctx)
        if dec["action"] == "NO_TRADE":
            return _NO()
        return _trade(dec["direction"], conf=round(dec["confidence"] * 100, 1))
    return _NO()


# ---------------------------------------------------------------------------
# Statistical significance (paired challenger-vs-production differences)
# ---------------------------------------------------------------------------
def _normal_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _significance(diffs: list[float]) -> dict:
    n = len(diffs)
    mean, sd = _mean(diffs), _std(diffs)
    if n < MIN_SAMPLE:
        return {"n": n, "mean_improvement": round(mean, 4), "std": round(sd, 4),
                "t": None, "p_value": None, "significance": "Insufficient Data"}
    se = max(sd / math.sqrt(n) if n > 0 else 0.0, 1e-9)
    t = mean / se        # zero-variance + non-zero mean -> maximally significant
    p = round(2 * (1 - _normal_cdf(abs(t))), 4)
    if abs(t) >= 2.0:
        sig = "Regressing" if mean < 0 else "Significant"
    elif abs(t) >= 1.3:
        sig = "Promising"
    elif n >= MIN_SAMPLE * 3 and abs(mean) < 0.02:
        sig = "Rejected"
    else:
        sig = "Promising" if mean > 0 else "Insufficient Data"
    return {"n": n, "mean_improvement": round(mean, 4), "std": round(sd, 4), "t": round(t, 3),
            "p_value": p, "significance": sig,
            "ci95": [round(mean - 1.96 * se, 4), round(mean + 1.96 * se, 4)]}


def _rolling_decay(trades) -> dict:
    rows = [{"date": t.decision_at, "won": bool(t.won), "pnl": t.realized_pnl, "stake": t.size} for t in trades]
    max_date = max((r["date"] for r in rows if r["date"]), default=None)
    return market_intel._rolling(rows, max_date)


# ---------------------------------------------------------------------------
# Experiment engine (immutable, append-only) + portfolios
# ---------------------------------------------------------------------------
def run_experiments(db: Session) -> dict:
    """Create one immutable experiment per NEW production 'would-buy' opportunity;
    record each challenger's paper trade. Existing experiments are never re-run."""
    seed_challengers(db)
    contexts = research._decision_contexts(db)
    regimes = market_intel._regime_map(db)
    try:
        leaders = {w["wallet"] for w in market_intel.originality(db).get("wallets", [])
                   if w.get("role") == "leader"}
    except Exception:  # noqa: BLE001  (originality optional)
        leaders = set()
    challengers = db.scalars(select(cm.PcChallenger)).all()
    new_exp = 0
    for ctx in contexts:
        if db.scalar(select(cm.PcExperiment).where(cm.PcExperiment.market_id == ctx["market_id"])):
            continue                                   # immutable — never overwrite
        base = _production_signal(ctx)
        if not (base and base["n"] >= 2 and base["conf"] >= 70):
            continue                                   # production would NOT buy -> no experiment
        regime = regimes.get(ctx["market_id"], "Mixed")
        path = _price_path(db, ctx["market_id"])
        exp = cm.PcExperiment(market_id=ctx["market_id"], market_question=ctx["question"],
                              regime=regime, decision_at=ctx["decision_at"], outcome=ctx["outcome"])
        db.add(exp); db.flush()
        decisions, prod_pnl = {}, 0.0
        for c in challengers:
            d = _evaluate({"kind": c.kind, "params": c.params}, ctx, base, path, leaders)
            decisions[c.key] = {"action": d["action"], "pnl": d["pnl"], "won": d["won"],
                                "entry_price": d["entry_price"], "size": d["size"], "confidence": d["confidence"]}
            if c.is_production:
                prod_pnl = d["pnl"]
            if d["acts"]:
                db.add(cm.PcTrade(challenger_id=c.id, challenger_key=c.key, experiment_id=exp.id,
                                  market_id=ctx["market_id"], regime=regime, action=d["action"],
                                  direction=d["direction"], entry_price=d["entry_price"], confidence=d["confidence"],
                                  size=d["size"], shares=d["shares"], won=d["won"], realized_pnl=d["pnl"],
                                  decision_at=ctx["decision_at"]))
        winner = max(decisions, key=lambda k: decisions[k]["pnl"])
        exp.production_decision = decisions.get("production", {})
        exp.challenger_decisions = decisions
        exp.winner = winner
        exp.improvement = round(decisions[winner]["pnl"] - prod_pnl, 4)
        db.add(exp)
        new_exp += 1
    db.commit()
    return {"new_experiments": new_exp,
            "total_experiments": db.scalar(select(func.count()).select_from(cm.PcExperiment))}


def rebuild_portfolios(db: Session) -> dict:
    challengers = db.scalars(select(cm.PcChallenger)).all()
    experiments = db.scalars(select(cm.PcExperiment)).all()
    prod_pnl = {e.market_id: (e.production_decision or {}).get("pnl", 0.0) for e in experiments}
    for c in challengers:
        trades = db.scalars(select(cm.PcTrade).where(cm.PcTrade.challenger_id == c.id)
                            .order_by(cm.PcTrade.decision_at.asc())).all()
        bank, equity, dates, meta = START_BANKROLL, [], [], []
        for t in trades:
            bank = round(bank + t.realized_pnl, 4)
            equity.append(bank); dates.append(t.decision_at)
            meta.append({"realized_pnl": t.realized_pnl, "size": t.size, "won": bool(t.won),
                         "confidence": t.confidence / 100.0, "edge": 0.0})
        metrics = research._compute_metrics(meta, equity, dates)
        c.trades = metrics["trades"]
        c.paper_bankroll = metrics["final_bankroll"]
        c.metrics = metrics
        c.robust_score = research._robust_score(metrics)
        c.equity_curve = [{"t": d.isoformat() if d else None, "equity": e} for d, e in zip(dates, equity)][-200:]
        # vs-production paired differences across ALL experiments
        diffs, by_regime = [], {}
        for e in experiments:
            cp = (e.challenger_decisions or {}).get(c.key, {}).get("pnl", 0.0)
            d = cp - prod_pnl.get(e.market_id, 0.0)
            diffs.append(d)
            r = by_regime.setdefault(e.regime, {"d": [], "ch": []})
            r["d"].append(d); r["ch"].append(cp)
        c.vs_production = _significance(diffs)
        c.by_regime = {rg: {"trades": len(v["ch"]), "mean_improvement": round(_mean(v["d"]), 4),
                            "improvement_pct": round(_mean(v["d"]) / BASE_STAKE * 100, 2),
                            "challenger_pnl": round(sum(v["ch"]), 2)} for rg, v in by_regime.items()}
        c.decay = _rolling_decay(trades)
        db.add(c)
    db.commit()
    return {"challengers": len(challengers)}


def pick_champion(db: Session) -> dict:
    challengers = db.scalars(select(cm.PcChallenger)).all()
    for c in challengers:
        c.is_champion = False
    eligible = [c for c in challengers if not c.is_production
                and (c.vs_production or {}).get("significance") in ("Significant", "Promising")
                and (c.vs_production or {}).get("mean_improvement", 0) > 0
                and (c.vs_production or {}).get("n", 0) >= MIN_SAMPLE]
    champ = max(eligible, key=lambda c: (c.robust_score, (c.vs_production or {}).get("mean_improvement", 0)),
                default=None)
    if champ is None:
        champ = next((c for c in challengers if c.is_production), None)   # status quo wins by default
    if champ:
        champ.is_champion = True
        db.add(champ)
    db.commit()
    return {"champion": champ.key if champ else None,
            "champion_improvement": (champ.vs_production or {}).get("mean_improvement") if champ and not champ.is_production else 0.0}


# ---------------------------------------------------------------------------
# Recommendations + nightly review
# ---------------------------------------------------------------------------
_CAT = {"timing": "timing", "sizing": "sizing", "confidence": "confidence",
        "consensus": "consensus", "strategy": "strategy"}


def generate_recommendations(db: Session) -> dict:
    challengers = db.scalars(select(cm.PcChallenger)).all()
    db.query(cm.PcRecommendation).delete(synchronize_session=False)   # recompute current recs (history kept in reviews)
    n = 0
    for c in challengers:
        if c.is_production:
            continue
        vs = c.vs_production or {}
        if vs.get("significance") not in ("Significant", "Promising") or vs.get("mean_improvement", 0) <= 0:
            continue
        pct = round(vs["mean_improvement"] / BASE_STAKE * 100, 1)
        # best regime for this challenger
        best_rg = max((c.by_regime or {}).items(), key=lambda kv: kv[1]["mean_improvement"], default=None)
        rg_txt = ""
        if best_rg and best_rg[1]["trades"] >= 5:
            rg_txt = f" — strongest in {best_rg[0]} markets ({best_rg[1]['improvement_pct']}%)"
        text = (f"'{c.name}' has outperformed production by {pct}% over {vs['n']} trades "
                f"({vs['significance']}, p={vs.get('p_value')}){rg_txt}.")
        db.add(cm.PcRecommendation(category=_CAT.get(c.kind, "strategy"), text=text,
                                   significance=vs["significance"], scope="global",
                                   evidence={"improvement_pct": pct, "n": vs["n"], "p_value": vs.get("p_value"),
                                             "by_regime": c.by_regime}))
        n += 1
    db.commit()
    return {"recommendations": n}


def nightly_review(db: Session) -> dict:
    challengers = db.scalars(select(cm.PcChallenger)).all()
    nonprod = [c for c in challengers if not c.is_production]
    n_exp = db.scalar(select(func.count()).select_from(cm.PcExperiment)) or 0
    winners = sorted([c for c in nonprod if (c.vs_production or {}).get("mean_improvement", 0) > 0],
                     key=lambda c: -(c.vs_production or {}).get("mean_improvement", 0))[:5]
    losers = sorted([c for c in nonprod if (c.vs_production or {}).get("mean_improvement", 0) < 0],
                    key=lambda c: (c.vs_production or {}).get("mean_improvement", 0))[:5]
    champ = next((c for c in challengers if c.is_champion), None)

    def _best(kind):
        cands = [c for c in nonprod if c.kind == kind]
        b = max(cands, key=lambda c: (c.vs_production or {}).get("mean_improvement", 0), default=None)
        return ({"key": b.key, "name": b.name, "improvement_pct": round((b.vs_production or {}).get("mean_improvement", 0) / BASE_STAKE * 100, 1),
                 "significance": (b.vs_production or {}).get("significance")} if b else None)

    regressing = [c.key for c in nonprod if (c.vs_production or {}).get("significance") == "Regressing"]
    ready = [c.key for c in nonprod if (c.vs_production or {}).get("significance") == "Significant"
             and (c.vs_production or {}).get("mean_improvement", 0) > 0]
    report = {
        "experiments": n_exp, "challengers": len(challengers),
        "winning_challengers": [{"key": c.key, "improvement_pct": round((c.vs_production or {}).get("mean_improvement", 0) / BASE_STAKE * 100, 1),
                                 "significance": (c.vs_production or {}).get("significance")} for c in winners],
        "losing_challengers": [{"key": c.key, "improvement_pct": round((c.vs_production or {}).get("mean_improvement", 0) / BASE_STAKE * 100, 1)} for c in losers],
        "timing_improvement": _best("timing"), "sizing_improvement": _best("sizing"),
        "confidence_improvement": _best("confidence"), "consensus_improvement": _best("consensus"),
        "best_strategy_challenger": _best("strategy"),
        "overall_champion": (champ.key if champ else None),
        "strategies_regressing": regressing,
        "ready_for_manual_review": ready,
    }
    summary = (f"{n_exp} experiments; champion {report['overall_champion']}; "
               f"{len(winners)} winners, {len(losers)} losers, {len(ready)} significant; "
               f"{len(regressing)} regressing.")
    row = cm.PcNightlyReview(summary=summary, report=report)
    db.add(row); db.commit()
    return {"id": row.id, "summary": summary, "report": report}


def run_challengers(db: Session, *, refresh_lab: bool = False, limit_markets: int | None = 150) -> dict:
    """Full Paper-Challenger cycle: (optionally refresh the Lab/regimes), build new
    immutable experiments, rebuild every challenger's independent paper portfolio,
    compute paired significance vs production, pick a champion, generate
    recommendations, and store a permanent nightly review. Idempotent — existing
    experiments are never re-run; portfolios are recomputed deterministically."""
    lab = {}
    if refresh_lab:
        lab = btc5m.refresh(db, limit_markets=limit_markets, train=True)
        market_intel.build_profiles(db)
    exp = run_experiments(db)
    rebuild_portfolios(db)
    champ = pick_champion(db)
    rec = generate_recommendations(db)
    review = nightly_review(db)
    return {"lab_champion": lab.get("champion"), "experiments": exp, "champion": champ["champion"],
            "champion_improvement": champ["champion_improvement"], "recommendations": rec,
            "nightly_review_id": review["id"], "nightly_summary": review["summary"]}


# ---------------------------------------------------------------------------
# Read APIs for the frontend
# ---------------------------------------------------------------------------
def _ch_dict(c, *, full=False) -> dict:
    d = {"id": c.id, "key": c.key, "name": c.name, "kind": c.kind, "is_production": c.is_production,
         "is_champion": c.is_champion, "trades": c.trades, "paper_bankroll": c.paper_bankroll,
         "robust_score": c.robust_score, "metrics": c.metrics or {}, "vs_production": c.vs_production or {},
         "decay": c.decay or {}}
    if full:
        d["params"] = c.params
        d["by_regime"] = c.by_regime
        d["equity_curve"] = c.equity_curve
    return d


def dashboard(db: Session) -> dict:
    challengers = db.scalars(select(cm.PcChallenger)).all()
    n_exp = db.scalar(select(func.count()).select_from(cm.PcExperiment)) or 0
    n_trades = db.scalar(select(func.count()).select_from(cm.PcTrade)) or 0
    champ = next((c for c in challengers if c.is_champion), None)
    nonprod = [c for c in challengers if not c.is_production]
    leading = sorted(nonprod, key=lambda c: -(c.vs_production or {}).get("mean_improvement", 0))[:6]
    last = db.scalar(select(cm.PcNightlyReview).order_by(cm.PcNightlyReview.created_at.desc()))
    return {
        "experiments": n_exp, "paper_portfolios": len(challengers), "paper_trades": n_trades,
        "champion": _ch_dict(champ, full=True) if champ else None,
        "leading_challengers": [_ch_dict(c) for c in leading],
        "significant": sum(1 for c in nonprod if (c.vs_production or {}).get("significance") == "Significant"),
        "last_review": ({"summary": last.summary, "created_at": last.created_at.isoformat()} if last else None),
        "safety": "paper A/B research only — never places orders or changes production",
    }


def challengers(db: Session) -> list[dict]:
    rows = db.scalars(select(cm.PcChallenger).order_by(cm.PcChallenger.robust_score.desc())).all()
    return [_ch_dict(c) for c in rows]


def challenger_detail(db: Session, key: str, *, trades_limit: int = 100) -> dict | None:
    c = db.scalar(select(cm.PcChallenger).where(cm.PcChallenger.key == key))
    if not c:
        return None
    trades = db.scalars(select(cm.PcTrade).where(cm.PcTrade.challenger_id == c.id)
                        .order_by(cm.PcTrade.decision_at.desc()).limit(trades_limit)).all()
    return {"challenger": _ch_dict(c, full=True),
            "trades": [{"market_id": t.market_id, "regime": t.regime, "action": t.action,
                        "entry_price": t.entry_price, "size": t.size, "won": t.won,
                        "realized_pnl": t.realized_pnl, "bankroll_after": t.bankroll_after,
                        "decision_at": t.decision_at.isoformat() if t.decision_at else None} for t in trades]}


def comparison(db: Session, kind: str) -> list[dict]:
    rows = db.scalars(select(cm.PcChallenger).where(cm.PcChallenger.kind == kind)
                      .order_by(cm.PcChallenger.robust_score.desc())).all()
    return [{"key": c.key, "name": c.name, "trades": c.trades,
             "roi": (c.metrics or {}).get("roi"), "win_rate": (c.metrics or {}).get("win_rate"),
             "profit_factor": (c.metrics or {}).get("profit_factor"), "sharpe": (c.metrics or {}).get("sharpe"),
             "max_drawdown": (c.metrics or {}).get("max_drawdown"), "robust_score": c.robust_score,
             "vs_production": c.vs_production or {}, "by_regime": c.by_regime or {}} for c in rows]


def regime_performance(db: Session) -> dict:
    rows = db.scalars(select(cm.PcChallenger).where(cm.PcChallenger.is_production.is_(False))).all()
    regimes = sorted({rg for c in rows for rg in (c.by_regime or {})})
    out = []
    for c in rows:
        out.append({"key": c.key, "name": c.name, "kind": c.kind,
                    "by_regime": {rg: (c.by_regime or {}).get(rg, {}).get("improvement_pct") for rg in regimes}})
    return {"regimes": regimes, "challengers": out}


def experiments(db: Session, *, limit: int = 60) -> list[dict]:
    rows = db.scalars(select(cm.PcExperiment).order_by(cm.PcExperiment.created_at.desc()).limit(limit)).all()
    return [{"id": e.id, "market_id": e.market_id, "market": e.market_question, "regime": e.regime,
             "outcome": e.outcome, "winner": e.winner, "improvement": e.improvement,
             "production_decision": e.production_decision,
             "created_at": e.created_at.isoformat() if e.created_at else None} for e in rows]


def experiment_detail(db: Session, experiment_id: int) -> dict | None:
    e = db.get(cm.PcExperiment, experiment_id)
    if not e:
        return None
    return {"id": e.id, "market_id": e.market_id, "market": e.market_question, "regime": e.regime,
            "outcome": e.outcome, "winner": e.winner, "improvement": e.improvement,
            "production_decision": e.production_decision, "challenger_decisions": e.challenger_decisions,
            "created_at": e.created_at.isoformat() if e.created_at else None}


def recommendations(db: Session, *, limit: int = 50) -> list[dict]:
    rows = db.scalars(select(cm.PcRecommendation).order_by(cm.PcRecommendation.created_at.desc()).limit(limit)).all()
    return [{"category": r.category, "text": r.text, "significance": r.significance,
             "scope": r.scope, "evidence": r.evidence} for r in rows]


def nightly_reviews(db: Session, *, limit: int = 30) -> list[dict]:
    rows = db.scalars(select(cm.PcNightlyReview).order_by(cm.PcNightlyReview.created_at.desc()).limit(limit)).all()
    return [{"id": r.id, "summary": r.summary, "report": r.report,
             "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]
