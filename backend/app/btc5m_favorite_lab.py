"""BTC 5M Favorite-Value / Under-reaction Lab — research/paper ONLY (Phase-0).

The longshot lab tested buying the CHEAP side (rejected, -EV) but its calibration curve
revealed the OPPOSITE, never-backtested signal: the market UNDER-reacts (slope > 1) — the
FAVORITE (the side priced > 0.5) resolves in its favor MORE often than priced (+5 to +11
pts in the 0.55-0.75 band). This module decides whether that is a real, durable, tradeable
edge or merely a short-sample UP-drift artifact.

It is deliberately conservative:
  * ONE independent trade per market (first decision point whose favorite enters the band),
    held to resolution — so trades are independent (no signal-correlation inflation).
  * MARKET-level bootstrap for confidence.
  * UP-favorite vs DOWN-favorite split — the decisive confound control. A genuine
    under-reaction edge must be >=0 on BOTH; a pure UP-drift shows up only on UP-favorites.
  * An "always buy UP" control strategy run through the identical harness, to size how much
    of any favorite edge is just directional drift.
  * Net EV after realistic spread + slippage (data spread, and fixed-cost sensitivity).
  * OOS via the dataset's chronological market-level train/val/holdout split.

100% read-only: reads btc5m_* tables + simulates fills from the historical trade stream.
NEVER places orders, never touches the live executor / bankroll / copy trading.
"""
from __future__ import annotations

import math
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import btc5m_execution_lab as ex
from . import btc5m_strategy_models as lm
from . import btc5m_longshot_models as lsm

_mean = ex._mean
_std = ex._std
_clip = ex._clip
SLIPPAGE = 0.01                         # taker slippage buffer on top of the half-spread
BAND = (0.55, 0.75)                     # the favorite mispricing sweet-spot from calibration


# ---------------------------------------------------------------------------
# dataset — every decision point, with everything the sims + splits need
# ---------------------------------------------------------------------------
def _points(db: Session) -> list[dict]:
    rows = [r for r in db.scalars(select(lm.Btc5mLabPoint)).all()
            if r.pm_yes is not None and r.label_up is not None]
    trades_by = ex._trades_by_market(db, {r.market_id for r in rows})
    times = ex._market_times(db, {r.market_id for r in rows})
    pts = []
    for r in rows:
        m = r.pm_yes
        if m <= 0.01 or m >= 0.99:
            continue
        fav_is_up = m > 0.5
        fav_side = "YES" if fav_is_up else "NO"
        fav_price = round(max(m, 1 - m), 4)
        t = r.t_offset_s or 0
        life_left = r.secs_to_expiry or 0
        future = [((tr.seconds_from_creation or 0) - t, ex._yes_price(tr), float(tr.usd_value or 0.0))
                  for tr in trades_by.get(r.market_id, []) if 0 < (tr.seconds_from_creation or 0) - t <= life_left]
        ct = times.get(r.market_id)
        pts.append({
            "market_id": r.market_id, "t": t, "mid": m, "half": (r.spread or 0.0) / 2 or 0.01,
            "side": fav_side, "up": bool(r.label_up), "model_prob": m,
            "fav_is_up": fav_is_up, "fav_price": fav_price,
            "regime": r.regime or "?", "duration_minutes": r.duration_minutes or 5,
            "secs_to_expiry": life_left, "btc_ret_30s": r.btc_ret_30s,
            "split": r.split or "?", "week": (ct.strftime("%Y-W%W") if ct else "?"),
            "btc_vol": 0.0, "volume_usd": 0.0, "future": future,
        })
    return pts


def _win(p: dict) -> bool:
    """Did the FAVORITE side resolve in its favour?"""
    return (p["up"] if p["side"] == "YES" else (not p["up"]))


