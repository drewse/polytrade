"""BTC 5M Execution Research Lab (Phase 3) — research/paper ONLY.

Phase 1/2 found good predictive models (AUC up to ~0.85) but no statistically
significant positive EV after spread + slippage: as a TAKER we pay the spread.
This lab asks a different question — not "can we predict better" but:

    Can PASSIVE execution (posting bids and CAPTURING spread instead of paying it)
    raise post-cost EV enough to overcome the lower fill rate, and convert any
    currently-rejected Alpha Lab model into statistically-significant positive EV?

It simulates execution styles (market / join-bid / improve+tick / passive with
timeouts / adaptive / fair-value-maker) over historical BTC 5m signals, using the
real subsequent TRADE stream to decide fills — which naturally captures ADVERSE
SELECTION (a resting bid fills precisely when price moves down to it). It then
re-runs the exact same promotion gates with the best passive execution.

100% isolated from production: reads only the lab dataset + historical trades,
writes only btc5m_lab_* research rows. It NEVER touches live.py / services.py /
live_ranking.py / execution / bankroll / approvals / copy trading / paper trading.
There is no live-order path anywhere in this module.

DATA NOTE: BTC5m trades are timestamped at 1-second resolution, so empirical fills
are measured at 1s/2s/5s; the 250ms/500ms points come from the fitted fill-
probability model and are labelled as MODELLED, not measured. Adaptive / fair-value-
maker / book-repricing use documented approximations (no stored L2 book).
"""
from __future__ import annotations

import math
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import btc5m_alpha_research as ph1
from . import btc5m_alpha_discovery as disc
from . import btc5m_ml as ml
from . import btc5m_models as bm
from . import btc5m_strategy_lab as lab
from . import btc5m_strategy_models as lm

_mean = lab._mean
_std = lab._std
_clip = lab._clip
_state = lab._state
_num = ph1._num

DEFAULT_SPREAD = 0.02          # fallback half-spread basis when the proxy is missing
DEFAULT_TICK = 0.01            # Polymarket price tick
SLIPPAGE = ph1.DEFAULT_SLIPPAGE
TIMEOUTS = (0.25, 0.5, 1.0, 2.0, 5.0)        # seconds (sub-second = modelled)
EMPIRICAL_TIMEOUTS = (1.0, 2.0, 5.0)         # resolvable from 1s-resolution trades
MIN_TRADES = ph1.MIN_TRADES
SIG_T = ph1.SIG_T


# ---------------------------------------------------------------------------
# significance on an explicit per-trade PnL list (execution PnLs, not model gaps)
# ---------------------------------------------------------------------------
def significance(pnls: list[float], costs: list[float]) -> dict:
    n = len(pnls)
    mean = _mean(pnls)
    sd = _std(pnls)
    se = sd / math.sqrt(n) if n > 1 else 0.0
    if se:
        t = mean / se
    else:
        t = 99.0 if mean > 0 else (-99.0 if mean < 0 else 0.0)
    roi = sum(pnls) / sum(costs) if costs and sum(costs) else 0.0
    sharpe = (mean / sd) if sd else 0.0          # per-trade Sharpe (not annualised)
    return {"n_trades": n, "ev_after_cost": round(mean, 5), "roi": round(roi, 4),
            "t_stat": round(t, 3), "ci_low": round(mean - SIG_T * se, 5),
            "ci_high": round(mean + SIG_T * se, 5), "sharpe": round(sharpe, 3),
            "significant": bool(n >= MIN_TRADES and mean > 0 and t >= SIG_T)}


# ---------------------------------------------------------------------------
# signal generation (the "paper signals" Alpha Lab would act on)
# ---------------------------------------------------------------------------
def _yes_price(tr) -> float:
    return tr.price if tr.direction == "YES" else (1.0 - tr.price)


def _trades_by_market(db: Session, market_ids: set[str]) -> dict:
    out: dict[str, list] = {}
    if not market_ids:
        return out
    rows = db.scalars(select(bm.Btc5mTrade).where(bm.Btc5mTrade.market_id.in_(market_ids))).all()
    for tr in rows:
        out.setdefault(tr.market_id, []).append(tr)
    for v in out.values():
        v.sort(key=lambda tr: tr.seconds_from_creation or 0)
    return out


