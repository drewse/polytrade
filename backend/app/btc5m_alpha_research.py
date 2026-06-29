"""BTC 5M Alpha Research Platform — research/paper ONLY.

A self-improving quantitative research environment layered on the Strategy Lab's
synchronized 1-second dataset. The objective is NOT to copy wallets or to find the
fastest copy-trader: it is to estimate the TRUE probability that a market resolves
YES (fair value), measure where that estimate disagrees with the market price, and
promote ONLY signals whose expected value is statistically significant AFTER
realistic spread, slippage and latency.

Subsystems (all read-only w.r.t. production; never place orders / touch live
execution / sizing / bankroll / copy ranking):

  * Fair-Value models   — calibrated P(YES) per decision point + EV-after-cost gate
  * Feature discovery    — auto-generate/score/prune candidate features
  * Ensemble             — perspective models (price/flow/vol/liquidity/structure/
                           wallet) combined by calibration reliability, not equally
  * Microstructure       — how the market behaves (spread, impact, clustering, speed)
  * Cross-market         — BTC 5m vs 15m information flow + BTC-spot lead
  * Evolutionary search  — mutate rule strategies under strict OOS validation
  * Nightly pipeline     — rebuild → features → models → backtest → decay → report

Wallet activity is just ANOTHER FEATURE here (`wallet_signal`): profitable wallets
are treated as labels for what experienced traders chose under a market state, not
as copy targets.
"""
from __future__ import annotations

import math
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import btc5m_ml as ml
from . import btc5m_models as bm
from . import btc5m_strategy_lab as lab
from . import btc5m_strategy_models as lm

_mean = lab._mean
_std = lab._std
_corr = lab._corr
_clip = lab._clip
_state = lab._state

DEFAULT_SLIPPAGE = 0.01
SIG_T = 1.96                 # ~95% two-sided significance on per-trade EV
MIN_TRADES = 8               # minimum holdout decisions to consider a signal real


# ---------------------------------------------------------------------------
# feature perspectives (used by fair-value + ensemble)
# ---------------------------------------------------------------------------
FEATURE_GROUPS: dict[str, list[str]] = {
    "price_action": ["btc_ret_1s", "btc_ret_2s", "btc_ret_3s", "btc_ret_5s", "btc_ret_10s",
                     "btc_ret_20s", "btc_ret_30s", "btc_ret_60s", "btc_ret_sofar",
                     "btc_momentum", "btc_acceleration", "btc_candle", "btc_breakout"],
    "order_flow": ["flow_imbalance", "recent_flow_imbalance", "volume_usd", "trade_freq",
                   "has_large_trade", "large_trade_usd"],
    "volatility": ["btc_vol", "btc_acceleration", "btc_momentum"],
    "liquidity": ["spread", "volume_usd", "trade_freq"],
    "market_structure": ["t_offset_s", "secs_to_expiry", "pm_yes", "pm_momentum", "lag"],
    "wallet_behavior": ["wallet_signal", "wallet_recent_signal", "wallet_trade_count", "flow_imbalance"],
}
ALL_FEATURES: list[str] = sorted({f for g in FEATURE_GROUPS.values() for f in g})


# ---------------------------------------------------------------------------
# dataset → matrix
# ---------------------------------------------------------------------------
def _num(v) -> float:
    try:
        if v is None or (isinstance(v, bool)):
            return 1.0 if v is True else 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def feature_matrix(points: list[dict], feats: list[str]):
    """(X, y, used) over decision points that have a market price + label."""
    X, y, used = [], [], []
    for p in points:
        if p.get("pm_yes") is None or p.get("label_up") is None:
            continue
        X.append([_num(p.get(f)) for f in feats])
        y.append(1 if p["label_up"] else 0)
        used.append(p)
    return X, y, used


# ---------------------------------------------------------------------------
# calibration metrics
# ---------------------------------------------------------------------------
def brier_score(probs: list[float], ys: list[int]) -> float:
    if not probs:
        return 0.25
    return round(_mean([(p - y) ** 2 for p, y in zip(probs, ys)]), 5)


def reliability_curve(probs: list[float], ys: list[int], bins: int = 5) -> list[dict]:
    out = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        seg = [(p, y) for p, y in zip(probs, ys) if (lo <= p < hi or (b == bins - 1 and p == 1.0))]
        if not seg:
            continue
        out.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(seg),
                    "predicted": round(_mean([p for p, _ in seg]), 4),
                    "actual": round(_mean([y for _, y in seg]), 4)})
    return out


def expected_calibration_error(curve: list[dict], total: int) -> float:
    if not total:
        return 0.0
    return round(sum(c["n"] * abs(c["predicted"] - c["actual"]) for c in curve) / total, 4)