# ---------------------------------------------------------------------------
# one independent trade per market: first decision point in-band (or by entry rule)
# ---------------------------------------------------------------------------
def _trades(pts: list[dict], *, band=BAND, entry="first", side_filter=None,
            force_side=None) -> list[dict]:
    """Collapse decision points to ONE trade per market.

    band         — keep points whose favorite price is in [lo, hi].
    entry        — 'first' (earliest in-band), 'late' (latest in-band), or an int target
                   secs_to_expiry (closest in-band point to that time-left).
    side_filter  — 'up' | 'down' to keep only UP- or DOWN-favorite markets.
    force_side   — 'UP' control: ignore favorite, always take the UP/YES side at its price
                   (used to size directional drift). Band then applies to the YES price.
    """
    by_mkt: dict = {}
    lo, hi = band
    for p in pts:
        if force_side == "UP":
            price = p["mid"]                      # buying YES/UP at its own price
            is_up = True
        else:
            price = p["fav_price"]
            is_up = p["fav_is_up"]
        if not (lo <= price <= hi):
            continue
        if side_filter == "up" and not p["fav_is_up"]:
            continue
        if side_filter == "down" and p["fav_is_up"]:
            continue
        by_mkt.setdefault(p["market_id"], []).append((p, price, is_up))
    trades = []
    for mid, cand in by_mkt.items():
        if entry == "first":
            chosen = min(cand, key=lambda c: c[0]["t"])
        elif entry == "late":
            chosen = max(cand, key=lambda c: c[0]["t"])
        else:                                     # closest to a target secs_to_expiry
            chosen = min(cand, key=lambda c: abs((c[0]["secs_to_expiry"] or 0) - int(entry)))
        p, price, is_up = chosen
        trades.append({**p, "_price": price, "_is_up": is_up,
                       "_win": (p["up"] if is_up else (not p["up"]))})
    return trades


# ---------------------------------------------------------------------------
# execution entries
# ---------------------------------------------------------------------------
def _entry_price(tr: dict, execution: str, *, slip=SLIPPAGE, fixed_cost=None):
    """Return (filled, entry_price) for a chosen trade under an execution model.
    mid   = no cost (pure mispricing).
    taker = pay ask: price + half_spread + slip (or price + fixed_cost if given).
    maker = rest a worst-case-queue bid on the favorite side (spread capture, lower fill)."""
    price = tr["_price"]
    if execution == "mid":
        return True, price
    if execution == "taker":
        c = fixed_cost if fixed_cost is not None else (tr["half"] + slip)
        return True, _clip(price + c, 0.01, 0.99)
    # maker on the favourite side — reuse the queue sim with side set to the favourite
    sig = {**tr, "side": "YES" if tr["_is_up"] else "NO"}
    r = ex.simulate_queue(sig, "join_bid", timeout=5, mode="worst")
    return (r["filled"], r["entry"]) if r["filled"] else (False, None)


def _pnls(trades: list[dict], execution: str, **kw) -> tuple[list, list, list, list]:
    """Return (pnls, entries, wins, market_ids) for filled trades."""
    pnls, entries, wins, mids = [], [], [], []
    for tr in trades:
        filled, entry = _entry_price(tr, execution, **kw)
        if not filled:
            continue
        win = tr["_win"]
        pnls.append((1.0 - entry) if win else -entry)
        entries.append(entry); wins.append(1 if win else 0); mids.append(tr["market_id"])
    return pnls, entries, wins, mids


# ---------------------------------------------------------------------------
# statistics — market-clustered bootstrap (trades are 1/market ⇒ already independent,
# but we keep the cluster form so it's correct if multiple trades/market are ever passed)
# ---------------------------------------------------------------------------
def _boot(pnls: list[float], mids: list[str], *, iters=4000, seed=7) -> dict:
    n = len(pnls)
    if n < 8:
        return {"ev": round(_mean(pnls), 5) if pnls else None, "ci95": None,
                "prob_ev_positive": None, "n": n}
    clusters: dict = {}
    for p, mid in zip(pnls, mids):
        clusters.setdefault(mid, []).append(p)
    keys = list(clusters.keys())
    # deterministic LCG so results are reproducible without Math.random / Date
    state = seed
    def rnd():
        nonlocal state
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        return state / 0x7FFFFFFF
    means = []
    K = len(keys)
    for _ in range(iters):
        s = c = 0
        for _ in range(K):
            blk = clusters[keys[int(rnd() * K) % K]]
            s += sum(blk); c += len(blk)
        means.append(s / c if c else 0.0)
    means.sort()
    ev = _mean(pnls)
    return {"ev": round(ev, 5), "ci95": [round(means[int(0.025 * iters)], 4), round(means[int(0.975 * iters)], 4)],
            "prob_ev_positive": round(sum(1 for x in means if x > 0) / iters, 4), "n": n}