def build_signals(db: Session, split: str, model, feats: list[str], *, min_edge: float = 0.01) -> list[dict]:
    """One signal per decision point where the model takes a directional view. Carries
    the market mid/half-spread, the chosen side, the outcome, and the subsequent trade
    stream (delay seconds, yes-price) used to decide passive fills."""
    rows = db.scalars(select(lm.Btc5mLabPoint).where(lm.Btc5mLabPoint.split == split)).all()
    rows = [r for r in rows if r.pm_yes is not None and r.label_up is not None]
    if not rows:
        return []
    X = [[_num((r.features or {}).get(f)) for f in feats] for r in rows]
    probs = model.predict_proba(X)
    trades_by = _trades_by_market(db, {r.market_id for r in rows})
    signals = []
    for r, p in zip(rows, probs):
        m = r.pm_yes
        edge = p - m
        if abs(edge) < min_edge:
            continue
        side = "YES" if edge > 0 else "NO"
        f = r.features or {}
        half = (r.spread or 0.0) / 2 or DEFAULT_SPREAD / 2
        t = r.t_offset_s or 0
        future = []
        for tr in trades_by.get(r.market_id, []):
            d = (tr.seconds_from_creation or 0) - t
            if 0 < d <= 5:
                future.append((d, _yes_price(tr)))
        signals.append({
            "market_id": r.market_id, "t": t, "mid": m, "half": half, "side": side,
            "model_prob": p, "edge": abs(edge), "up": bool(r.label_up),
            "regime": r.regime or "?", "secs_to_expiry": r.secs_to_expiry or 0,
            "btc_vol": _num(f.get("btc_vol")), "volume_usd": _num(f.get("volume_usd")),
            "flow_imbalance": _num(f.get("flow_imbalance")), "btc_ret_sofar": _num(f.get("btc_ret_sofar")),
            "future": future,
        })
    return signals


# ---------------------------------------------------------------------------
# execution methods — each returns (filled, entry_price, fill_delay or None)
# ---------------------------------------------------------------------------
def _first_cross(sig: dict, target: float, *, up_cross: bool, timeout: float) -> float | None:
    """Delay (s) of the first subsequent trade that crosses `target`. up_cross=True ⇒
    a NO bid needs yes-price to rise to target; False ⇒ a YES bid needs it to fall."""
    for d, yp in sig["future"]:
        if d > timeout:
            break
        if (up_cross and yp >= target) or (not up_cross and yp <= target):
            return d
    return None


def _passive_entry(sig: dict, *, improve_ticks: int = 0):
    """Post at (or improve on) the bid for the chosen side. Returns (entry_price,
    crossing_target, up_cross)."""
    m, h = sig["mid"], sig["half"]
    if sig["side"] == "YES":
        bid = _clip(m - h + improve_ticks * DEFAULT_TICK, 0.01, 0.99)
        return bid, bid, False                 # fills when a seller crosses down to bid
    no_bid = _clip((1 - m) - h + improve_ticks * DEFAULT_TICK, 0.01, 0.99)
    return no_bid, _clip(1 - no_bid, 0.01, 0.99), True   # NO bid fills when yes-price rises


def _taker_entry(sig: dict) -> float:
    m, h = sig["mid"], sig["half"]
    base = (m + h) if sig["side"] == "YES" else ((1 - m) + h)
    return _clip(base + SLIPPAGE, 0.01, 0.99)


def _pnl(side: str, entry: float, up: bool) -> float:
    win = up if side == "YES" else (not up)
    return (1.0 - entry) if win else -entry