def calibration_score(brier: float) -> float:
    """1 - Brier/0.25 — skill vs an always-0.5 forecaster (0.25 is its Brier). >0 = skill."""
    return round(1 - brier / 0.25, 4)


def auc_score(probs: list[float], ys: list[int]) -> float:
    """Mann–Whitney rank AUC. 0.5 = no discrimination."""
    pos = [p for p, y in zip(probs, ys) if y == 1]
    neg = [p for p, y in zip(probs, ys) if y == 0]
    if not pos or not neg:
        return 0.5
    wins = ties = 0
    for a in pos:
        for b in neg:
            if a > b:
                wins += 1
            elif a == b:
                ties += 1
    return round((wins + 0.5 * ties) / (len(pos) * len(neg)), 4)


# ---------------------------------------------------------------------------
# EV after realistic costs + statistical significance (the promotion gate)
# ---------------------------------------------------------------------------
def ev_after_costs(probs: list[float], points: list[dict], *, slippage: float = DEFAULT_SLIPPAGE) -> dict:
    """For each decision point, compare the model's P(YES) to the market price and
    bet the side the model favors — but ONLY when the model's edge exceeds the
    realistic round-trip cost (half-spread + slippage). Returns per-trade realized
    PnL stats with a confidence interval + t-stat so we can demand statistical
    significance, not just a positive average."""
    profits, costs, edges, gaps, wins = [], [], [], [], []
    for prob, p in zip(probs, points):
        m = p.get("pm_yes")
        if m is None or p.get("label_up") is None:
            continue
        spread = p.get("spread") or 0.0
        cost = spread / 2 + slippage
        edge = abs(prob - m)
        gaps.append(prob - m)
        if edge <= cost:
            continue                                   # no EV after costs → don't trade
        side = "YES" if prob > m else "NO"
        profit, pe, win = lab._trade_pnl(side, m, spread, p["label_up"], slippage)
        profits.append(profit); costs.append(pe); edges.append(edge - cost)
        wins.append(1 if win else 0)
    n = len(profits)
    mean = _mean(profits)
    sd = _std(profits)
    se = sd / math.sqrt(n) if n > 1 else 0.0
    if se:
        t = mean / se
    else:                                              # zero variance: a perfectly
        t = 99.0 if mean > 0 else (-99.0 if mean < 0 else 0.0)  # consistent signal
    roi = sum(profits) / sum(costs) if costs and sum(costs) else 0.0
    significant = bool(n >= MIN_TRADES and mean > 0 and t >= SIG_T and n >= 2)
    return {
        "n_trades": n,
        "ev_after_cost": round(mean, 5),
        "ev_after_spread": round(_mean(edges), 5),     # mean post-cost model edge
        "roi": round(roi, 4),
        "win_rate": round(_mean(wins), 4) if n else 0.0,
        "t_stat": round(t, 3),
        "ci_low": round(mean - SIG_T * se, 5),
        "ci_high": round(mean + SIG_T * se, 5),
        "avg_gap": round(_mean(gaps), 5),              # avg (model − market) over ALL points
        "significant": significant,
    }


# ---------------------------------------------------------------------------
# fair-value probability model
# ---------------------------------------------------------------------------
def _fit(algo: str, X, y):
    factory = ml.MODEL_FACTORIES.get(algo, ml.MODEL_FACTORIES["logistic_regression"])
    return factory().fit(X, y)


def fair_value(db: Session, *, algo: str = "logistic_regression", feats: list[str] | None = None,
               slippage: float = DEFAULT_SLIPPAGE) -> dict:
    """Train a calibrated P(YES) model on train, calibrate on validation, and judge
    its tradeability on the untouched holdout via EV-after-cost significance."""
    feats = feats or ALL_FEATURES
    trX, trY, _ = feature_matrix(lab._point_dicts(db, "train"), feats)
    vaX, vaY, vaP = feature_matrix(lab._point_dicts(db, "val"), feats)
    hoX, hoY, hoP = feature_matrix(lab._point_dicts(db, "holdout"), feats)
    if len(trX) < 20 or len(hoX) < MIN_TRADES:
        return {"ok": False, "error": "dataset too small", "n_train": len(trX), "n_holdout": len(hoX)}
    model = _fit(algo, trX, trY)
    va_probs = model.predict_proba(vaX) if vaX else []
    ho_probs = model.predict_proba(hoX)
    va_curve = reliability_curve(va_probs, vaY) if va_probs else []
    ho_curve = reliability_curve(ho_probs, hoY)
    brier = brier_score(ho_probs, hoY)
    ev = ev_after_costs(ho_probs, hoP, slippage=slippage)
    # most-informative features (|importance|)
    imp = model.feature_importance() if hasattr(model, "feature_importance") else []
    top = sorted(zip(feats, imp), key=lambda kv: -kv[1])[:8] if imp else []
    sample = []
    for prob, p in list(zip(ho_probs, hoP))[:14]:
        m = p["pm_yes"]
        sample.append({"market_implied": round(m, 4), "model_prob": round(prob, 4),
                       "gap": round(prob - m, 4), "ev_after_spread": round(abs(prob - m) - (p.get("spread") or 0) / 2, 4),
                       "ev_after_slippage": round(abs(prob - m) - (p.get("spread") or 0) / 2 - slippage, 4),
                       "resolved_up": bool(p["label_up"])})
    return {
        "ok": True, "algo": algo, "n_features": len(feats),
        "n_train": len(trX), "n_val": len(vaX), "n_holdout": len(hoX),
        "brier": brier, "calibration_score": calibration_score(brier),
        "ece": expected_calibration_error(ho_curve, len(hoY)),
        "auc": auc_score(ho_probs, hoY),
        "val_brier": brier_score(va_probs, vaY) if va_probs else None,
        "reliability": ho_curve, "val_reliability": va_curve,
        "ev": ev, "top_features": [{"feature": f, "importance": round(i, 4)} for f, i in top],
        "sample": sample,
    }


