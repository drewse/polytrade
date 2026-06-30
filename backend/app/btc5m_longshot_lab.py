"""BTC 5M Longshot / Value Lab — research/paper ONLY.

The decisive experiment. Our directional backtests said "no edge," yet 12/12 real
wallets trading these markets are profitable, all systematically buying BELOW 0.50
(avg entry ~0.43). This tests the strategy they actually run — **buy the CHEAP side
and hold to resolution** (favorite-longshot / overreaction-reversion value making) —
directly on our own 419-market / 2,000+ point dataset, where we finally have the
sample size the passive-maker fill experiments lacked.

It answers three questions:
  1. Is the market MIS-CALIBRATED — does the cheap side resolve in its favor MORE
     often than its price implies? (pure mispricing, full sample, high power)
  2. Does buying the cheap side have positive EV at the MID, as a MAKER (realistic
     worst-case fills + spread capture), and as a TAKER (control — should lose)?
  3. At what entry threshold is it best, and does ~0.43 (the wallets' avg) work?

100% read-only research: reads btc5m_* tables + simulates fills from the historical
trade stream. NEVER places orders or touches live execution / bankroll.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import btc5m_execution_lab as ex
from . import btc5m_maker_validation as mv
from . import btc5m_models as bm
from . import btc5m_strategy_models as lm
from . import btc5m_longshot_models as lsm

_mean = ex._mean
_std = ex._std
_clip = ex._clip
SLIPPAGE = 0.01
WALLET_AVG_ENTRY = 0.43          # measured avg entry of the 12 profitable DREW FINDS wallets


# ---------------------------------------------------------------------------
# signal construction — cheap side + the forward trade stream for maker fills
# ---------------------------------------------------------------------------
def _signals(db: Session) -> list[dict]:
    rows = [r for r in db.scalars(select(lm.Btc5mLabPoint)).all()
            if r.pm_yes is not None and r.label_up is not None]
    trades_by = ex._trades_by_market(db, {r.market_id for r in rows})
    times = ex._market_times(db, {r.market_id for r in rows})
    out = []
    for r in rows:
        m = r.pm_yes
        if m <= 0.01 or m >= 0.99:
            continue
        side = "YES" if m < 0.5 else "NO"          # the cheaper side
        cheap_price = round(min(m, 1 - m), 4)
        half = (r.spread or 0.0) / 2 or 0.01
        t = r.t_offset_s or 0
        life_left = r.secs_to_expiry or 0
        future = [((tr.seconds_from_creation or 0) - t, ex._yes_price(tr), float(tr.usd_value or 0.0))
                  for tr in trades_by.get(r.market_id, []) if 0 < (tr.seconds_from_creation or 0) - t <= life_left]
        ct = times.get(r.market_id)
        out.append({"market_id": r.market_id, "t": t, "mid": m, "half": half, "side": side,
                    "up": bool(r.label_up), "model_prob": m, "cheap_price": cheap_price,
                    "pm_yes": m, "regime": r.regime or "?", "duration_minutes": r.duration_minutes or 5,
                    "secs_to_expiry": life_left, "btc_vol": 0.0, "volume_usd": 0.0,
                    "week": (ct.strftime("%Y-W%W") if ct else "?"), "future": future})
    return out


def _win(s: dict) -> bool:
    return s["up"] if s["side"] == "YES" else (not s["up"])


# ---------------------------------------------------------------------------
# 1) calibration — is the cheap side mispriced? (full sample, high power)
# ---------------------------------------------------------------------------
def calibration_curve(sigs: list[dict], bins: int = 10) -> dict:
    """Bin by implied UP probability (pm_yes) and compare to the ACTUAL up-rate. If the
    curve regresses toward 0.5 (cheap sides resolve in their favor more than priced),
    buying the cheap side is +EV. Reported as the under/over-pricing per bin."""
    rows = []
    cheap_edge = []     # per point: actual_win - price_paid (cheap side at mid)
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        seg = [s for s in sigs if lo <= s["pm_yes"] < hi or (b == bins - 1 and s["pm_yes"] >= hi - 1e-9)]
        if len(seg) < 10:
            continue
        implied = _mean([s["pm_yes"] for s in seg])
        actual = _mean([1.0 if s["up"] else 0.0 for s in seg])
        rows.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(seg), "implied_up": round(implied, 4),
                     "actual_up": round(actual, 4), "mispricing": round(actual - implied, 4)})
    for s in sigs:
        cheap_edge.append((1.0 if _win(s) else 0.0) - s["cheap_price"])
    # "reversion" = do extremes regress toward 0.5? slope of actual vs implied (<1 ⇒ reversion)
    xs = [r["implied_up"] for r in rows]
    ys = [r["actual_up"] for r in rows]
    slope = _slope_xy(xs, ys)
    return {"bins": rows, "calibration_slope": round(slope, 3),
            "cheap_side_edge_at_mid": round(_mean(cheap_edge), 5),
            "interpretation": "slope<1 ⇒ market over-reacts/regresses to 0.5 ⇒ cheap side underpriced ⇒ "
                              "buying the cheap side is +EV; cheap_side_edge_at_mid is that edge per $1 at the mid"}


def _slope_xy(xs, ys):
    n = len(xs)
    if n < 2:
        return 1.0
    mx, my = _mean(xs), _mean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return (sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / den) if den else 1.0


# ---------------------------------------------------------------------------
# 2) cheap-side backtest — mid / maker / taker, with significance + bootstrap
# ---------------------------------------------------------------------------
def _entry(s: dict, execution: str, *, queue: str):
    """Return (filled, entry_price). mid = no cost (pure mispricing); maker = rest a
    worst-case-queue bid (fills from the trade stream, captures spread); taker = pay
    the ask + slippage (control)."""
    cp = s["cheap_price"]
    if execution == "mid":
        return True, cp
    if execution == "taker":
        return True, _clip(cp + s["half"] + SLIPPAGE, 0.01, 0.99)
    r = ex.simulate_queue(s, "join_bid", timeout=5, mode=queue)   # maker
    return (r["filled"], r["entry"]) if r["filled"] else (False, None)


def backtest(sigs: list[dict], *, execution: str = "mid", max_entry: float = 0.5,
             queue: str = "worst") -> dict:
    pnls, costs, wins, weeks, regimes = [], [], [], [], []
    for s in sigs:
        if s["cheap_price"] > max_entry:
            continue
        filled, entry = _entry(s, execution, queue=queue)
        if not filled:
            continue
        win = _win(s)
        pnls.append((1.0 - entry) if win else -entry)
        costs.append(entry); wins.append(1 if win else 0)
        weeks.append(s["week"]); regimes.append(s["regime"])
    n = len(pnls)
    if n == 0:
        return {"execution": execution, "max_entry": max_entry, "n": 0}
    boot = mv.phase_d_bootstrap([{"pnl": p, "spread_captured": 0.0} for p in pnls]) if n >= 4 else {"ok": False}
    mean = _mean(pnls); sd = _std(pnls)
    se = sd / (n ** 0.5) if n > 1 else 0.0
    t = (mean / se) if se else (99.0 if mean > 0 else 0.0)
    return {
        "execution": execution, "max_entry": max_entry, "queue": queue if execution == "maker" else None,
        "n": n, "ev_per_trade": round(mean, 5), "win_rate": round(_mean(wins), 4),
        "avg_entry_price": round(_mean(costs), 4), "roi": round(sum(pnls) / sum(costs), 4) if sum(costs) else 0.0,
        "t_stat": round(t, 3), "ci95": boot.get("ev_per_fill", {}).get("ci95") if boot.get("ok") else None,
        "prob_ev_positive": boot.get("prob_true_ev_positive") if boot.get("ok") else None,
        "significant": bool(n >= 30 and mean > 0 and t >= 1.96),
        "weeks": len(set(weeks)), "n_regimes_positive": _regime_pos(pnls, regimes),
    }


def _regime_pos(pnls, regimes) -> str:
    g: dict = {}
    for p, r in zip(pnls, regimes):
        g.setdefault(r, []).append(p)
    pos = sum(1 for v in g.values() if _mean(v) > 0)
    return f"{pos}/{len(g)}"


# ---------------------------------------------------------------------------
# orchestration + verdict
# ---------------------------------------------------------------------------
def run(db: Session) -> dict:
    sigs = _signals(db)
    if len(sigs) < 50:
        rep = {"ok": False, "error": "dataset too small", "n_signals": len(sigs)}
        _store(db, rep)
        return rep
    calib = calibration_curve(sigs)
    # grid: execution × entry threshold
    grid = []
    for ex_mode in ("mid", "maker", "taker"):
        for thr in (0.50, 0.45, 0.40, 0.35, 0.30):
            grid.append(backtest(sigs, execution=ex_mode, max_entry=thr))
    # headline cells
    mid_all = next(g for g in grid if g["execution"] == "mid" and g["max_entry"] == 0.50)
    mid_wallet = next(g for g in grid if g["execution"] == "mid" and g["max_entry"] == 0.45)   # ~wallet zone
    maker_wallet = next(g for g in grid if g["execution"] == "maker" and g["max_entry"] == 0.45)
    taker_all = next(g for g in grid if g["execution"] == "taker" and g["max_entry"] == 0.50)

    report = {
        "ok": True, "generated_at": datetime.utcnow().isoformat(), "n_signals": len(sigs),
        "calibration": calib, "grid": grid,
        "headline_cells": {"mid_all": mid_all, "mid_cheap": mid_wallet, "maker_cheap": maker_wallet, "taker_all": taker_all},
        "wallet_benchmark": {"avg_entry": WALLET_AVG_ENTRY, "profitable_wallets": 12,
                             "note": "the 12 DREW FINDS wallets buy at avg ~0.43 and are all net-profitable"},
        **_verdict(calib, mid_all, mid_wallet, maker_wallet, taker_all),
        "safety": "research/paper only — backtests buying the cheap side; never places orders",
    }
    _store(db, report)
    return report


def _verdict(calib, mid_all, mid_cheap, maker_cheap, taker_all) -> dict:
    edge = calib["cheap_side_edge_at_mid"]
    mispriced = edge > 0.005 and calib["calibration_slope"] < 0.95
    mid_sig = mid_cheap.get("significant") or mid_all.get("significant")
    maker_pos = (maker_cheap.get("ev_per_trade") or 0) > 0
    if maker_pos and maker_cheap.get("significant"):
        code, verdict = 1, "Tradeable: cheap-side value-making is +EV as a maker, significant"
    elif mispriced and mid_sig:
        code, verdict = 2, "Real mispricing: cheap side is underpriced (significant at mid), but maker fills/costs need work"
    elif mispriced:
        code, verdict = 3, "Suggestive mispricing, not yet significant"
    else:
        code, verdict = 4, "No cheap-side mispricing in our data — the wallets' edge is NOT longshot bias (likely execution/latency)"
    headline = (f"cheap-side edge at mid {edge:+.4f}/share (calibration slope {calib['calibration_slope']}); "
                f"mid<0.45 EV {mid_cheap.get('ev_per_trade')} (t={mid_cheap.get('t_stat')}, n={mid_cheap.get('n')}); "
                f"maker<0.45 EV {maker_cheap.get('ev_per_trade')} (n={maker_cheap.get('n')}); "
                f"taker EV {taker_all.get('ev_per_trade')} (control)")
    return {"verdict_code": code, "verdict": verdict, "headline": headline}


def _state(db: Session) -> lsm.Btc5mLongshotState:
    st = db.get(lsm.Btc5mLongshotState, 1)
    if st is None:
        st = lsm.Btc5mLongshotState(id=1)
        db.add(st)
        db.commit()
    return st


def _store(db: Session, rep: dict) -> None:
    st = _state(db)
    st.report = rep
    st.built_at = datetime.utcnow()
    db.commit()


def status(db: Session) -> dict:
    st = _state(db)
    return {"report": st.report, "built_at": st.built_at.isoformat() if st.built_at else None,
            "safety": "BTC 5M Longshot/Value Lab — research/paper only; never trades"}