def simulate_method(sig: dict, method: str, *, timeout: float = 2.0, ev_threshold: float = 0.0):
    """Simulate one execution style on one signal. Returns dict with filled / entry /
    pnl / delay / taker_pnl / spread_captured, or filled=False (missed)."""
    side, up, m = sig["side"], sig["up"], sig["mid"]
    taker_entry = _taker_entry(sig)
    taker_pnl = _pnl(side, taker_entry, up)
    out = {"side": side, "taker_pnl": taker_pnl, "taker_entry": taker_entry,
           "regime": sig["regime"], "btc_vol": sig["btc_vol"], "volume_usd": sig["volume_usd"],
           "secs_to_expiry": sig["secs_to_expiry"], "mid": m, "filled": False,
           "entry": None, "pnl": 0.0, "delay": None, "spread_captured": 0.0}

    if method == "market":
        out.update(filled=True, entry=taker_entry, pnl=taker_pnl, delay=0.0,
                   spread_captured=round(m - taker_entry, 4))   # negative (paid spread)
        return out

    improve = 1 if method == "improve_bid" else 0
    entry, target, up_cross = _passive_entry(sig, improve_ticks=improve)

    # fair-value maker: only rest while EV at the bid exceeds threshold; here EV at the
    # bid = model_prob-vs-bid edge. If it doesn't clear the threshold, never post.
    if method == "fair_value_maker":
        bid_edge = (sig["model_prob"] - entry) if side == "YES" else ((1 - entry) - (1 - sig["model_prob"]))
        if bid_edge <= ev_threshold:
            return out                                          # no post → no fill (skipped)

    # adaptive passive: cap resting time to 1s (cancel stale quotes → less adverse fill)
    eff_timeout = min(timeout, 1.0) if method == "adaptive" else timeout
    delay = _first_cross(sig, target, up_cross=up_cross, timeout=eff_timeout)
    if delay is None:
        return out                                              # missed fill
    pnl = _pnl(side, entry, up)
    out.update(filled=True, entry=entry, pnl=pnl, delay=delay,
               spread_captured=round(m - entry if side == "YES" else (1 - m) - entry, 4))
    return out


# ---------------------------------------------------------------------------
# metric suite over a set of simulated trades
# ---------------------------------------------------------------------------
def _metrics(results: list[dict]) -> dict:
    n = len(results)
    filled = [r for r in results if r["filled"]]
    nf = len(filled)
    pnls = [r["pnl"] for r in filled]
    costs = [r["entry"] for r in filled]
    sig = significance(pnls, costs)
    # opportunity cost: profit on signals we FAILED to fill (would-be taker profit)
    missed = [r for r in results if not r["filled"]]
    missed_profit = sum(r["taker_pnl"] for r in missed if r["taker_pnl"] > 0)
    saved_loss = -sum(r["taker_pnl"] for r in missed if r["taker_pnl"] < 0)
    # drawdown + duration
    cum = peak = mdd = 0.0
    for r in filled:
        cum += r["pnl"]; peak = max(peak, cum); mdd = max(mdd, peak - cum)
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    return {
        "signals": n, "fills": nf, "fill_rate": round(nf / n, 4) if n else 0.0,
        "missed_fills": n - nf,
        "avg_fill_delay_s": round(_mean([r["delay"] for r in filled if r["delay"] is not None]), 3),
        "avg_fill_price": round(_mean(costs), 4) if costs else None,
        "avg_spread_captured": round(_mean([r["spread_captured"] for r in filled]), 4) if filled else 0.0,
        "win_rate": round(_mean([1 if p > 0 else 0 for p in pnls]), 4) if pnls else 0.0,
        "ev_after_cost": sig["ev_after_cost"], "roi": sig["roi"], "t_stat": sig["t_stat"],
        "ci": [sig["ci_low"], sig["ci_high"]], "sharpe": sig["sharpe"], "significant": sig["significant"],
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else (round(gross_win, 3) if gross_win else 0.0),
        "max_drawdown": round(mdd, 4),
        "opportunity_cost": {"missed_profitable_pnl": round(missed_profit, 4),
                             "saved_loss_pnl": round(saved_loss, 4),
                             "net_of_misses": round(saved_loss - missed_profit, 4)},
    }