# ---------------------------------------------------------------------------
# ensemble of perspective models (combined by calibration reliability)
# ---------------------------------------------------------------------------
def ensemble(db: Session, *, algo: str = "logistic_regression", slippage: float = DEFAULT_SLIPPAGE) -> dict:
    """Train one model per perspective (price/flow/vol/liquidity/structure/wallet),
    score each on validation Brier, then combine on holdout weighted by reliability
    (inverse Brier) — NOT equal weighting. Report each model + the ensemble's EV."""
    trP, vaP, hoP = (lab._point_dicts(db, s) for s in ("train", "val", "holdout"))
    if len([p for p in trP if p.get("pm_yes") is not None]) < 20:
        return {"ok": False, "error": "dataset too small"}
    members = []
    ho_matrix = []        # per-model holdout probs aligned to ho_used
    ho_used = None
    for group, feats in FEATURE_GROUPS.items():
        trX, trY, _ = feature_matrix(trP, feats)
        vaX, vaY, _ = feature_matrix(vaP, feats)
        hoX, hoY, used = feature_matrix(hoP, feats)
        if len(trX) < 20 or not hoX:
            continue
        model = _fit(algo, trX, trY)
        va_probs = model.predict_proba(vaX) if vaX else []
        ho_probs = model.predict_proba(hoX)
        vb = brier_score(va_probs, vaY) if va_probs else 0.25
        weight = 1.0 / max(vb, 1e-3)
        ev = ev_after_costs(ho_probs, used, slippage=slippage)
        members.append({"perspective": group, "val_brier": vb, "weight": weight,
                        "holdout_brier": brier_score(ho_probs, hoY), "auc": auc_score(ho_probs, hoY),
                        "ev": ev})
        ho_used = used if ho_used is None else ho_used
        ho_matrix.append((weight, ho_probs, used))
    if not members:
        return {"ok": False, "error": "no perspective had enough data"}
    # reliability-weighted ensemble on the common holdout index space
    base = ho_matrix[0][2]
    key = lambda pt: (pt.get("market_id"), pt.get("t_offset_s"), round(pt.get("pm_yes") or 0, 4))
    idx = {key(pt): i for i, pt in enumerate(base)}
    ens_probs, ens_pts = [], []
    for i, pt in enumerate(base):
        num = den = 0.0
        ok = True
        for w, probs, used in ho_matrix:
            j = idx.get(key(pt))
            if j is None or j >= len(probs):
                ok = False
                break
            num += w * probs[j]; den += w
        if ok and den:
            ens_probs.append(num / den); ens_pts.append(pt)
    ens_y = [1 if p["label_up"] else 0 for p in ens_pts]
    eb = brier_score(ens_probs, ens_y)
    ens_ev = ev_after_costs(ens_probs, ens_pts, slippage=slippage)
    total_w = sum(m["weight"] for m in members) or 1.0
    for m in members:
        m["weight"] = round(m["weight"] / total_w, 4)
        m["val_brier"] = round(m["val_brier"], 5)
    members.sort(key=lambda m: -m["weight"])
    return {
        "ok": True, "algo": algo, "members": members,
        "ensemble": {"brier": eb, "calibration_score": calibration_score(eb),
                     "auc": auc_score(ens_probs, ens_y), "n": len(ens_probs),
                     "reliability": reliability_curve(ens_probs, ens_y), "ev": ens_ev},
    }