def _summary(trades: list[dict], execution="taker", **kw) -> dict:
    pnls, entries, wins, mids = _pnls(trades, execution, **kw)
    n = len(pnls)
    if n == 0:
        return {"n_trades": 0, "n_markets": 0}
    sd = _std(pnls); se = sd / (n ** 0.5) if n > 1 else 0.0
    b = _boot(pnls, mids)
    return {
        "n_trades": n, "n_markets": len(set(mids)),
        "win_rate": round(_mean(wins), 4), "avg_entry_price": round(_mean(entries), 4),
        "ev_per_trade": round(_mean(pnls), 5), "roi": round(sum(pnls) / sum(entries), 4) if sum(entries) else 0.0,
        "stdev": round(sd, 4), "t_stat": round(_mean(pnls) / se, 3) if se else None,
        "ci95": b["ci95"], "prob_ev_positive": b["prob_ev_positive"],
        "total_pnl": round(sum(pnls), 4),
    }


def _distribution(trades: list[dict], execution="taker", **kw) -> dict:
    pnls, _, _, _ = _pnls(trades, execution, **kw)
    if not pnls:
        return {}
    s = sorted(pnls)
    q = lambda f: round(s[min(len(s) - 1, int(f * len(s)))], 4)
    wins = [p for p in pnls if p > 0]; losses = [p for p in pnls if p <= 0]
    return {"n": len(pnls), "min": round(s[0], 4), "p10": q(.1), "p25": q(.25), "median": q(.5),
            "p75": q(.75), "p90": q(.9), "max": round(s[-1], 4),
            "avg_win": round(_mean(wins), 4) if wins else None, "avg_loss": round(_mean(losses), 4) if losses else None,
            "n_win": len(wins), "n_loss": len(losses),
            "profit_factor": round(sum(wins) / -sum(losses), 3) if losses and sum(losses) < 0 else None}


def _split_by(trades: list[dict], keyfn, execution="taker", **kw) -> dict:
    g: dict = {}
    for tr in trades:
        g.setdefault(keyfn(tr), []).append(tr)
    return {str(k): _summary(v, execution, **kw) for k, v in sorted(g.items(), key=lambda kv: str(kv[0]))}


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------
def run(db: Session) -> dict:
    pts = _points(db)
    if len(pts) < 50:
        rep = {"ok": False, "error": "dataset too small", "n_points": len(pts)}
        _store(db, rep); return rep

    base = _trades(pts, band=BAND, entry="first")                 # primary: favorite, 0.55-0.75, first in-band
    n_markets_total = len({p["market_id"] for p in pts})

    def trend_align(tr):                                          # does favourite align with recent BTC move?
        b = tr.get("btc_ret_30s")
        if b is None or abs(b) < 1e-9:
            return "flat"
        return "aligned" if ((b > 0) == tr["_is_up"]) else "against"

    report = {
        "ok": True, "generated_at": datetime.utcnow().isoformat(),
        "dataset": {"lab_points": len(pts), "markets_total": n_markets_total,
                    "band": list(BAND), "entry_rule": "first in-band decision point, hold to resolution",
                    "weeks": sorted({p["week"] for p in pts})},
        "headline": {
            "mid_gross": _summary(base, "mid"),
            "taker_net_dataspread": _summary(base, "taker"),       # realistic net EV
        },
        "distribution_taker": _distribution(base, "taker"),
        "up_vs_down": {                                            # THE confound control
            "up_favorite": _summary(_trades(pts, band=BAND, entry="first", side_filter="up"), "taker"),
            "down_favorite": _summary(_trades(pts, band=BAND, entry="first", side_filter="down"), "taker"),
            "up_favorite_mid": _summary(_trades(pts, band=BAND, entry="first", side_filter="up"), "mid"),
            "down_favorite_mid": _summary(_trades(pts, band=BAND, entry="first", side_filter="down"), "mid"),
        },
        "control_always_up": _summary(_trades(pts, band=BAND, entry="first", force_side="UP"), "taker"),
        "oos_split": _split_by(base, lambda t: t["split"], "taker"),
        "by_week": _split_by(base, lambda t: t["week"], "taker"),
        "by_regime": _split_by(base, lambda t: t["regime"], "taker"),
        "by_trend_alignment": _split_by(base, trend_align, "taker"),
        "sensitivity": {
            "band": {f"{lo}-{hi}": _summary(_trades(pts, band=(lo, hi), entry="first"), "taker")
                     for lo, hi in [(0.50, 0.60), (0.55, 0.65), (0.60, 0.70), (0.65, 0.75), (0.55, 0.75), (0.50, 0.80)]},
            "entry_time": {rule: _summary(_trades(pts, band=BAND, entry=rule), "taker")
                           for rule in ["first", "late", 240, 120, 30]},
            "fixed_cost": {f"{int(c*100)}c": _summary(base, "taker", fixed_cost=c)
                           for c in [0.0, 0.005, 0.01, 0.015, 0.02]},
            "execution": {exm: _summary(base, exm) for exm in ["mid", "taker", "maker"]},
        },
        "baselines": {
            "favorite_taker": _summary(base, "taker"),
            "cheap_side_taker": _summary(_trades(pts, band=(0.0, 0.45), entry="first"), "taker"),  # inverse (should lose)
            "always_up_taker": _summary(_trades(pts, band=BAND, entry="first", force_side="UP"), "taker"),
            "passive_maker_paper_ref": {"ev_per_fill": 0.058, "prob_ev_positive": 0.637,
                                        "note": "from forward paper test, join_bid/5s/worst, n=10 fills"},
        },
    }
    report["gates"] = _gates(report)
    report.update(_verdict(report))
    report["safety"] = "research/paper only — backtests the favorite/under-reaction signal; never places orders"
    _store(db, report)
    return report


