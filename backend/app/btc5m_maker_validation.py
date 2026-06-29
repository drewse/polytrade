"""BTC 5M Passive-Maker Validation — research/paper ONLY.

We stop inventing strategies and rigorously stress-test the ONE edge that keeps
surviving: the 5-second passive maker (join_bid / improve_bid, short rest window,
conservative worst-case queue). The question is binary — is it a real structural
edge or a small-sample fluke?

This runs the EXACT fixed winning configuration (no optimisation, no re-fitting the
execution rule) over the largest obtainable dataset and reports:
  B  stability across regimes (month / vol / bull-bear / volume / weekday / age /
     spread / liquidity)
  C  walk-forward over chronological time slices
  D  bootstrap confidence — P(true EV/fill > 0), the number that matters
  E  failure analysis — the conditions where it loses (when NOT to quote)
  F  parameter sensitivity — does the edge survive nearby settings?

100% isolated: reads the lab dataset + historical trades and the execution-lab
simulator; writes only a research blob. NEVER touches live.py / services.py /
live_ranking.py / execution / bankroll / approvals / copy trading. No order path.
"""
from __future__ import annotations

import math
import random
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import btc5m_execution_lab as ex
from . import btc5m_models as bm
from . import btc5m_strategy_lab as lab
from . import btc5m_strategy_models as lm


def _market_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(bm.Btc5mMarket).where(bm.Btc5mMarket.resolved.is_(True))) or 0


def _point_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(lm.Btc5mLabPoint)) or 0

_mean = ex._mean
_std = ex._std

# THE fixed winning configuration — pre-registered, NOT optimised here.
WIN = {"policy": "join_bid", "timeout": 5, "queue": "worst"}
ALT = {"policy": "improve_bid", "timeout": 5, "queue": "worst"}
DURS = [5, 15]
MIN_BUCKET = 4              # minimum fills to characterise a bucket
BOOT = 3000                # bootstrap resamples


def _signal_set(db: Session, splits=("train", "val", "holdout")):
    spec = next(iter(ex._models_to_test(db)), None)
    if spec is None:
        return None, []
    sigs = []
    for s in splits:
        sigs += ex.build_signals(db, s, spec["model"], spec["feats"], max_future=None)
    return spec, [s for s in sigs if s["duration_minutes"] in DURS]


def _simulate(sigs, cfg):
    """Return aligned (filled_results, all_results) for a config."""
    res = [ex.simulate_queue(s, cfg["policy"], timeout=cfg["timeout"], mode=cfg["queue"]) for s in sigs]
    return res


def _ev(results):
    filled = [r for r in results if r["filled"]]
    pnls = [r["pnl"] for r in filled]
    return _mean(pnls) if pnls else 0.0, pnls, filled


# ---------------------------------------------------------------------------
# Phase B — stability across regimes
# ---------------------------------------------------------------------------
def phase_b_stability(sigs, results) -> dict:
    pairs = list(zip(sigs, results))

    def bucket(keyfn):
        groups: dict = {}
        for s, r in pairs:
            if r["filled"]:
                groups.setdefault(str(keyfn(s)), []).append(r["pnl"])
        out = {}
        for k, v in sorted(groups.items()):
            if len(v) >= MIN_BUCKET:
                sd = _std(v)
                t = (_mean(v) / (sd / math.sqrt(len(v)))) if sd and len(v) > 1 else (99.0 if _mean(v) > 0 else 0.0)
                out[k] = {"fills": len(v), "ev_per_fill": round(_mean(v), 4), "t_stat": round(t, 2),
                          "positive": _mean(v) > 0}
        return out

    def vol(s):
        v = s["btc_vol"]
        return "hi_vol" if v > 0.0009 else ("lo_vol" if v < 0.0004 else "mid_vol")
    axes = {
        "month": lambda s: s.get("month", "?"),
        "volatility": vol,
        "btc_direction": lambda s: "bull" if s["btc_ret_sofar"] > 0 else "bear",
        "volume": lambda s: "high_vol$" if s["volume_usd"] > 400 else ("low_vol$" if s["volume_usd"] < 100 else "mid_vol$"),
        "day_type": lambda s: s.get("day_type", "?"),
        "market_age": lambda s: "young" if s["secs_to_expiry"] > 120 else "old",
        "spread": lambda s: "wide" if s.get("spread_w", 0) > 0.06 else "tight",
        "liquidity": lambda s: "deep" if s["volume_usd"] > 400 else ("thin" if s["volume_usd"] < 100 else "mid"),
        "duration": lambda s: f"{s['duration_minutes']}m",
        "time_of_day": lambda s: f"{(s.get('hour', 0) // 6) * 6:02d}-{(s.get('hour', 0) // 6) * 6 + 6:02d}h",
    }
    buckets = {ax: bucket(fn) for ax, fn in axes.items()}
    # stable vs concentrated: fraction of populated buckets (n>=MIN) that are positive
    allb = [b for ax in buckets.values() for b in ax.values()]
    pos_frac = round(_mean([1.0 if b["positive"] else 0.0 for b in allb]), 3) if allb else 0.0
    return {"buckets": buckets, "n_buckets": len(allb), "fraction_positive": pos_frac,
            "interpretation": "fraction_positive near 1 ⇒ broadly stable; near 0.5 ⇒ concentrated/noise"}


