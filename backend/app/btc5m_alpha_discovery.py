"""BTC 5M Alpha Discovery Engine (Phase 2) — research/paper ONLY.

Phase 1 proved the models carry genuine predictive information (ensemble AUC ~0.85)
but the edge vanishes after spread+slippage. So prediction is no longer the
bottleneck — finding sources of alpha the market does NOT already price is.

This engine continuously MINES new candidate features, scores each with a full
statistical suite (Information Coefficient, Mutual Information, permutation /
SHAP-style importance, stability across splits / regimes / months, redundancy,
decay), keeps only the statistically stable ones, and tracks every feature across
nightly GENERATIONS so we can see which features gain or lose predictive power.

A meta-learning layer retrains a fair-value model on the surviving features each
generation and manages a model LIFECYCLE (candidate → paper → demoted → retired)
driven purely by out-of-sample performance. Strict promotion rules apply, and a
model can reach at most 'paper' here: nothing goes live from a backtest, and there
is no live-trading path in this module.

100% read-only w.r.t. production — it reads the lab dataset, fetches public spot
prices (BTC/ETH/SOL via Kraken), and writes only btc5m_alpha_* / btc5m_lab_* rows.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import btc5m_alpha_research as ph1
from . import btc5m_models as bm
from . import btc5m_ml as ml
from . import btc5m_strategy_lab as lab
from . import btc5m_strategy_models as lm

_mean = lab._mean
_std = lab._std
_corr = lab._corr
_state = lab._state
_num = ph1._num

# survival thresholds (a feature must clear ALL to be considered stable alpha)
MIN_IC = 0.05               # |Spearman IC| on train
MIN_MI = 0.004              # mutual information (nats)
MIN_STABILITY = 0.5         # fraction of splits/regimes/months with consistent sign
MAX_REDUNDANCY = 0.92       # drop a feature too correlated with a stronger survivor
MAX_DECAY = 0.75            # 1 - |ic_holdout|/|ic_train|; >this ⇒ decayed out of sample
GAIN_THRESH = 0.02          # |Δic| vs previous generation that counts as gain/loss


# ---------------------------------------------------------------------------
# statistical primitives (pure python; no numpy/sklearn)
# ---------------------------------------------------------------------------
def _ranks(v: list[float]) -> list[float]:
    order = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def spearman_ic(xs: list[float], ys: list[float]) -> float:
    """Rank correlation — the Information Coefficient. Robust to monotone nonlinearity."""
    if len(xs) < 5 or len(set(xs)) < 2:
        return 0.0
    return _corr(_ranks(xs), _ranks(ys))


def mutual_information(xs: list[float], ys: list[int], bins: int = 5) -> float:
    """MI between a continuous feature (quantile-binned) and a binary label, in nats."""
    n = len(xs)
    if n < 10 or len(set(xs)) < 2:
        return 0.0
    sv = sorted(xs)
    edges = [sv[min(n - 1, int(n * q / bins))] for q in range(1, bins)]

    def b(x):
        lo = 0
        for e in edges:
            if x > e:
                lo += 1
        return lo
    joint: dict = {}
    px: dict = {}
    py: dict = {}
    for x, y in zip(xs, ys):
        bx = b(x)
        joint[(bx, y)] = joint.get((bx, y), 0) + 1
        px[bx] = px.get(bx, 0) + 1
        py[y] = py.get(y, 0) + 1
    mi = 0.0
    for (bx, y), c in joint.items():
        pxy = c / n
        denom = (px[bx] / n) * (py[y] / n)
        if pxy > 0 and denom > 0:
            mi += pxy * math.log(pxy / denom)
    return max(0.0, round(mi, 5))


def permutation_importance(feats: list[str], vals: dict, train, eval_pts, *, eval_split: str = "val") -> dict:
    """SHAP-style model-agnostic importance: train a logistic model on the surviving
    features (TRAIN), then measure the AUC drop on an EVALUATION split (validation by
    default — NEVER holdout, to keep holdout untouched for the final EV test) when each
    feature column is shuffled (a deterministic half-roll, so runs are reproducible)."""
    trX = [[vals["train"][f][i] for f in feats] for i in range(len(train))]
    evX = [[vals[eval_split][f][i] for f in feats] for i in range(len(eval_pts))]
    trY = [p["_label"] for p in train]
    evY = [p["_label"] for p in eval_pts]
    if len(trX) < 20 or len(evX) < 8:
        return {f: 0.0 for f in feats}
    model = ml.LogisticRegression().fit(trX, trY)
    base = ph1.auc_score(model.predict_proba(evX), evY)
    out = {}
    shift = max(1, len(evX) // 2)
    for j, f in enumerate(feats):
        col = [row[j] for row in evX]
        perm = col[shift:] + col[:shift]
        Xp = [row[:] for row in evX]
        for i in range(len(Xp)):
            Xp[i][j] = perm[i]
        out[f] = round(base - ph1.auc_score(model.predict_proba(Xp), evY), 4)
    return out


# ---------------------------------------------------------------------------
# dataset loader (augmented with month + regime for stability axes)
# ---------------------------------------------------------------------------
def _load(db: Session, split: str) -> list[dict]:
    rows = db.scalars(select(lm.Btc5mLabPoint).where(lm.Btc5mLabPoint.split == split)).all()
    mids = {r.market_id for r in rows}
    month = {}
    for mid in mids:
        mk = db.get(bm.Btc5mMarket, mid)
        month[mid] = mk.created_time.strftime("%Y-%m") if (mk and mk.created_time) else "?"
    out = []
    for r in rows:
        if r.label_up is None or r.pm_yes is None:
            continue
        f = dict(r.features or {})
        f.update({"pm_yes": r.pm_yes, "spread": r.spread, "t_offset_s": r.t_offset_s,
                  "secs_to_expiry": r.secs_to_expiry, "_label": 1 if r.label_up else 0,
                  "_regime": r.regime or "?", "_month": month.get(r.market_id, "?"),
                  "label_up": r.label_up, "market_id": r.market_id})
        out.append(f)
    return out


# ---------------------------------------------------------------------------
# candidate feature generation (categories from the Phase-2 brief)
# ---------------------------------------------------------------------------
_RET_HORIZONS = ["btc_ret_1s", "btc_ret_2s", "btc_ret_3s", "btc_ret_5s", "btc_ret_10s",
                 "btc_ret_20s", "btc_ret_30s", "btc_ret_60s"]
_CORE = ["btc_ret_sofar", "btc_ret_3s", "btc_ret_5s", "btc_ret_10s", "btc_ret_30s",
         "btc_momentum", "btc_acceleration", "btc_vol", "btc_breakout",
         "flow_imbalance", "recent_flow_imbalance", "pm_momentum", "lag",
         "wallet_signal", "wallet_recent_signal", "trade_freq", "volume_usd",
         "has_large_trade", "large_trade_usd", "secs_to_expiry", "t_offset_s", "pm_yes"]
_REGIMES = ["strong_trend", "high_vol", "chop", "mixed"]


def generate_candidates() -> list[tuple]:
    """Return [(name, category, fn)]. Mines a large space from the available decision-
    point data: nonlinear transforms, multi-timeframe structure, acceleration/jerk,
    interactions, lags, regime-conditioned and time-to-expiry variants. (Raw L2 book /
    quote-replenishment categories need tick/book ingestion we don't store — see the
    report's `data_gaps`.)"""
    C: list[tuple] = []

    def add(name, cat, fn):
        C.append((name, cat, fn))

    # passthrough base
    for f in _CORE:
        add(f, "base", (lambda p, f=f: _num(p.get(f))))
    # nonlinear transforms
    for f in _CORE:
        add(f"sq[{f}]", "nonlinear", (lambda p, f=f: _num(p.get(f)) ** 2))
        add(f"abs[{f}]", "nonlinear", (lambda p, f=f: abs(_num(p.get(f)))))
        add(f"sign[{f}]", "nonlinear", (lambda p, f=f: (1.0 if _num(p.get(f)) > 0 else (-1.0 if _num(p.get(f)) < 0 else 0.0))))
        add(f"tanh[{f}]", "nonlinear", (lambda p, f=f: math.tanh(_num(p.get(f)) * 50)))
    # multi-timeframe BTC structure + acceleration/jerk (finite differences of returns)
    for a, b in [("btc_ret_1s", "btc_ret_3s"), ("btc_ret_3s", "btc_ret_10s"),
                 ("btc_ret_5s", "btc_ret_30s"), ("btc_ret_10s", "btc_ret_60s"),
                 ("btc_ret_5s", "btc_ret_60s")]:
        add(f"mtf_diff[{a}-{b}]", "multi_timeframe", (lambda p, a=a, b=b: _num(p.get(a)) - _num(p.get(b))))
        add(f"mtf_ratio[{a}/{b}]", "multi_timeframe", (lambda p, a=a, b=b: _num(p.get(a)) / (abs(_num(p.get(b))) + 1e-5)))
    add("jerk", "acceleration", (lambda p: _num(p.get("btc_ret_1s")) - 2 * _num(p.get("btc_ret_2s")) + _num(p.get("btc_ret_3s"))))
    add("accel_5_30", "acceleration", (lambda p: _num(p.get("btc_ret_5s")) - _num(p.get("btc_ret_30s"))))
    # volatility expansion/compression (relative to move) + liquidity / clustering / entropy proxies
    add("vol_norm_ret5", "volatility", (lambda p: _num(p.get("btc_ret_5s")) / (_num(p.get("btc_vol")) + 1e-4)))
    add("vol_norm_mom", "volatility", (lambda p: _num(p.get("btc_momentum")) / (_num(p.get("btc_vol")) + 1e-4)))
    add("vol_x_ttexp", "volatility", (lambda p: _num(p.get("btc_vol")) * _num(p.get("secs_to_expiry"))))
    add("liq_imbalance", "liquidity", (lambda p: _num(p.get("flow_imbalance")) - _num(p.get("recent_flow_imbalance"))))
    add("clustering", "clustering", (lambda p: _num(p.get("trade_freq")) * _num(p.get("volume_usd"))))
    add("flow_entropy", "entropy", (lambda p: 1.0 - abs(_num(p.get("flow_imbalance")))))
    add("recent_flow_entropy", "entropy", (lambda p: 1.0 - abs(_num(p.get("recent_flow_imbalance")))))
    # probability velocity / acceleration (PM repricing dynamics) + cross-market lag
    add("prob_velocity", "prob_dynamics", (lambda p: _num(p.get("pm_momentum"))))
    add("prob_vel_x_btc", "prob_dynamics", (lambda p: _num(p.get("pm_momentum")) * _num(p.get("btc_ret_sofar"))))
    add("btc_lead_gap", "cross_market", (lambda p: _num(p.get("lag"))))
    add("btc_lead_x_vol", "cross_market", (lambda p: _num(p.get("lag")) * _num(p.get("btc_vol"))))
    # whale / wallet behavior
    add("whale_dir", "whale", (lambda p: _num(p.get("has_large_trade")) * (1.0 if _num(p.get("flow_imbalance")) > 0 else -1.0)))
    add("whale_size_signed", "whale", (lambda p: _num(p.get("large_trade_usd")) * (1.0 if _num(p.get("flow_imbalance")) > 0 else -1.0)))
    add("wallet_x_btc", "wallet", (lambda p: _num(p.get("wallet_signal")) * _num(p.get("btc_ret_sofar"))))
    add("wallet_conviction", "wallet", (lambda p: _num(p.get("wallet_signal")) * _num(p.get("wallet_trade_count"))))
    # time-to-expiry interactions
    for f in ("btc_ret_5s", "flow_imbalance", "wallet_signal", "lag", "pm_momentum"):
        add(f"tte_x[{f}]", "time_to_expiry", (lambda p, f=f: _num(p.get(f)) / (1.0 + _num(p.get("secs_to_expiry")) / 60.0)))
    # pairwise interactions (curated strong set)
    inter = ["btc_ret_3s", "btc_ret_5s", "btc_ret_10s", "btc_momentum", "flow_imbalance",
             "recent_flow_imbalance", "lag", "wallet_signal", "pm_momentum", "btc_vol",
             "trade_freq", "has_large_trade", "btc_breakout", "btc_acceleration"]
    for i in range(len(inter)):
        for j in range(i + 1, len(inter)):
            a, b = inter[i], inter[j]
            add(f"x[{a}*{b}]", "interaction", (lambda p, a=a, b=b: _num(p.get(a)) * _num(p.get(b))))
    # regime-conditioned versions of the strongest signals (regime transitions / regime-specific alpha)
    for f in ("btc_ret_5s", "flow_imbalance", "wallet_signal", "lag", "btc_momentum",
              "recent_flow_imbalance", "pm_momentum"):
        for rg in _REGIMES:
            add(f"{f}@{rg}", "regime", (lambda p, f=f, rg=rg: _num(p.get(f)) if p.get("_regime") == rg else 0.0))
    return C


# ---------------------------------------------------------------------------
# feature mining + scoring
# ---------------------------------------------------------------------------
def _stability(per_group_ic: dict, sign: float) -> float:
    groups = [v for v in per_group_ic.values() if v is not None]
    if not groups:
        return 0.0
    consistent = sum(1 for ic in groups if (ic > 0) == (sign > 0) and abs(ic) >= 0.02)
    return round(consistent / len(groups), 3)


def mine_features(db: Session, *, top_survivors: int = 40) -> dict:
    """Generate candidates and score each with IC / MI / stability(splits, regime,
    month) / redundancy / decay. Keep only statistically stable, non-redundant
    features. Permutation (SHAP-style) importance is computed for the survivors."""
    train, val, hold = (_load(db, s) for s in ("train", "val", "holdout"))
    if len(train) < 25 or len(hold) < 8:
        return {"ok": False, "error": "dataset too small", "n_train": len(train), "n_holdout": len(hold)}
    cands = generate_candidates()
    trY = [p["_label"] for p in train]
    vaY = [p["_label"] for p in val]
    hoY = [p["_label"] for p in hold]
    # precompute values per split
    vals = {"train": {}, "val": {}, "holdout": {}}
    scored = []
    for name, cat, fn in cands:
        xt = [fn(p) for p in train]
        if len(set(xt)) < 2:
            continue
        ic = spearman_ic(xt, trY)
        if abs(ic) < MIN_IC:
            continue
        xv = [fn(p) for p in val]
        xh = [fn(p) for p in hold]
        ic_val = spearman_ic(xv, vaY) if val else 0.0
        ic_hold = spearman_ic(xh, hoY)            # REPORTED ONLY — never used to select
        mi = mutual_information(xt, trY)
        if mi < MIN_MI:
            continue
        sign = ic
        # stability is judged on TRAIN folds + VALIDATION only (holdout stays untouched
        # so the final EV test isn't biased by selecting features that fit the holdout).
        half = len(train) // 2
        ic_tr_a = spearman_ic(xt[:half], trY[:half])
        ic_tr_b = spearman_ic(xt[half:], trY[half:])
        stab_splits = _stability({"tr_a": ic_tr_a, "tr_b": ic_tr_b, "val": ic_val}, sign)
        # by regime
        reg_ic = {}
        for rg in set(p["_regime"] for p in train):
            seg = [(fn(p), p["_label"]) for p in train if p["_regime"] == rg]
            reg_ic[rg] = spearman_ic([a for a, _ in seg], [b for _, b in seg]) if len(seg) >= 10 else None
        stab_regime = _stability(reg_ic, sign)
        # by month
        mon_ic = {}
        for mo in set(p["_month"] for p in train):
            seg = [(fn(p), p["_label"]) for p in train if p["_month"] == mo]
            mon_ic[mo] = spearman_ic([a for a, _ in seg], [b for _, b in seg]) if len(seg) >= 10 else None
        stab_month = _stability(mon_ic, sign)
        # decay = degradation from train to VALIDATION (leakage-free; holdout stays clean)
        decay = round(1.0 - (abs(ic_val) / abs(ic)) if abs(ic) > 1e-9 else 1.0, 3)
        scored.append({
            "name": name, "category": cat, "ic": round(ic, 4), "ic_pearson": round(_corr(xt, trY), 4),
            "ic_val": round(ic_val, 4), "ic_hold": round(ic_hold, 4), "mutual_info": mi,
            "stability_splits": stab_splits, "stability_regime": stab_regime, "stability_month": stab_month,
            "decay": decay, "abs_ic": abs(ic),
        })
        vals["train"][name] = xt
        vals["val"][name] = xv
        vals["holdout"][name] = xh
    scored.sort(key=lambda s: -s["abs_ic"])
    # survival + greedy redundancy pruning
    survivors = []
    for s in scored:
        if s["stability_splits"] < MIN_STABILITY or s["decay"] > MAX_DECAY:
            continue
        red = 0.0
        for kept in survivors:
            red = max(red, abs(_corr(vals["train"][s["name"]], vals["train"][kept["name"]])))
        if red > MAX_REDUNDANCY:
            s["redundancy"] = round(red, 3)
            continue
        s["redundancy"] = round(red, 3)
        survivors.append(s)
        if len(survivors) >= top_survivors:
            break
    # SHAP-style permutation importance for survivors — on VALIDATION, never holdout
    surv_names = [s["name"] for s in survivors]
    shap = permutation_importance(surv_names, vals, train, val, eval_split="val") if surv_names else {}
    for s in survivors:
        s["shap_importance"] = shap.get(s["name"], 0.0)
    survivors.sort(key=lambda s: (-s["shap_importance"], -s["abs_ic"]))
    return {"ok": True, "generated": len(cands), "evaluated": len(scored),
            "survived": len(survivors), "survivors": survivors,
            "by_category": _category_counts(survivors),
            "_vals": vals, "_train": train, "_holdout": hold}


def _category_counts(survivors: list[dict]) -> dict:
    out: dict = {}
    for s in survivors:
        out[s["category"]] = out.get(s["category"], 0) + 1
    return out


# ---------------------------------------------------------------------------
# persistent feature registry + generational tracking
# ---------------------------------------------------------------------------
def _update_registry(db: Session, survivors: list[dict], generation: int) -> dict:
    """Upsert survivors into the registry, append to their history, compute Δic vs the
    previous generation, and mark features that dropped out as decayed. Returns the
    generational diff (new / gained / lost)."""
    existing = {f.name: f for f in db.scalars(select(lm.Btc5mAlphaFeature)).all()}
    surv_names = {s["name"] for s in survivors}
    new_alpha, gained, lost = [], [], []
    for s in survivors:
        row = existing.get(s["name"])
        prev_ic = row.ic if row else 0.0
        ic_change = round(s["ic"] - prev_ic, 4)
        if row is None:
            row = lm.Btc5mAlphaFeature(name=s["name"], category=s["category"], first_seen_gen=generation)
            db.add(row)
            new_alpha.append(s["name"])
        elif row.status in ("decayed", "retired"):
            new_alpha.append(s["name"])          # re-emerged
        row.generation = generation
        row.ic = s["ic"]; row.ic_pearson = s["ic_pearson"]; row.mutual_info = s["mutual_info"]
        row.shap_importance = s["shap_importance"]; row.stability_splits = s["stability_splits"]
        row.stability_regime = s["stability_regime"]; row.stability_month = s["stability_month"]
        row.redundancy = s["redundancy"]; row.decay = s["decay"]; row.ic_change = ic_change
        row.survived = True; row.status = "active"
        row.description = f"{s['category']} · IC {s['ic']} · MI {s['mutual_info']}"
        hist = list(row.history or [])
        hist.append({"gen": generation, "ic": s["ic"], "mi": s["mutual_info"], "shap": s["shap_importance"]})
        row.history = hist[-24:]
        if row.name not in {n for n in new_alpha} and ic_change >= GAIN_THRESH:
            gained.append({"name": s["name"], "ic_change": ic_change})
    # features that were active last gen but dropped out → decayed (lost power)
    for name, row in existing.items():
        if name not in surv_names and row.status == "active":
            row.status = "decayed"
            row.survived = False
            row.ic_change = round(0.0 - row.ic, 4)
            lost.append({"name": name, "ic_change": row.ic_change})
            row.generation = generation
    db.commit()
    return {"new_alpha": new_alpha, "gained_power": gained[:12], "lost_power": lost[:12]}


def feature_registry(db: Session, *, limit: int = 60) -> dict:
    rows = db.scalars(select(lm.Btc5mAlphaFeature)
                      .order_by(lm.Btc5mAlphaFeature.shap_importance.desc(),
                                lm.Btc5mAlphaFeature.survived.desc())).all()
    def row(f):
        return {"name": f.name, "category": f.category, "generation": f.generation,
                "first_seen_gen": f.first_seen_gen, "ic": f.ic, "ic_pearson": f.ic_pearson,
                "mutual_info": f.mutual_info, "shap_importance": f.shap_importance,
                "stability_splits": f.stability_splits, "stability_regime": f.stability_regime,
                "stability_month": f.stability_month, "redundancy": f.redundancy, "decay": f.decay,
                "ic_change": f.ic_change, "survived": f.survived, "status": f.status,
                "history": f.history}
    active = [row(f) for f in rows if f.survived]
    return {"active": active[:limit], "n_active": sum(1 for f in rows if f.survived),
            "n_total_tracked": len(rows), "all": [row(f) for f in rows][:limit]}


# ---------------------------------------------------------------------------
# meta-learning: retrain on survivors, manage model lifecycle
# ---------------------------------------------------------------------------
def _promotion_decision(gen_metrics: dict) -> tuple[str, str]:
    """Strict promotion rules. A model can reach at most 'paper' — never live from a
    backtest. Requires statistically-significant +EV after costs AND robust OOS AND
    sample size AND regime stability AND low decay."""
    m = gen_metrics
    reasons = []
    if not m["significant"]:
        reasons.append("EV after costs not statistically significant")
    if m["ev_after_cost"] <= 0:
        reasons.append("non-positive post-cost EV")
    if m["n_trades"] < ph1.MIN_TRADES:
        reasons.append(f"insufficient sample ({m['n_trades']} < {ph1.MIN_TRADES})")
    if m["regime_stability"] < 0.5:
        reasons.append(f"unstable across regimes ({m['regime_stability']})")
    if m["decay"] > 0.05:
        reasons.append(f"model decay too high (Δbrier {m['decay']})")
    if not reasons:
        return "paper", "passed all promotion gates → eligible for PAPER trading (never live from backtest)"
    return "candidate", "; ".join(reasons)


def meta_learn(db: Session, mined: dict, generation: int, *, slippage: float = ph1.DEFAULT_SLIPPAGE) -> dict:
    """Retrain a fair-value model on the surviving mined features, score it OOS, decide
    its lifecycle vs the previous generation, and persist the model generation."""
    survivors = mined.get("survivors", [])
    feats = [s["name"] for s in survivors][:24]
    train, hold = mined["_train"], mined["_holdout"]
    vals = mined["_vals"]
    if len(feats) < 3 or len(train) < 25 or len(hold) < 8:
        return {"ok": False, "error": "not enough survivors/data to retrain"}
    trX = [[vals["train"][f][i] for f in feats] for i in range(len(train))]
    hoX = [[vals["holdout"][f][i] for f in feats] for i in range(len(hold))]
    trY = [p["_label"] for p in train]
    hoY = [p["_label"] for p in hold]
    model = ml.LogisticRegression().fit(trX, trY)
    ho_probs = model.predict_proba(hoX)
    tr_probs = model.predict_proba(trX)
    brier = ph1.brier_score(ho_probs, hoY)
    auc = ph1.auc_score(ho_probs, hoY)
    ev = ph1.ev_after_costs(ho_probs, hold, slippage=slippage)
    decay = round(ph1.brier_score(ho_probs, hoY) - ph1.brier_score(tr_probs, trY), 4)
    # regime stability of EV: fraction of regimes with non-negative post-cost EV
    reg_ev = {}
    for rg in set(p["_regime"] for p in hold):
        seg_i = [i for i, p in enumerate(hold) if p["_regime"] == rg]
        if len(seg_i) >= 5:
            reg_ev[rg] = ph1.ev_after_costs([ho_probs[i] for i in seg_i], [hold[i] for i in seg_i], slippage=slippage)["ev_after_cost"]
    regime_stability = round(_mean([1.0 if v >= 0 else 0.0 for v in reg_ev.values()]), 3) if reg_ev else 0.0
    gen_metrics = {"auc": auc, "brier": brier, "ev_after_cost": ev["ev_after_cost"],
                   "ev_t_stat": ev["t_stat"], "n_trades": ev["n_trades"], "significant": ev["significant"],
                   "regime_stability": regime_stability, "decay": decay}
    state, reason = _promotion_decision(gen_metrics)
    robust = state == "paper"

    prev = db.scalar(select(lm.Btc5mAlphaModelGen).where(lm.Btc5mAlphaModelGen.name == "fair_value_mined")
                     .order_by(lm.Btc5mAlphaModelGen.generation.desc()))
    vs_prev = "new"
    if prev:
        if auc > prev.auc + 0.01 or ev["ev_after_cost"] > prev.ev_after_cost + 0.01:
            vs_prev = "improved"
        elif auc < prev.auc - 0.01 or ev["ev_after_cost"] < prev.ev_after_cost - 0.01:
            vs_prev = "degraded"
        else:
            vs_prev = "same"
        # demote a prior paper model whose retrain no longer passes the gate
        if prev.lifecycle_state == "paper" and state != "paper":
            prev.lifecycle_state = "demoted"
            prev.promotion_reason = f"gen {generation} retrain failed gate: {reason}"
        if prev.lifecycle_state in ("demoted",) and vs_prev == "degraded":
            prev.lifecycle_state = "retired"

    row = lm.Btc5mAlphaModelGen(
        generation=generation, name="fair_value_mined", algo="logistic_regression",
        feature_set=feats, n_features=len(feats), auc=auc, brier=brier,
        calibration_score=ph1.calibration_score(brier), ev_after_cost=ev["ev_after_cost"],
        ev_t_stat=ev["t_stat"], n_trades=ev["n_trades"], significant=ev["significant"],
        regime_stability=regime_stability, decay=decay, robust=robust,
        lifecycle_state=state, promotion_reason=reason, vs_prev=vs_prev,
        metrics={"ev": ev, "reg_ev": reg_ev, "top_features": feats[:10]})
    db.add(row)
    db.commit()
    return {"ok": True, "generation": generation, "lifecycle_state": state, "vs_prev": vs_prev,
            "promotion_reason": reason, "metrics": gen_metrics, "feature_set": feats}


def model_generations(db: Session, *, limit: int = 20) -> dict:
    rows = db.scalars(select(lm.Btc5mAlphaModelGen)
                      .order_by(lm.Btc5mAlphaModelGen.generation.desc()).limit(limit)).all()
    def row(m):
        return {"generation": m.generation, "name": m.name, "n_features": m.n_features,
                "auc": m.auc, "brier": m.brier, "calibration_score": m.calibration_score,
                "ev_after_cost": m.ev_after_cost, "ev_t_stat": m.ev_t_stat, "n_trades": m.n_trades,
                "significant": m.significant, "regime_stability": m.regime_stability, "decay": m.decay,
                "robust": m.robust, "lifecycle_state": m.lifecycle_state,
                "promotion_reason": m.promotion_reason, "vs_prev": m.vs_prev, "feature_set": m.feature_set}
    return {"generations": [row(m) for m in rows],
            "paper": [row(m) for m in rows if m.lifecycle_state == "paper"]}


# ---------------------------------------------------------------------------
# cross-market: does any external asset lead Polymarket?
# ---------------------------------------------------------------------------
def cross_market_assets(db: Session, *, sample_markets: int = 6, fetch_fn=None) -> dict:
    """Test whether ETH / SOL spot lead the BTC Polymarket repricing (same 1s cross-
    correlation method as the BTC-spot lead). Bounded sample to stay fast. Derivatives
    feeds (funding/OI/liquidations/options IV/macro) need other APIs — listed as gaps."""
    markets = db.scalars(select(bm.Btc5mMarket).where(bm.Btc5mMarket.resolved.is_(True))
                         .order_by(bm.Btc5mMarket.created_time.desc()).limit(sample_markets)).all()
    assets = {"ETH": "ETHUSD", "SOL": "SOLUSD"}
    results = {}
    for label, pair in assets.items():
        peaks, leads = [], 0
        for mk in markets:
            dur = lab._slug_duration(mk.slug, mk.question) or 5
            life = dur * 60
            start = mk.created_time
            end = mk.resolution_time or (start + timedelta(seconds=life) if start else None)
            if not (start and end):
                continue
            try:
                if fetch_fn is not None:
                    series = fetch_fn(label, start, end)
                else:
                    series, _ = lab._kraken_1s(start, end, pair=pair)
            except Exception:  # noqa: BLE001  (fail-soft; asset feed optional)
                continue
            if not series:
                continue
            trades = db.scalars(select(bm.Btc5mTrade).where(bm.Btc5mTrade.market_id == mk.market_id)).all()
            prof = lab._market_lag_profile(series, trades, life)
            if prof:
                peak = max(prof, key=lambda k: prof[k])
                peaks.append((peak, prof[peak]))
                if peak > 0 and prof[peak] >= 0.05:
                    leads += 1
        if peaks:
            avg_lag = round(_mean([p for p, _ in peaks]), 1)
            avg_corr = round(_mean([c for _, c in peaks]), 4)
            results[label] = {"n_markets": len(peaks), "avg_peak_lag_s": avg_lag, "avg_peak_corr": avg_corr,
                              "leads_fraction": round(leads / len(peaks), 2),
                              "leads": leads >= max(1, len(peaks) // 2) and avg_corr >= 0.05}
        else:
            results[label] = {"n_markets": 0, "leads": False, "note": "no data (feed unreachable or empty)"}
    return {"ok": True, "assets": results,
            "interpretation": "an asset 'leads' if its 1s move predicts the YES change a few seconds later, "
                              "across most sampled markets — a candidate external alpha source",
            "data_gaps": ["perp funding rates", "open interest", "liquidations", "options implied vol",
                          "macro event calendar", "correlated Polymarket contracts"]}


# ---------------------------------------------------------------------------
# nightly orchestration + generational report
# ---------------------------------------------------------------------------
def run_discovery(db: Session, *, slippage: float = ph1.DEFAULT_SLIPPAGE,
                  cross_assets: bool = True, fetch_fn=None) -> dict:
    """One discovery GENERATION: mine features → update registry (new/gained/lost) →
    meta-learn (retrain + lifecycle) → cross-asset leads → generational report."""
    st = _state(db)
    generation = (st.alpha_generation or 0) + 1
    mined = mine_features(db)
    if not mined.get("ok"):
        report = {"ok": False, "generation": generation, "error": mined.get("error"),
                  "headline": f"alpha discovery: {mined.get('error')}", "mining": mined}
        st.alpha_research = report
        st.alpha_built_at = datetime.utcnow()
        db.commit()
        return report
    diff = _update_registry(db, mined["survivors"], generation)
    meta = meta_learn(db, mined, generation, slippage=slippage)
    cross = cross_market_assets(db, fetch_fn=fetch_fn) if cross_assets else {"ok": False, "skipped": True}
    report = _assemble(db, generation, mined, diff, meta, cross)
    st.alpha_generation = generation
    st.alpha_research = report
    st.alpha_built_at = datetime.utcnow()
    db.commit()
    return report


def _assemble(db, generation, mined, diff, meta, cross) -> dict:
    survivors = mined["survivors"]
    top = [{"name": s["name"], "category": s["category"], "ic": s["ic"], "mutual_info": s["mutual_info"],
            "shap": s["shap_importance"], "stability_splits": s["stability_splits"],
            "stability_regime": s["stability_regime"], "stability_month": s["stability_month"],
            "decay": s["decay"]} for s in survivors[:15]]
    m = meta.get("metrics", {}) if meta.get("ok") else {}
    state = meta.get("lifecycle_state") if meta.get("ok") else None
    if state == "paper":
        code, verdict = 1, "alpha promoted to paper"
        headline = (f"gen {generation}: a fair-value model on {meta.get('feature_set') and len(meta['feature_set'])} "
                    f"mined features passed all gates (EV/trade {m.get('ev_after_cost')}, t={m.get('ev_t_stat')}, "
                    f"regime-stable) → eligible for PAPER trading (never live from backtest)")
    elif m.get("significant") is False and (m.get("auc", 0) >= 0.55):
        code, verdict = 2, "predictive alpha, not yet tradeable"
        headline = (f"gen {generation}: {mined['survived']} stable features mined (of {mined['generated']}); "
                    f"retrained model AUC {m.get('auc')} but post-cost EV not significant — {meta.get('promotion_reason')}")
    else:
        code, verdict = 3, "no tradeable alpha this generation"
        headline = (f"gen {generation}: {mined['survived']} stable features mined; "
                    f"no model passed the promotion gates after costs")
    leads = [a for a, v in (cross.get("assets") or {}).items() if v.get("leads")] if cross.get("ok") else []
    return {
        "ok": True, "generation": generation, "verdict_code": code, "verdict": verdict, "headline": headline,
        "generated_at": datetime.utcnow().isoformat(),
        "mining": {"generated": mined["generated"], "evaluated": mined["evaluated"],
                   "survived": mined["survived"], "by_category": mined["by_category"]},
        "top_features": top,
        "new_alpha": diff["new_alpha"], "gained_power": diff["gained_power"], "lost_power": diff["lost_power"],
        "model": meta if meta.get("ok") else {"ok": False, "reason": meta.get("error")},
        "cross_market": cross,
        "external_leads": leads,
        "data_gaps": ["raw L2 order book (book pressure / quote replenishment / liquidity add-remove)",
                      "tick-level trade stream (true entropy / micro-clustering)",
                      "derivatives: funding, open interest, liquidations, options IV", "macro event calendar"],
        "promotion_rules": "promote to PAPER only if: significant +EV after costs, robust OOS, sample ≥ "
                           f"{ph1.MIN_TRADES}, regime-stable, low decay. Never live from a backtest — paper first.",
        "safety": "research/paper only — mines & validates alpha; never places orders or touches live trading",
    }


def run_nightly(db: Session, *, build: bool = False, limit_markets: int = 80,
                fetch_fn=None, cross_assets: bool = True) -> dict:
    """Full nightly run: Phase-1 fair-value/ensemble pipeline THEN Phase-2 discovery.
    Paper/research only."""
    phase1 = ph1.run_pipeline(db, build=build, limit_markets=limit_markets, fetch_fn=fetch_fn)
    discovery = run_discovery(db, cross_assets=cross_assets, fetch_fn=fetch_fn)
    return {"phase1": phase1, "alpha_discovery": discovery,
            "generation": discovery.get("generation"),
            "headline": discovery.get("headline"),
            "safety": "research/paper only — never trades"}


def discovery_status(db: Session) -> dict:
    st = _state(db)
    return {
        "generation": st.alpha_generation or 0,
        "alpha_research": st.alpha_research,
        "alpha_built_at": st.alpha_built_at.isoformat() if st.alpha_built_at else None,
        "feature_registry": feature_registry(db),
        "model_generations": model_generations(db),
        "safety": "BTC 5M Alpha Discovery Engine — research/paper only; mines & validates alpha, never trades",
    }