def _breakdowns(results: list[dict]) -> dict:
    def by(keyfn):
        groups: dict = {}
        for r in results:
            groups.setdefault(keyfn(r), []).append(r)
        return {k: {"signals": len(v), "fill_rate": round(sum(1 for x in v if x["filled"]) / len(v), 3),
                    "ev_after_cost": _metrics(v)["ev_after_cost"], "significant": _metrics(v)["significant"]}
                for k, v in sorted(groups.items(), key=lambda kv: str(kv[0]))}
    def vol_bucket(r):
        v = r["btc_vol"]
        return "lo_vol" if v < 0.0004 else ("hi_vol" if v > 0.0009 else "mid_vol")
    def liq_bucket(r):
        v = r["volume_usd"]
        return "thin" if v < 100 else ("deep" if v > 400 else "mid")
    def age_bucket(r):
        a = r["secs_to_expiry"]
        return "late" if a < 120 else ("early" if a > 240 else "mid")
    def entry_bucket(r):
        m = r["mid"]
        return "low" if m < 0.4 else ("high" if m > 0.6 else "mid")
    return {"by_regime": by(lambda r: r["regime"]), "by_volatility": by(vol_bucket),
            "by_liquidity": by(liq_bucket), "by_market_age": by(age_bucket),
            "by_entry_price": by(entry_bucket)}


# ---------------------------------------------------------------------------
# fill-probability model: P(fill ≤ Δt | features)
# ---------------------------------------------------------------------------
def fill_probability_model(signals: list[dict]) -> dict:
    """Empirical fill rates by timeout (1s/2s/5s measured), an exponential hazard model
    for the sub-second points (250ms/500ms — MODELLED), and a logistic model of which
    features predict a fill within 5s."""
    if len(signals) < 20:
        return {"ok": False, "error": "too few signals"}
    # per-signal passive fill delay (None if no crossing within 5s)
    feats = ["half", "btc_vol", "volume_usd", "flow_imbalance", "secs_to_expiry", "btc_ret_sofar", "edge"]
    X, y5, sig_delays, delays = [], [], [], []
    for s in signals:
        entry, target, up_cross = _passive_entry(s)
        d = _first_cross(s, target, up_cross=up_cross, timeout=5.0)
        sig_delays.append(d)
        if d is not None:
            delays.append(d)
        X.append([_num(s.get(f)) for f in feats])
        y5.append(1 if d is not None else 0)
    emp = {str(to): round(_mean([1 if (d is not None and d <= to) else 0 for d in sig_delays]), 4)
           for to in EMPIRICAL_TIMEOUTS}
    # UNCONDITIONAL crossing-trade hazard λ = fills per signal-second over the 5s window
    # (NOT 1/mean-delay, which only describes the few orders that did fill and wildly
    # overstates fill probability when most orders never fill). This keeps the modelled
    # sub-second points consistent with the empirical 1s/2s/5s rates.
    lam = len(delays) / (len(signals) * 5.0) if signals else 0.0
    modelled = {str(to): round(1 - math.exp(-lam * to), 4) for to in TIMEOUTS}
    # which features predict a fill (logistic on fill-within-5s)
    importances = []
    if sum(y5) >= 5 and len(set(y5)) > 1:
        model = ml.LogisticRegression().fit(X, y5)
        imp = model.feature_importance()
        importances = sorted(zip(feats, imp), key=lambda kv: -kv[1])[:5]
    return {"ok": True, "empirical_fill_rate": emp,
            "modelled_fill_rate": modelled, "hazard_lambda_per_s": round(lam, 4),
            "overall_5s_fill_rate": round(_mean(y5), 4),
            "fill_predictors": [{"feature": f, "importance": round(i, 4)} for f, i in importances],
            "note": "1s/2s/5s empirical (1s-resolution trades); 250ms/500ms modelled via exponential hazard"}


# ---------------------------------------------------------------------------
# execution frontier + research orchestration
# ---------------------------------------------------------------------------
EXEC_POLICIES = [
    ("market", {}), ("join_bid", {"timeout": 2.0}), ("improve_bid", {"timeout": 2.0}),
    ("passive_1s", {"method": "join_bid", "timeout": 1.0}),
    ("passive_2s", {"method": "join_bid", "timeout": 2.0}),
    ("passive_5s", {"method": "join_bid", "timeout": 5.0}),
    ("adaptive", {"timeout": 2.0}),
    ("fair_value_maker", {"timeout": 5.0, "ev_threshold": 0.0}),
]