# ---------------------------------------------------------------------------
# Phase C — walk-forward over chronological slices
# ---------------------------------------------------------------------------
def phase_c_walkforward(sigs, results, *, folds: int = 6) -> dict:
    pairs = sorted(zip(sigs, results), key=lambda sr: sr[0].get("created_ts", 0))
    ts = [s.get("created_ts", 0) for s, _ in pairs]
    span_h = round((max(ts) - min(ts)) / 3600, 2) if ts and max(ts) > 0 else 0.0
    n = len(pairs)
    size = max(1, n // folds)
    out = []
    for i in range(folds):
        seg = pairs[i * size: (i + 1) * size] if i < folds - 1 else pairs[i * size:]
        pnls = [r["pnl"] for _, r in seg if r["filled"]]
        if not seg:
            continue
        sd = _std(pnls)
        t = (_mean(pnls) / (sd / math.sqrt(len(pnls)))) if sd and len(pnls) > 1 else (99.0 if pnls and _mean(pnls) > 0 else 0.0)
        cum = peak = mdd = 0.0
        for p in pnls:
            cum += p; peak = max(peak, cum); mdd = max(mdd, peak - cum)
        out.append({"fold": i + 1, "quotes": len(seg), "fills": len(pnls),
                    "fill_rate": round(len(pnls) / len(seg), 4),
                    "ev_per_fill": round(_mean(pnls), 4) if pnls else 0.0,
                    "sharpe": round(_mean(pnls) / sd, 3) if sd else 0.0,
                    "t_stat": round(t, 2), "max_drawdown": round(mdd, 4)})
    pos = sum(1 for f in out if f["fills"] >= MIN_BUCKET and f["ev_per_fill"] > 0)
    usable = sum(1 for f in out if f["fills"] >= MIN_BUCKET)
    return {"calendar_span_hours": span_h, "folds": out,
            "folds_positive": pos, "folds_usable": usable,
            "note": ("data spans only ~%.1f h — these are intra-window time slices, not months; "
                     "multi-month persistence cannot be tested with this dataset" % span_h)}


# ---------------------------------------------------------------------------
# Phase D — bootstrap confidence (the number that matters)
# ---------------------------------------------------------------------------
def phase_d_bootstrap(filled, *, seed: int = 12345) -> dict:
    pnls = [r["pnl"] for r in filled]
    caps = [r["spread_captured"] for r in filled]
    n = len(pnls)
    if n < MIN_BUCKET:
        return {"ok": False, "n_fills": n, "error": "too few fills to bootstrap"}
    rng = random.Random(seed)
    means, sharpes, advs, capt = [], [], [], []
    uncond = None  # adverse handled separately at metric level
    for _ in range(BOOT):
        sample = [pnls[rng.randrange(n)] for _ in range(n)]
        m = _mean(sample); sd = _std(sample)
        means.append(m)
        sharpes.append(m / sd if sd else 0.0)
        cs = [caps[rng.randrange(n)] for _ in range(n)]
        capt.append(_mean(cs))
    means.sort(); sharpes.sort(); capt.sort()
    def ci(xs):
        return [round(xs[int(0.025 * len(xs))], 4), round(xs[int(0.975 * len(xs))], 4)]
    p_ev_pos = round(_mean([1.0 if m > 0 else 0.0 for m in means]), 4)
    return {"ok": True, "n_fills": n, "boot": BOOT,
            "ev_per_fill": {"point": round(_mean(pnls), 4), "ci95": ci(means)},
            "sharpe": {"point": round((_mean(pnls) / _std(pnls)) if _std(pnls) else 0.0, 3), "ci95": ci(sharpes)},
            "spread_captured": {"point": round(_mean(caps), 4), "ci95": ci(capt)},
            "prob_true_ev_positive": p_ev_pos}


# ---------------------------------------------------------------------------
# Phase E — failure analysis (when NOT to quote)
# ---------------------------------------------------------------------------
def phase_e_failure(sigs, results) -> dict:
    losers = [(s, r) for s, r in zip(sigs, results) if r["filled"] and r["pnl"] < 0]
    winners = [(s, r) for s, r in zip(sigs, results) if r["filled"] and r["pnl"] > 0]
    nL, nW = len(losers), len(winners)

    def profile(group, keyfn):
        if not group:
            return {}
        c: dict = {}
        for s, _ in group:
            c[str(keyfn(s))] = c.get(str(keyfn(s)), 0) + 1
        return {k: round(v / len(group), 3) for k, v in sorted(c.items(), key=lambda kv: -kv[1])}
    conds = {
        "btc_trend": lambda s: "strong" if abs(s["btc_ret_sofar"]) > 0.0012 else "calm",
        "volatility": lambda s: "hi_vol" if s["btc_vol"] > 0.0009 else "normal",
        "btc_lead": lambda s: "btc_ahead" if abs(s.get("lag", 0)) > 0.08 else "in_line",
        "flow": lambda s: "heavy_flow" if abs(s["flow_imbalance"]) > 0.4 else "balanced",
        "liquidity": lambda s: "thin" if s["volume_usd"] < 100 else "ok",
        "large_trade": lambda s: "large_present" if s.get("has_large_trade") else "none",
    }
    return {"n_losers": nL, "n_winners": nW,
            "loser_profile": {c: profile(losers, fn) for c, fn in conds.items()},
            "winner_profile": {c: profile(winners, fn) for c, fn in conds.items()},
            "interpretation": "conditions over-represented among losers vs winners ⇒ when NOT to quote"}


# ---------------------------------------------------------------------------
# Phase F — parameter sensitivity (is it a knife-edge?)
# ---------------------------------------------------------------------------
def phase_f_sensitivity(sigs) -> dict:
    grid = []
    for policy in ("join_bid", "improve_bid"):
        for to in (3, 4, 5, 6, 8):
            res = [ex.simulate_queue(s, policy, timeout=to, mode="worst") for s in sigs]
            ev, pnls, filled = _ev(res)
            sd = _std(pnls)
            t = (_mean(pnls) / (sd / math.sqrt(len(pnls)))) if sd and len(pnls) > 1 else (99.0 if pnls and ev > 0 else 0.0)
            grid.append({"policy": policy, "timeout_s": to, "fills": len(filled),
                         "ev_per_fill": round(ev, 4), "t_stat": round(t, 2), "positive": ev > 0})
    # entry-threshold perturbation handled via min_edge would need re-signalling; report timeout/policy/queue neighbourhood
    neighbourhood = [g for g in grid if g["policy"] in ("join_bid", "improve_bid") and 4 <= g["timeout_s"] <= 8]
    frac_pos = round(_mean([1.0 if g["positive"] else 0.0 for g in neighbourhood]), 3) if neighbourhood else 0.0
    # queue robustness at the fixed timeout
    queue_rob = []
    for mode in ex.QUEUE_MODES:
        res = [ex.simulate_queue(s, "join_bid", timeout=5, mode=mode) for s in sigs]
        ev, pnls, filled = _ev(res)
        queue_rob.append({"queue": mode, "fills": len(filled), "ev_per_fill": round(ev, 4), "positive": ev > 0})
    return {"timeout_policy_grid": grid, "neighbourhood_fraction_positive": frac_pos,
            "queue_robustness": queue_rob,
            "interpretation": "fraction_positive near 1 across nearby timeouts/policies ⇒ robust, not a knife-edge"}


# ---------------------------------------------------------------------------
# orchestration + verdict + the 4 answers
# ---------------------------------------------------------------------------
def run_validation(db: Session) -> dict:
    spec, sigs = _signal_set(db)
    if spec is None or len(sigs) < ex.MIN_TRADES:
        rep = {"ok": False, "error": "no model / too few signals", "n_signals": len(sigs)}
        _store(db, rep)
        return rep
    res = _simulate(sigs, WIN)
    _, pnls, filled = _ev(res)
    # out-of-sample-only cross-check (val+holdout — model is in-sample on train)
    _, oos = _signal_set(db, splits=("val", "holdout"))
    oos_res = _simulate(oos, WIN) if oos else []
    oos_filled = [r for r in oos_res if r["filled"]]

    B = phase_b_stability(sigs, res)
    C = phase_c_walkforward(sigs, res)
    D = phase_d_bootstrap(filled)
    D_oos = phase_d_bootstrap(oos_filled, seed=999) if len(oos_filled) >= MIN_BUCKET else {"ok": False, "n_fills": len(oos_filled)}
    E = phase_e_failure(sigs, res)
    F = phase_f_sensitivity(sigs)

    # alt config (improve_bid) bootstrap as a second pre-registered view
    alt_filled = [r for r in _simulate(sigs, ALT) if r["filled"]]
    D_alt = phase_d_bootstrap(alt_filled, seed=7)

    p_pos = D.get("prob_true_ev_positive") if D.get("ok") else None
    answers = _answers(B, C, D, D_oos, F, p_pos, len(filled))
    rep = {
        "ok": True, "generated_at": datetime.utcnow().isoformat(),
        "fixed_config": WIN, "alt_config": ALT,
        "n_signals": len(sigs), "n_fills": len(filled),
        "n_signals_oos": len(oos), "n_fills_oos": len(oos_filled),
        "phase_a_sample": {"markets": _market_count(db), "points": _point_count(db),
                           "note": "dataset rebuilt from all resolved BTC markets (Phase A); ETH/SOL up-or-down "
                                   "confirmed absent (BTC-only indexer + 0 Gamma matches); 5m+15m only, no hourly"},
        "phase_b_stability": B, "phase_c_walkforward": C,
        "phase_d_bootstrap": D, "phase_d_bootstrap_oos": D_oos, "phase_d_bootstrap_alt": D_alt,
        "phase_e_failure": E, "phase_f_sensitivity": F,
        **answers, "safety": ex._safety(),
    }
    _store(db, rep)
    return rep


def _answers(B, C, D, D_oos, F, p_pos, n_fills) -> dict:
    boot_ok = D.get("ok")
    strong = bool(boot_ok and p_pos is not None and p_pos >= 0.95)
    moderate = bool(boot_ok and p_pos is not None and 0.85 <= p_pos < 0.95)
    stable = B["fraction_positive"] >= 0.7 and F["neighbourhood_fraction_positive"] >= 0.7
    wf_ok = C["folds_usable"] > 0 and C["folds_positive"] / max(1, C["folds_usable"]) >= 0.6

    if strong and stable and wf_ok:
        code, verdict = 1, "Edge is real and robust — justifies a dedicated paper market-maker"
    elif (moderate or strong) and (stable or wf_ok):
        code, verdict = 2, "Edge is probably real but sample still thin — build the paper maker to keep validating"
    elif boot_ok and p_pos is not None and p_pos >= 0.6:
        code, verdict = 3, "Suggestive but not confident enough — needs more fills before committing"
    else:
        code, verdict = 4, "Not supported after expanded sample — do not build"

    return {
        "verdict_code": code, "verdict": verdict,
        "answers": {
            "1_is_edge_real": ("likely yes" if code <= 2 else ("uncertain" if code == 3 else "no")),
            "2_prob_true_ev_positive": p_pos,
            "2_prob_true_ev_positive_oos": D_oos.get("prob_true_ev_positive") if D_oos.get("ok") else None,
            "3_confidence_after_expansion": (
                f"{n_fills} fills (was ~8–12); P(EV>0)={p_pos}; stability {B['fraction_positive']}; "
                f"nearby-settings positive {F['neighbourhood_fraction_positive']}"),
            "4_justifies_paper_maker": code <= 2,
        },
    }


def _store(db: Session, rep: dict) -> None:
    st = lab._state(db)
    base = dict(st.execution) if isinstance(st.execution, dict) else {}
    base["validation"] = rep
    st.execution = base
    st.execution_built_at = datetime.utcnow()
    db.commit()


def validation_status(db: Session) -> dict:
    st = lab._state(db)
    return {"validation": (st.execution or {}).get("validation") if isinstance(st.execution, dict) else None,
            "safety": ex._safety()}