# ---------------------------------------------------------------------------
# automated feature discovery
# ---------------------------------------------------------------------------
_BASE_FEATS = ["btc_ret_3s", "btc_ret_5s", "btc_ret_10s", "btc_ret_30s", "btc_ret_sofar",
               "btc_momentum", "btc_acceleration", "btc_vol", "flow_imbalance",
               "recent_flow_imbalance", "pm_momentum", "lag", "wallet_signal", "trade_freq"]


def _candidates() -> dict:
    """Generate candidate-feature extractors: nonlinear transforms, interactions,
    ratios and regime-conditioned variants of the base features."""
    cand: dict[str, callable] = {}
    for f in _BASE_FEATS:
        cand[f"sq[{f}]"] = (lambda p, f=f: _num(p.get(f)) ** 2)
        cand[f"abs[{f}]"] = (lambda p, f=f: abs(_num(p.get(f))))
        cand[f"sign[{f}]"] = (lambda p, f=f: (1.0 if _num(p.get(f)) > 0 else (-1.0 if _num(p.get(f)) < 0 else 0.0)))
    pairs = [("btc_ret_3s", "flow_imbalance"), ("btc_ret_5s", "wallet_signal"),
             ("btc_momentum", "btc_vol"), ("btc_ret_sofar", "pm_momentum"),
             ("recent_flow_imbalance", "btc_ret_5s"), ("lag", "btc_ret_10s"),
             ("wallet_signal", "flow_imbalance"), ("btc_ret_10s", "trade_freq")]
    for a, b in pairs:
        cand[f"{a}*{b}"] = (lambda p, a=a, b=b: _num(p.get(a)) * _num(p.get(b)))
    # vol-normalized momentum / flow (a classic z-score style signal)
    cand["btc_ret_5s/vol"] = (lambda p: _num(p.get("btc_ret_5s")) / (_num(p.get("btc_vol")) + 1e-4))
    cand["btc_mom/vol"] = (lambda p: _num(p.get("btc_momentum")) / (_num(p.get("btc_vol")) + 1e-4))
    cand["flow*btc"] = (lambda p: _num(p.get("flow_imbalance")) * _num(p.get("btc_ret_sofar")))
    # regime-conditioned
    for f in ("btc_ret_5s", "flow_imbalance", "wallet_signal"):
        cand[f"{f}@highvol"] = (lambda p, f=f: _num(p.get(f)) if p.get("regime") == "high_vol" else 0.0)
        cand[f"{f}@trend"] = (lambda p, f=f: _num(p.get(f)) if p.get("regime") == "strong_trend" else 0.0)
    return cand


def discover_features(db: Session, *, top: int = 15, min_abs_corr: float = 0.08) -> dict:
    """Generate thousands-of-combos-style candidates, score by |corr| with the
    label on TRAIN, keep only those STABLE on validation (same sign, still
    predictive), then prune redundant features (|inter-corr| > 0.9 → drop the
    weaker). Promotes stable predictive signals; eliminates redundancy."""
    tr = [p for p in lab._point_dicts(db, "train") if p.get("label_up") is not None]
    va = [p for p in lab._point_dicts(db, "val") if p.get("label_up") is not None]
    if len(tr) < 20:
        return {"ok": False, "error": "dataset too small", "generated": 0}
    cand = _candidates()
    tr_y = [1 if p["label_up"] else 0 for p in tr]
    va_y = [1 if p["label_up"] else 0 for p in va]
    scored = []
    tr_vals: dict[str, list] = {}
    for name, fn in cand.items():
        xv = [fn(p) for p in tr]
        if len(set(xv)) < 2:
            continue
        ct = _corr(xv, tr_y)
        cv = _corr([fn(p) for p in va], va_y) if va else 0.0
        stable = abs(ct) >= min_abs_corr and (ct > 0) == (cv > 0) and abs(cv) >= min_abs_corr * 0.5
        scored.append({"feature": name, "train_corr": ct, "val_corr": cv,
                       "abs_corr": abs(ct), "stable": stable})
        tr_vals[name] = xv
    scored.sort(key=lambda s: -s["abs_corr"])
    # redundancy pruning over the stable set
    promoted, eliminated = [], []
    for s in [x for x in scored if x["stable"]]:
        redundant_of = None
        for kept in promoted:
            if abs(_corr(tr_vals[s["feature"]], tr_vals[kept["feature"]])) > 0.9:
                redundant_of = kept["feature"]
                break
        if redundant_of:
            eliminated.append({**s, "redundant_with": redundant_of})
        else:
            promoted.append(s)
        if len(promoted) >= top:
            break
    return {
        "ok": True, "generated": len(cand), "evaluated": len(scored),
        "n_stable": sum(1 for s in scored if s["stable"]),
        "promoted": [{"feature": p["feature"], "train_corr": round(p["train_corr"], 4),
                      "val_corr": round(p["val_corr"], 4)} for p in promoted],
        "eliminated_redundant": [{"feature": e["feature"], "redundant_with": e["redundant_with"]}
                                 for e in eliminated[:10]],
    }