def _run_policy(signals: list[dict], policy: str, params: dict) -> list[dict]:
    method = params.get("method", policy if policy in ("market", "join_bid", "improve_bid",
                                                       "adaptive", "fair_value_maker") else "join_bid")
    timeout = params.get("timeout", 2.0)
    ev_threshold = params.get("ev_threshold", 0.0)
    return [simulate_method(s, method, timeout=timeout, ev_threshold=ev_threshold) for s in signals]


def execution_frontier(signals: list[dict]) -> dict:
    rows = []
    for policy, params in EXEC_POLICIES:
        res = _run_policy(signals, policy, params)
        m = _metrics(res)
        rows.append({"policy": policy, "fill_rate": m["fill_rate"], "fills": m["fills"],
                     "avg_fill_price": m["avg_fill_price"], "avg_spread_captured": m["avg_spread_captured"],
                     "ev_after_cost": m["ev_after_cost"], "roi": m["roi"], "t_stat": m["t_stat"],
                     "sharpe": m["sharpe"], "significant": m["significant"],
                     "profit_factor": m["profit_factor"], "max_drawdown": m["max_drawdown"]})
    rows.sort(key=lambda r: (-1 if r["significant"] else 0, -r["ev_after_cost"]))
    best = rows[0] if rows else None
    return {"frontier": rows, "best_policy": best}


# ---------------------------------------------------------------------------
# promotion experiment — re-gate models under the best passive execution
# ---------------------------------------------------------------------------
def _models_to_test(db: Session) -> list[dict]:
    """The Alpha Lab models to re-evaluate (no retraining). fair-value on ALL_FEATURES,
    plus a model on the surviving mined features if available."""
    out = []
    trX, trY, _ = ph1.feature_matrix(lab._point_dicts(db, "train"), ph1.ALL_FEATURES)
    if len(trX) >= 20:
        out.append({"name": "fair_value", "feats": ph1.ALL_FEATURES,
                    "model": ml.LogisticRegression().fit(trX, trY)})
    mined = db.scalars(select(lm.Btc5mAlphaModelGen).order_by(lm.Btc5mAlphaModelGen.generation.desc())).first()
    if mined and mined.feature_set:
        feats = [f for f in mined.feature_set if f in ph1.ALL_FEATURES]   # only directly-computable feats
        if len(feats) >= 3:
            mX, mY, _ = ph1.feature_matrix(lab._point_dicts(db, "train"), feats)
            if len(mX) >= 20:
                out.append({"name": f"mined_gen{mined.generation}", "feats": feats,
                            "model": ml.LogisticRegression().fit(mX, mY)})
    return out


def _gate(metrics: dict, regime_stability: float) -> tuple[str, str]:
    """The SAME promotion gate as Alpha Discovery, applied to execution-simulated PnL."""
    g = {"significant": metrics["significant"], "ev_after_cost": metrics["ev_after_cost"],
         "n_trades": metrics["fills"], "regime_stability": regime_stability, "decay": 0.0}
    return disc._promotion_decision(g)


def _regime_stability(results: list[dict]) -> float:
    filled = [r for r in results if r["filled"]]
    if not filled:
        return 0.0
    regs: dict = {}
    for r in filled:
        regs.setdefault(r["regime"], []).append(r["pnl"])
    if not regs:
        return 0.0
    return round(_mean([1.0 if _mean(v) >= 0 else 0.0 for v in regs.values()]), 3)