def _gates(rep: dict) -> dict:
    taker = rep["headline"]["taker_net_dataspread"]
    up = rep["up_vs_down"]["up_favorite"]; dn = rep["up_vs_down"]["down_favorite"]
    oos = rep["oos_split"].get("holdout", {})
    weeks = rep["by_week"]
    week_evs = [v.get("ev_per_trade") for v in weeks.values() if v.get("n_trades", 0) >= 10]
    g = {
        "1_net_ev_after_costs_positive": bool((taker.get("ev_per_trade") or -1) > 0),
        "2_out_of_sample_positive": bool((oos.get("ev_per_trade") or -1) > 0 and (oos.get("n_trades") or 0) >= 20),
        "3_up_and_down_both_nonneg": bool((up.get("ev_per_trade") or -1) >= 0 and (dn.get("ev_per_trade") or -1) >= 0),
        "4_robust_across_periods": bool(len(week_evs) >= 2 and all(e is not None and e > 0 for e in week_evs)),
        "5_statistical_confidence": bool((taker.get("prob_ev_positive") or 0) >= 0.95
                                         and (taker.get("ci95") or [-1, -1])[0] > 0),
    }
    g["passed"] = sum(1 for k, v in g.items() if k.startswith(("1", "2", "3", "4", "5")) and v)
    g["total"] = 5
    return g


def _verdict(rep: dict) -> dict:
    g = rep["gates"]
    up = rep["up_vs_down"]["up_favorite"].get("ev_per_trade")
    dn = rep["up_vs_down"]["down_favorite"].get("ev_per_trade")
    drift = rep["control_always_up"].get("ev_per_trade")
    fav = rep["headline"]["taker_net_dataspread"].get("ev_per_trade")
    if g["passed"] == 5:
        code, v = 1, "PASS — durable favorite/under-reaction edge survives costs, OOS, UP/DOWN, and time; justifies a small live trial"
    elif g["1_net_ev_after_costs_positive"] and not g["3_up_and_down_both_nonneg"]:
        code, v = 4, "REJECT — net edge is concentrated on UP-favorites (DOWN-favorite not >=0); this is UP-drift, not a durable under-reaction edge"
    elif g["passed"] >= 3:
        code, v = 2, "PARTIAL — promising but at least one gate fails; not yet justifying a live trial"
    else:
        code, v = 3, "REJECT — no net edge after costs"
    headline = (f"favorite taker EV {fav}; UP-fav {up} vs DOWN-fav {dn}; always-UP control {drift}; "
                f"gates {g['passed']}/5")
    return {"verdict_code": code, "verdict": v, "verdict_headline": headline}


# ---------------------------------------------------------------------------
def _state(db: Session) -> lsm.Btc5mLongshotState:
    st = db.get(lsm.Btc5mLongshotState, 1)
    if st is None:
        st = lsm.Btc5mLongshotState(id=1); db.add(st); db.commit()
    return st


def _store(db: Session, rep: dict) -> None:
    st = _state(db)
    st.favorite_report = rep
    st.favorite_built_at = datetime.utcnow()
    db.commit()


def status(db: Session) -> dict:
    st = _state(db)
    rep = getattr(st, "favorite_report", None)
    return {"report": rep, "built_at": getattr(st, "favorite_built_at", None).isoformat()
            if getattr(st, "favorite_built_at", None) else None,
            "safety": "BTC 5M Favorite/Under-reaction Lab — research/paper only; never trades"}