# ---------------------------------------------------------------------------
# market microstructure (how the market behaves, not only how it resolves)
# ---------------------------------------------------------------------------
def microstructure(db: Session) -> dict:
    """Study behavior: spread regime, large-trade price impact, trade clustering,
    and price-discovery speed (from the 1s BTC→YES lag profile). Order-book depth /
    liquidity add-remove events need book snapshots we don't store — flagged."""
    pts = [p for p in lab._point_dicts(db) if p.get("pm_yes") is not None]
    if not pts:
        return {"ok": False, "error": "no points"}
    spreads = [p.get("spread") or 0.0 for p in pts]
    freqs = [p.get("trade_freq") or 0.0 for p in pts]
    vols = [p.get("volume_usd") or 0.0 for p in pts]
    big = [p for p in pts if p.get("has_large_trade")]
    # large-trade price impact: |pm_momentum| right after a large trade vs baseline
    impact_big = _mean([abs(p.get("pm_momentum", 0.0)) for p in big])
    impact_base = _mean([abs(p.get("pm_momentum", 0.0)) for p in pts if not p.get("has_large_trade")])
    # trade clustering: dispersion of trade frequency (Fano-like: var/mean)
    clustering = (_std(freqs) ** 2 / _mean(freqs)) if _mean(freqs) else 0.0
    st = _state(db)
    prof = {int(k): v for k, v in (st.lag_profile or {}).items()}
    # price-discovery speed: lag at which cross-corr reaches ~90% of its peak
    disc_speed = None
    if prof:
        peak = max(prof.values())
        if peak > 0:
            disc_speed = min((k for k in sorted(prof) if prof[k] >= 0.9 * peak), default=None)
    return {
        "ok": True, "n": len(pts),
        "spread": {"mean": round(_mean(spreads), 4), "p90": round(sorted(spreads)[int(len(spreads) * 0.9)], 4),
                   "expansion_ratio": round((sorted(spreads)[int(len(spreads) * 0.9)] / _mean(spreads)), 2) if _mean(spreads) else 0.0},
        "large_trade_impact": {"n_large": len(big), "impact_after_large": round(impact_big, 4),
                               "baseline_impact": round(impact_base, 4),
                               "impact_ratio": round(impact_big / impact_base, 2) if impact_base else 0.0},
        "trade_clustering_index": round(clustering, 3),
        "avg_volume_usd": round(_mean(vols), 2),
        "price_discovery_speed_s": disc_speed,
        "interpretation": "impact_ratio>>1 ⇒ large trades move price (information); discovery_speed_s ⇒ "
                          "how fast YES absorbs a BTC move; clustering>1 ⇒ bursty (liquidity vacuums)",
        "unavailable": ["order_book_depth", "liquidity_add_remove", "market_maker_quotes",
                        "(need live L2 book snapshots — not stored)"],
    }


# ---------------------------------------------------------------------------
# cross-market intelligence
# ---------------------------------------------------------------------------
def cross_market(db: Session) -> dict:
    """Information flow between related markets. We have BTC 5m vs 15m + the BTC
    spot→Polymarket lead (1s lag profile). ETH/futures/funding/OI/liquidations and
    other Polymarket contracts need external feeds — listed as scoped-next."""
    pts = [p for p in lab._point_dicts(db) if p.get("pm_yes") is not None and p.get("label_up") is not None]
    by_dur = {}
    for dur in sorted({p.get("duration_minutes") for p in pts if p.get("duration_minutes")}):
        seg = [p for p in pts if p.get("duration_minutes") == dur]
        y = [1 if p["label_up"] else 0 for p in seg]
        # how efficiently is each horizon priced? corr(market price, outcome) = price informativeness
        price_info = _corr([p["pm_yes"] for p in seg], y)
        btc_info = _corr([p.get("btc_ret_sofar", 0.0) for p in seg], y)
        by_dur[str(dur)] = {"n": len(seg), "price_informativeness": price_info,
                            "btc_move_informativeness": btc_info}
    st = _state(db)
    prof = {int(k): v for k, v in (st.lag_profile or {}).items()}
    peak_lag = max(prof, key=lambda k: prof[k]) if prof else None
    return {
        "ok": True, "by_duration": by_dur,
        "btc_spot_lead": {"peak_lag_s": peak_lag, "peak_corr": round(prof.get(peak_lag, 0), 4) if peak_lag is not None else None,
                          "leads": bool(peak_lag and peak_lag > 0 and prof.get(peak_lag, 0) >= 0.05)},
        "interpretation": "compare price_informativeness across 5m vs 15m: a horizon whose price tracks the "
                          "outcome worse is less efficient (more researchable edge)",
        "scoped_next": ["ETH prediction markets", "BTC futures basis", "funding rates", "open interest",
                        "liquidations", "hourly BTC markets", "related Polymarket contracts"],
    }