def promotion_experiment(db: Session, best_policy: str, best_params: dict) -> dict:
    """Re-run each model under MARKET vs the BEST passive execution, applying the exact
    same promotion gates. Reports whether execution alone flips any model rejected →
    candidate → paper. No retraining — only the execution changes."""
    results = []
    flips = 0
    for spec in _models_to_test(db):
        sigs = build_signals(db, "holdout", spec["model"], spec["feats"])
        if len(sigs) < MIN_TRADES:
            results.append({"model": spec["name"], "skipped": "too few signals", "n_signals": len(sigs)})
            continue
        mkt = _run_policy(sigs, "market", {})
        mkt_m = _metrics(mkt)
        mkt_state, _ = _gate(mkt_m, _regime_stability(mkt))
        pas = _run_policy(sigs, best_policy, best_params)
        pas_m = _metrics(pas)
        pas_state, pas_reason = _gate(pas_m, _regime_stability(pas))
        flipped = (mkt_state != "paper" and pas_state == "paper")
        flips += 1 if flipped else 0
        results.append({
            "model": spec["name"], "n_signals": len(sigs),
            "market": {"state": mkt_state, "ev": mkt_m["ev_after_cost"], "t": mkt_m["t_stat"],
                       "fills": mkt_m["fills"], "fill_rate": mkt_m["fill_rate"]},
            "passive": {"state": pas_state, "ev": pas_m["ev_after_cost"], "t": pas_m["t_stat"],
                        "fills": pas_m["fills"], "fill_rate": pas_m["fill_rate"], "reason": pas_reason},
            "flipped_to_paper": flipped,
        })
    return {"best_policy": best_policy, "models_tested": len(results),
            "models_flipped_to_paper": flips, "results": results}


# ---------------------------------------------------------------------------
# answers to the 7 research questions
# ---------------------------------------------------------------------------
def _answers(frontier: dict, promo: dict, fillm: dict, breakdown: dict) -> list[dict]:
    rows = frontier["frontier"]
    mkt = next((r for r in rows if r["policy"] == "market"), {})
    passives = [r for r in rows if r["policy"] != "market"]
    best = frontier["best_policy"] or {}
    best_passive = max((p for p in passives), key=lambda r: r["ev_after_cost"], default={})
    # which timeout maximizes EV?
    timed = [r for r in rows if r["policy"].startswith("passive_")]
    best_to = max(timed, key=lambda r: r["ev_after_cost"], default={}) if timed else {}
    # regime where passive dominates
    dom_regime = None
    for rg, d in (breakdown.get("by_regime") or {}).items():
        if d.get("significant"):
            dom_regime = rg
            break
    return [
        {"q": "Is passive execution statistically superior to market execution?",
         "a": ("yes" if best.get("policy") != "market" and best.get("significant") and not mkt.get("significant")
               else ("partially" if best_passive.get("ev_after_cost", -9) > mkt.get("ev_after_cost", 0) else "no")),
         "detail": f"best={best.get('policy')} EV {best.get('ev_after_cost')} (sig={best.get('significant')}) "
                   f"vs market EV {mkt.get('ev_after_cost')} (sig={mkt.get('significant')})"},
        {"q": "At what timeout is EV maximized?",
         "a": best_to.get("policy", "n/a"), "detail": f"EV {best_to.get('ev_after_cost')} at {best_to.get('policy')}"},
        {"q": "How much spread can realistically be captured?",
         "a": f"{best_passive.get('avg_spread_captured')}", "detail": f"avg captured by {best_passive.get('policy')}"},
        {"q": "Is there a regime where passive execution dominates?",
         "a": dom_regime or "none", "detail": "regime with significant passive EV" if dom_regime else "no regime reached significance"},
        {"q": "Does passive execution convert rejected models into significant +EV?",
         "a": "yes" if promo["models_flipped_to_paper"] > 0 else "no",
         "detail": f"{promo['models_flipped_to_paper']}/{promo['models_tested']} models flipped to paper"},
        {"q": "Does lower fill rate outweigh execution improvement?",
         "a": ("yes (fills too rarely)" if best_passive.get("fill_rate", 0) < 0.15 and not best.get("significant")
               else "no"),
         "detail": f"best passive fill rate {best_passive.get('fill_rate')}, 5s fill {fillm.get('overall_5s_fill_rate')}"},
        {"q": "If passive were default, how many models pass promotion gates?",
         "a": f"{promo['models_flipped_to_paper']} of {promo['models_tested']}",
         "detail": "same statistical gates, execution-only change"},
    ]