# ---------------------------------------------------------------------------
# evolutionary strategy search (mutation under strict OOS validation)
# ---------------------------------------------------------------------------
def _mutate(prm: dict, family: str, gen: int) -> dict:
    """Deterministic neighbor of a param set (no RNG — vary by generation/value so
    resume/replay is stable). Nudges numeric params ±1 step."""
    grid = {}
    for combo in lab._grid(family):
        for k, v in combo.items():
            grid.setdefault(k, [])
            if v not in grid[k]:
                grid[k].append(v)
    out = dict(prm)
    keys = [k for k in prm if isinstance(prm[k], (int, float)) and not isinstance(prm[k], bool) and len(grid.get(k, [])) > 1]
    if not keys:
        return out
    k = keys[gen % len(keys)]
    opts = sorted(grid[k])
    try:
        i = opts.index(prm[k])
    except ValueError:
        i = 0
    out[k] = opts[(i + (1 if gen % 2 == 0 else -1)) % len(opts)]
    return out


def evolve(db: Session, *, families=lab.STRATEGY_FAMILIES, generations: int = 4,
           survivors: int = 6, slippage: float = DEFAULT_SLIPPAGE) -> dict:
    """Seed from the best grid strategies, then mutate the survivors across
    generations, re-validating on train/val/holdout each round and discarding any
    candidate that overfits, fails after costs, or has too few holdout trades."""
    train = lab._point_dicts(db, "train")
    val = lab._point_dicts(db, "val")
    hold = lab._point_dicts(db, "holdout")
    if not (train and hold):
        return {"ok": False, "error": "dataset too small"}

    def score(fam, prm):
        tr = lab.backtest(train, fam, prm, slippage=slippage)
        if tr["trades"] < 10 or tr["roi"] <= 0:
            return None
        va = lab.backtest(val, fam, prm, slippage=slippage)
        ho = lab.backtest(hold, fam, prm, slippage=slippage)
        if ho["trades"] < MIN_TRADES or va["roi"] <= 0 or ho["roi"] <= 0:
            return None
        if (tr["roi"] - ho["roi"]) > 0.15:
            return None
        return {"family": fam, "params": prm, "score": lab._robust_score(tr, va, ho),
                "holdout": ho, "train": tr, "val": va}

    population, evaluated = [], 0
    for fam in families:
        seeds = lab._grid(fam)
        scored = []
        for prm in seeds:
            evaluated += 1
            r = score(fam, prm)
            if r:
                scored.append(r)
        scored.sort(key=lambda r: -r["score"])
        population += scored[:max(1, survivors // 2)]
    population.sort(key=lambda r: -r["score"])
    population = population[:survivors]
    best_by_gen = [{"gen": 0, "best_score": round(population[0]["score"], 2) if population else None,
                    "survivors": len(population)}]
    for gen in range(1, generations + 1):
        children = []
        for parent in population:
            child = _mutate(parent["params"], parent["family"], gen)
            evaluated += 1
            r = score(parent["family"], child)
            if r:
                children.append(r)
        pool = {(_freeze(r["family"], r["params"])): r for r in (population + children)}
        population = sorted(pool.values(), key=lambda r: -r["score"])[:survivors]
        best_by_gen.append({"gen": gen, "best_score": round(population[0]["score"], 2) if population else None,
                            "survivors": len(population)})
    best = population[0] if population else None
    return {
        "ok": True, "generations": generations, "evaluated": evaluated,
        "generation_log": best_by_gen,
        "best": ({"family": best["family"], "params": best["params"], "score": round(best["score"], 2),
                  "holdout_roi": best["holdout"]["roi"], "holdout_trades": best["holdout"]["trades"],
                  "win_rate": best["holdout"]["win_rate"]} if best else None),
        "survivors": [{"family": r["family"], "score": round(r["score"], 2),
                       "holdout_roi": r["holdout"]["roi"], "trades": r["holdout"]["trades"]}
                      for r in population[:survivors]],
    }


def _freeze(family, prm):
    return (family, tuple(sorted((k, v) for k, v in prm.items())))


# ---------------------------------------------------------------------------
# model decay detection
# ---------------------------------------------------------------------------
def detect_decay(db: Session, *, slippage: float = DEFAULT_SLIPPAGE) -> dict:
    """Compare the fair-value model's calibration/EV between the early (train) and
    recent (holdout) regimes — degradation flags model decay / a regime shift."""
    feats = ALL_FEATURES
    trX, trY, trP = feature_matrix(lab._point_dicts(db, "train"), feats)
    hoX, hoY, hoP = feature_matrix(lab._point_dicts(db, "holdout"), feats)
    if len(trX) < 20 or len(hoX) < MIN_TRADES:
        return {"ok": False, "error": "dataset too small"}
    model = _fit("logistic_regression", trX, trY)
    tr_b = brier_score(model.predict_proba(trX), trY)
    ho_b = brier_score(model.predict_proba(hoX), hoY)
    decayed = ho_b > tr_b + 0.05
    return {"ok": True, "train_brier": tr_b, "holdout_brier": ho_b,
            "brier_degradation": round(ho_b - tr_b, 5), "decayed": bool(decayed),
            "interpretation": "holdout Brier >> train Brier ⇒ the signal degrades out-of-sample "
                              "(overfit or regime shift)"}


# ---------------------------------------------------------------------------
# persistence: trained-model leaderboard
# ---------------------------------------------------------------------------
def _save_models(db: Session, fv: dict, ens: dict) -> None:
    for old in db.scalars(select(lm.Btc5mResearchModel)).all():
        db.delete(old)
    if fv.get("ok"):
        ev = fv["ev"]
        db.add(lm.Btc5mResearchModel(
            name="fair_value", kind="fair_value", algo=fv["algo"], perspective="all",
            brier=fv["brier"], calibration_score=fv["calibration_score"], ece=fv["ece"], auc=fv["auc"],
            n_trades=ev["n_trades"], ev_after_cost=ev["ev_after_cost"], ev_t_stat=ev["t_stat"],
            ev_ci_low=ev["ci_low"], ev_ci_high=ev["ci_high"], roi=ev["roi"],
            significant=ev["significant"], promoted=ev["significant"],
            metrics={"reliability": fv["reliability"], "top_features": fv["top_features"], "sample": fv["sample"]}))
    if ens.get("ok"):
        for m in ens["members"]:
            ev = m["ev"]
            db.add(lm.Btc5mResearchModel(
                name=f"perspective:{m['perspective']}", kind="perspective", algo=ens["algo"],
                perspective=m["perspective"], brier=m["holdout_brier"], auc=m["auc"], weight=m["weight"],
                calibration_score=calibration_score(m["holdout_brier"]),
                n_trades=ev["n_trades"], ev_after_cost=ev["ev_after_cost"], ev_t_stat=ev["t_stat"],
                ev_ci_low=ev["ci_low"], ev_ci_high=ev["ci_high"], roi=ev["roi"],
                significant=ev["significant"], promoted=False, metrics={"ev": ev}))
        e = ens["ensemble"]; ev = e["ev"]
        db.add(lm.Btc5mResearchModel(
            name="ensemble", kind="ensemble", algo=ens["algo"], perspective="weighted",
            brier=e["brier"], calibration_score=e["calibration_score"], auc=e["auc"],
            n_trades=ev["n_trades"], ev_after_cost=ev["ev_after_cost"], ev_t_stat=ev["t_stat"],
            ev_ci_low=ev["ci_low"], ev_ci_high=ev["ci_high"], roi=ev["roi"],
            significant=ev["significant"], promoted=ev["significant"],
            metrics={"reliability": e["reliability"], "members": ens["members"]}))
    db.commit()


def model_leaderboard(db: Session) -> dict:
    rows = db.scalars(select(lm.Btc5mResearchModel).order_by(lm.Btc5mResearchModel.calibration_score.desc())).all()
    def row(m):
        return {"name": m.name, "kind": m.kind, "algo": m.algo, "perspective": m.perspective,
                "brier": m.brier, "calibration_score": m.calibration_score, "auc": m.auc, "weight": m.weight,
                "n_trades": m.n_trades, "ev_after_cost": m.ev_after_cost, "ev_t_stat": m.ev_t_stat,
                "ev_ci": [m.ev_ci_low, m.ev_ci_high], "roi": m.roi,
                "significant": m.significant, "promoted": m.promoted, "metrics": m.metrics}
    return {"models": [row(m) for m in rows],
            "promoted": [row(m) for m in rows if m.promoted]}


# ---------------------------------------------------------------------------
# nightly research pipeline + report
# ---------------------------------------------------------------------------
def run_pipeline(db: Session, *, build: bool = False, limit_markets: int = 60,
                 fetch_fn=None, slippage: float = DEFAULT_SLIPPAGE) -> dict:
    """The nightly research run: (optionally) ingest+rebuild the dataset, then
    fair-value + ensemble + feature discovery + microstructure + cross-market +
    evolutionary search + decay detection, persist models, and produce a research
    report with a verdict. 100% paper/research — never trades."""
    build_summary = None
    if build:
        build_summary = lab.build_dataset(db, limit_markets=limit_markets, fetch_fn=fetch_fn)
        lab.run_search(db, slippage=slippage)
    fv = fair_value(db, slippage=slippage)
    ens = ensemble(db, slippage=slippage)
    feats = discover_features(db)
    micro = microstructure(db)
    cross = cross_market(db)
    evo = evolve(db, slippage=slippage)
    decay = detect_decay(db, slippage=slippage)
    _save_models(db, fv, ens)
    report = _assemble_report(db, fv, ens, feats, micro, cross, evo, decay, build_summary)
    st = _state(db)
    st.research = report
    st.research_built_at = datetime.utcnow()
    db.commit()
    return report


def _assemble_report(db, fv, ens, feats, micro, cross, evo, decay, build_summary) -> dict:
    src_q = lab.btc_source_quality(db)
    n_pts = _state(db).points_built or 0
    fv_ev = (fv.get("ev") or {}) if fv.get("ok") else {}
    ens_ev = (ens.get("ensemble", {}).get("ev") or {}) if ens.get("ok") else {}
    evo_best = evo.get("best") if evo.get("ok") else None
    # any model/strategy with significant post-cost EV?
    fv_sig = bool(fv_ev.get("significant"))
    ens_sig = bool(ens_ev.get("significant"))
    evo_sig = bool(evo_best and evo_best.get("holdout_roi", 0) > 0 and evo_best.get("holdout_trades", 0) >= MIN_TRADES)
    # predictive-but-not-tradeable: model discriminates (AUC>0.55 / calibration skill) but EV not significant
    fv_predictive = bool(fv.get("ok") and (fv.get("auc", 0.5) >= 0.55 or fv.get("calibration_score", 0) > 0.02))

    if not src_q.get("is_true_1s") or src_q.get("coverage_pct", 0) < 70 or n_pts < 60 or not fv.get("ok"):
        code, verdict = 4, "data insufficient"
        headline = (f"data insufficient for fair-value research — source {src_q.get('source')} "
                    f"({src_q.get('resolution_s')}s, {src_q.get('coverage_pct')}% cov, {n_pts} points)")
    elif fv_sig or ens_sig or evo_sig:
        code, verdict = 1, "tradeable edge (significant post-cost EV)"
        who = "fair-value model" if fv_sig else ("ensemble" if ens_sig else f"evolved {evo_best['family']}")
        ev = fv_ev if fv_sig else (ens_ev if ens_sig else {})
        headline = (f"{who} shows statistically-significant EV after spread+slippage "
                    f"(EV/trade {ev.get('ev_after_cost')}, t={ev.get('t_stat')}, n={ev.get('n_trades')}) — "
                    "candidate for paper-validation before any deployment")
    elif fv_predictive:
        code, verdict = 2, "predictive signal, not yet tradeable"
        headline = (f"fair-value model has out-of-sample skill (AUC {fv.get('auc')}, calibration "
                    f"{fv.get('calibration_score')}, Brier {fv.get('brier')}) but its post-cost EV is not "
                    f"significant (t={fv_ev.get('t_stat')}, n={fv_ev.get('n_trades')}): edge exists, costs eat it")
    else:
        code, verdict = 3, "efficient market (no durable post-cost edge)"
        headline = (f"market is efficiently priced at this scale — best AUC {fv.get('auc')}, Brier "
                    f"{fv.get('brier')}; no model or evolved strategy beat spread+slippage out-of-sample")

    return {
        "verdict_code": code, "verdict": verdict, "headline": headline,
        "generated_at": datetime.utcnow().isoformat(),
        "build": build_summary,
        "btc_source_quality": src_q,
        "fair_value": fv, "ensemble": ens, "feature_discovery": feats,
        "microstructure": micro, "cross_market": cross,
        "evolution": evo, "decay": decay,
        "promoted_models": model_leaderboard(db)["promoted"],
        "newly_discovered_features": (feats.get("promoted") or [])[:8] if feats.get("ok") else [],
        "safety": "research/paper only — estimates probabilities; never places orders or touches live trading",
    }


def research_status(db: Session) -> dict:
    st = _state(db)
    return {
        "research": st.research,
        "research_built_at": st.research_built_at.isoformat() if st.research_built_at else None,
        "model_leaderboard": model_leaderboard(db),
        "safety": "BTC 5M Alpha Research Platform — research/paper only; estimates fair value, never trades",
    }