# ---------------------------------------------------------------------------
# orchestration + report
# ---------------------------------------------------------------------------
def run_execution_lab(db: Session) -> dict:
    """Full execution-research run: signals → frontier → fill model → breakdowns →
    promotion experiment → answers → verdict. Paper/research only."""
    spec = next(iter(_models_to_test(db)), None)
    if spec is None:
        rep = {"ok": False, "error": "no model / dataset too small",
               "headline": "execution lab: dataset too small", "safety": _safety()}
        _store(db, rep)
        return rep
    # signals across all splits for descriptive stats; holdout drives the headline EV
    sig_all = []
    for s in ("train", "val", "holdout"):
        sig_all += build_signals(db, s, spec["model"], spec["feats"])
    sig_hold = build_signals(db, "holdout", spec["model"], spec["feats"])
    if len(sig_hold) < MIN_TRADES:
        rep = {"ok": False, "error": "too few holdout signals", "n_holdout_signals": len(sig_hold),
               "headline": "execution lab: too few signals to evaluate", "safety": _safety()}
        _store(db, rep)
        return rep
    frontier = execution_frontier(sig_hold)
    fillm = fill_probability_model(sig_all)
    # breakdowns under the best passive policy
    best = frontier["best_policy"]
    best_policy = best["policy"] if best else "passive_2s"
    best_params = dict(next((p for n, p in EXEC_POLICIES if n == best_policy), {"timeout": 2.0}))
    breakdown = _breakdowns(_run_policy(sig_hold, best_policy, best_params))
    promo = promotion_experiment(db, best_policy, best_params)
    answers = _answers(frontier, promo, fillm, breakdown)

    mkt = next((r for r in frontier["frontier"] if r["policy"] == "market"), {})
    improved = best and best["policy"] != "market" and best["ev_after_cost"] > mkt.get("ev_after_cost", -9)
    if promo["models_flipped_to_paper"] > 0:
        code, verdict = 1, "execution creates a tradeable edge"
        headline = (f"passive execution ({best_policy}) flips {promo['models_flipped_to_paper']}/"
                    f"{promo['models_tested']} model(s) to PAPER — EV {best['ev_after_cost']} (t={best['t_stat']}) "
                    f"vs market {mkt.get('ev_after_cost')}; spread captured {best.get('avg_spread_captured')}")
    elif improved and best["ev_after_cost"] > 0:
        code, verdict = 2, "execution helps but not enough"
        headline = (f"passive ({best_policy}) lifts EV to {best['ev_after_cost']} (from market "
                    f"{mkt.get('ev_after_cost')}) but not to significance — fill rate "
                    f"{best['fill_rate']} (5s {fillm.get('overall_5s_fill_rate')})")
    else:
        code, verdict = 3, "execution is not the bottleneck"
        headline = (f"passive execution does not materially beat market here — best {best_policy} EV "
                    f"{best['ev_after_cost'] if best else None}; fills too rarely "
                    f"({fillm.get('overall_5s_fill_rate')} in 5s) to overcome the edge after costs")

    rep = {
        "ok": True, "verdict_code": code, "verdict": verdict, "headline": headline,
        "generated_at": datetime.utcnow().isoformat(),
        "n_signals": {"all": len(sig_all), "holdout": len(sig_hold)},
        "execution_frontier": frontier["frontier"], "best_policy": frontier["best_policy"],
        "fill_probability": fillm, "breakdowns": breakdown,
        "promotion_experiment": promo, "research_answers": answers,
        "approximations": [
            "fills decided from the 1s-resolution historical TRADE stream (captures adverse selection)",
            "250ms/500ms fill rates are MODELLED (exponential hazard), not measured",
            "adaptive = passive capped to 1s resting; fair_value_maker = post only while bid-edge > threshold",
            "no stored L2 book ⇒ book-change repricing / quote replenishment not simulated",
        ],
        "safety": _safety(),
    }
    _store(db, rep)
    return rep


def _safety() -> str:
    return ("BTC 5M Execution Research Lab — research/paper only; simulates execution on "
            "historical data; never places orders or touches live execution / bankroll / copy trading")


def _store(db: Session, rep: dict) -> None:
    st = _state(db)
    st.execution = rep
    st.execution_built_at = datetime.utcnow()
    db.commit()


def execution_status(db: Session) -> dict:
    st = _state(db)
    return {"execution": st.execution,
            "execution_built_at": st.execution_built_at.isoformat() if st.execution_built_at else None,
            "safety": _safety()}
