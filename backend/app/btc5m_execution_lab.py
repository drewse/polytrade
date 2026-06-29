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

from sqlalchemy import func, select
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


def build_signals(db: Session, split: str, model, feats: list[str], *, min_edge: float = 0.01,
                  max_future: float | None = None) -> list[dict]:
    """One signal per decision point where the model takes a directional view. Carries
    the market mid/half-spread, the chosen side, the outcome, and the subsequent trade
    stream (delay seconds, yes-price) used to decide passive fills. `max_future` caps
    the captured trade horizon (seconds; None = the rest of the market's life — needed
    to simulate long resting windows up to full market life)."""
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
        life_left = r.secs_to_expiry or 0
        horizon = life_left if max_future is None else min(max_future, life_left)
        future = []
        for tr in trades_by.get(r.market_id, []):
            d = (tr.seconds_from_creation or 0) - t
            if 0 < d <= horizon:
                future.append((d, _yes_price(tr), float(tr.usd_value or 0.0)))   # carry trade SIZE for queue sim
        signals.append({
            "market_id": r.market_id, "t": t, "mid": m, "half": half, "side": side,
            "model_prob": p, "edge": abs(edge), "up": bool(r.label_up),
            "regime": r.regime or "?", "secs_to_expiry": life_left,
            "duration_minutes": r.duration_minutes or 5,
            "btc_vol": _num(f.get("btc_vol")), "volume_usd": _num(f.get("volume_usd")),
            "flow_imbalance": _num(f.get("flow_imbalance")), "btc_ret_sofar": _num(f.get("btc_ret_sofar")),
            "spread_w": 2 * half, "has_large_trade": _num(f.get("has_large_trade")),
            "lag": _num(f.get("lag")), "hour": int(_num(f.get("hour"))),
            "future": future,
        })
    return signals


# ---------------------------------------------------------------------------
# execution methods — each returns (filled, entry_price, fill_delay or None)
# ---------------------------------------------------------------------------
def _first_cross(sig: dict, target: float, *, up_cross: bool, timeout: float) -> float | None:
    """Delay (s) of the first subsequent trade that crosses `target`. up_cross=True ⇒
    a NO bid needs yes-price to rise to target; False ⇒ a YES bid needs it to fall.
    This is the BEST-CASE (touch / front-of-queue) fill."""
    for e in sig["future"]:
        d, yp = e[0], e[1]
        if d > timeout:
            break
        if (up_cross and yp >= target) or (not up_cross and yp <= target):
            return d
    return None


# --- queue-position models (NO L2 book in the data → bracket reality) --------
# best  : touch-fill — we are at the FRONT of the queue, fill on first touch.
# mid   : proportional — fill only after `queue_ahead_usd` of volume clears at our
#         level (one typical resting order ahead of us), then our 5-share order fills.
# worst : behind-all — fill only when a trade prints THROUGH our price (≥1 tick past),
#         i.e. the whole level (including us) was swept.
QUEUE_MODES = ("best", "mid", "worst")


def _queue_fill(sig: dict, target: float, *, up_cross: bool, timeout: float, mode: str,
                our_usd: float, queue_ahead_usd: float, tick: float = DEFAULT_TICK):
    """Returns (fill_delay or None, fill_fraction). Uses the historical trade SIZE
    stream as a proxy for queue consumption (we have no L2 book)."""
    cum = 0.0
    for e in sig["future"]:
        d, yp = e[0], e[1]
        sz = e[2] if len(e) > 2 else our_usd
        if d > timeout:
            break
        crosses = (up_cross and yp >= target) or (not up_cross and yp <= target)
        through = (up_cross and yp >= target + tick) or (not up_cross and yp <= target - tick)
        if mode == "best":
            if crosses:
                return d, 1.0
        elif mode == "worst":
            if through:
                return d, 1.0
        else:  # mid — cumulative volume must clear the queue ahead, then fill us
            if crosses:
                cum += sz
                if cum >= queue_ahead_usd:
                    frac = min(1.0, max(0.0, (cum - queue_ahead_usd) / our_usd))
                    if frac > 0:
                        return d, frac
    return None, 0.0


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


def _eff_timeout(sig: dict, timeout: float | None) -> float:
    """Resting window in seconds; None ⇒ the full remaining market life."""
    return float(sig["secs_to_expiry"]) if timeout is None else float(timeout)


def simulate_method(sig: dict, method: str, *, timeout: float | None = 2.0, ev_threshold: float = 0.0):
    """Simulate one execution style on one signal. `timeout=None` rests for the full
    market life. Returns a result dict (filled / entry / pnl / delay / spread_captured /
    win / matched), or filled=False (missed)."""
    side, up, m, h = sig["side"], sig["up"], sig["mid"], sig["half"]
    to = _eff_timeout(sig, timeout)
    taker_entry = _taker_entry(sig)
    taker_pnl = _pnl(side, taker_entry, up)
    out = {"side": side, "up": up, "taker_pnl": taker_pnl, "taker_entry": taker_entry,
           "regime": sig["regime"], "btc_vol": sig["btc_vol"], "volume_usd": sig["volume_usd"],
           "secs_to_expiry": sig["secs_to_expiry"], "duration_minutes": sig.get("duration_minutes", 5),
           "mid": m, "filled": False, "entry": None, "pnl": 0.0, "delay": None,
           "spread_captured": 0.0, "win": None, "matched": False}

    if method == "market":
        out.update(filled=True, entry=taker_entry, pnl=taker_pnl, delay=0.0,
                   win=taker_pnl > 0, spread_captured=round(m - taker_entry, 4))
        return out

    # two-sided market making: rest BOTH a YES bid (m-h) and a YES ask (m+h), ignore the
    # model side. If BOTH cross within the window -> matched, pure spread capture (2h),
    # direction-neutral. If only one crosses -> hold that (adversely selected) inventory
    # to resolution. If neither -> no fill.
    if method == "two_sided":
        ybid = _clip(m - h, 0.01, 0.99)
        yask = _clip(m + h, 0.01, 0.99)
        d_bid = _first_cross(sig, ybid, up_cross=False, timeout=to)   # someone sells to our bid
        d_ask = _first_cross(sig, yask, up_cross=True, timeout=to)    # someone buys our ask
        if d_bid is not None and d_ask is not None:
            out.update(filled=True, matched=True, entry=round((ybid + yask) / 2, 4),
                       pnl=round(yask - ybid, 4), win=True, delay=max(d_bid, d_ask),
                       spread_captured=round(yask - ybid, 4))      # captured full spread, neutral
        elif d_bid is not None:                                    # left holding YES bought at bid
            pnl = _pnl("YES", ybid, up)
            out.update(filled=True, side="YES", entry=ybid, pnl=pnl, win=pnl > 0,
                       delay=d_bid, spread_captured=round(m - ybid, 4))
        elif d_ask is not None:                                    # left holding NO (sold YES at ask)
            pnl = _pnl("NO", _clip(1 - yask, 0.01, 0.99), up)
            out.update(filled=True, side="NO", entry=_clip(1 - yask, 0.01, 0.99), pnl=pnl,
                       win=pnl > 0, delay=d_ask, spread_captured=round(yask - m, 4))
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
    eff = min(to, 1.0) if method == "adaptive" else to
    delay = _first_cross(sig, target, up_cross=up_cross, timeout=eff)
    if delay is None:
        return out                                              # missed fill
    pnl = _pnl(side, entry, up)
    out.update(filled=True, entry=entry, pnl=pnl, delay=delay, win=pnl > 0,
               spread_captured=round(m - entry if side == "YES" else (1 - m) - entry, 4))
    return out


def simulate_queue(sig: dict, policy: str, *, timeout: float | None, mode: str,
                   our_shares: float = 5.0, queue_ahead_usd: float = 25.0, ev_threshold: float = 0.0):
    """Queue-position-aware simulation for the maker policies. Same result shape as
    simulate_method, plus `frac` (fill fraction) — best/mid/worst bracket where our
    5-share order sits in an unobservable queue."""
    side, up, m, h = sig["side"], sig["up"], sig["mid"], sig["half"]
    to = _eff_timeout(sig, timeout)
    out = {"side": side, "up": up, "taker_pnl": _pnl(side, _taker_entry(sig), up),
           "regime": sig["regime"], "btc_vol": sig["btc_vol"], "volume_usd": sig["volume_usd"],
           "secs_to_expiry": sig["secs_to_expiry"], "duration_minutes": sig.get("duration_minutes", 5),
           "mid": m, "filled": False, "entry": None, "pnl": 0.0, "delay": None,
           "spread_captured": 0.0, "win": None, "matched": False, "frac": 0.0}

    if policy == "two_sided":
        ybid, yask = _clip(m - h, .01, .99), _clip(m + h, .01, .99)
        ub = our_shares * ybid
        db_, fb = _queue_fill(sig, ybid, up_cross=False, timeout=to, mode=mode, our_usd=ub, queue_ahead_usd=queue_ahead_usd)
        da_, fa = _queue_fill(sig, yask, up_cross=True, timeout=to, mode=mode, our_usd=our_shares * yask, queue_ahead_usd=queue_ahead_usd)
        if db_ is not None and da_ is not None:
            out.update(filled=True, matched=True, entry=round((ybid + yask) / 2, 4), pnl=round(yask - ybid, 4),
                       win=True, delay=max(db_, da_), spread_captured=round(yask - ybid, 4), frac=min(fb, fa))
        elif db_ is not None:
            pnl = _pnl("YES", ybid, up)
            out.update(filled=True, side="YES", entry=ybid, pnl=pnl, win=pnl > 0, delay=db_,
                       spread_captured=round(m - ybid, 4), frac=fb)
        elif da_ is not None:
            pnl = _pnl("NO", _clip(1 - yask, .01, .99), up)
            out.update(filled=True, side="NO", entry=_clip(1 - yask, .01, .99), pnl=pnl, win=pnl > 0,
                       delay=da_, spread_captured=round(yask - m, 4), frac=fa)
        return out

    improve = 1 if policy == "improve_bid" else 0
    entry, target, up_cross = _passive_entry(sig, improve_ticks=improve)
    if policy == "fair_value_maker":
        bid_edge = (sig["model_prob"] - entry) if side == "YES" else ((1 - entry) - (1 - sig["model_prob"]))
        if bid_edge <= ev_threshold:
            return out
    eff = min(to, 1.0) if policy == "adaptive" else to
    delay, frac = _queue_fill(sig, target, up_cross=up_cross, timeout=eff, mode=mode,
                              our_usd=our_shares * entry, queue_ahead_usd=queue_ahead_usd)
    if delay is None:
        return out
    pnl = _pnl(side, entry, up)
    out.update(filled=True, entry=entry, pnl=pnl, delay=delay, win=pnl > 0, frac=frac,
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
    # ADVERSE SELECTION: do the FILLED (price-came-to-you) trades win less than the
    # unconditional base rate for that side? Δwin-rate ≈ per-trade EV cost (payoff swing
    # win→loss ≈ 1.0). Matched two-sided fills are direction-neutral → excluded.
    def would_win(r):
        return (r["up"] if r["side"] == "YES" else (not r["up"]))
    uncond = _mean([1 if would_win(r) else 0 for r in results]) if results else 0.0
    dir_fills = [r for r in filled if not r.get("matched")]
    filled_win = _mean([1 if r["win"] else 0 for r in dir_fills]) if dir_fills else 0.0
    adverse = round(uncond - filled_win, 4) if dir_fills else 0.0
    return {
        "signals": n, "fills": nf, "fill_rate": round(nf / n, 4) if n else 0.0,
        "missed_fills": n - nf, "matched_fills": sum(1 for r in filled if r.get("matched")),
        "avg_fill_delay_s": round(_mean([r["delay"] for r in filled if r["delay"] is not None]), 3),
        "avg_fill_price": round(_mean(costs), 4) if costs else None,
        "avg_spread_captured": round(_mean([r["spread_captured"] for r in filled]), 4) if filled else 0.0,
        "uncond_win_rate": round(uncond, 4), "filled_win_rate": round(filled_win, 4),
        "adverse_selection_cost": adverse,
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
            "sweep": (st.execution or {}).get("sweep") if isinstance(st.execution, dict) else None,
            "queue_study": (st.execution or {}).get("queue_study") if isinstance(st.execution, dict) else None,
            "safety": _safety()}


# ---------------------------------------------------------------------------
# REST-WINDOW SWEEP — does per-fill EV survive as resting time (and fills) grow,
# or does informed BTC-led flow pick off longer-rested quotes?
# ---------------------------------------------------------------------------
MARKETS_PER_DAY = {5: 288, 15: 96, 60: 24}        # structural BTC up/down cadence
REST_WINDOWS: list = [5, 15, 30, 60, 120, None]   # seconds; None = full market life
SWEEP_POLICIES = ("join_bid", "improve_bid", "fair_value_maker", "adaptive", "two_sided")
UNIVERSES = {"5m": [5], "5m+15m": [5, 15]}        # hourly not indexed in the dataset
STAKE_USD = 10.0


def _fills_per_day(sigs: list[dict], results: list[dict]) -> float:
    """Operational fills/day = Σ_duration (markets/day · fill_rate_for_that_duration)."""
    by_dur: dict = {}
    for s, r in zip(sigs, results):
        by_dur.setdefault(s["duration_minutes"], [0, 0])
        by_dur[s["duration_minutes"]][0] += 1
        by_dur[s["duration_minutes"]][1] += 1 if r["filled"] else 0
    fpd = 0.0
    for dur, (n, f) in by_dur.items():
        fpd += MARKETS_PER_DAY.get(dur, 0) * (f / n if n else 0.0)
    return round(fpd, 1)


def _sweep_row(uname: str, policy: str, win, sigs: list[dict]) -> dict:
    results = [simulate_method(s, policy, timeout=win) for s in sigs]
    m = _metrics(results)
    fpd = _fills_per_day(sigs, results)
    ev = m["ev_after_cost"]
    # avg hold ≈ half the remaining life of FILLED markets; concurrency + capital
    hold = _mean([r["secs_to_expiry"] * 0.5 for r in results if r["filled"]]) or 0.0
    concurrent = round(fpd * hold / 86400.0, 3)
    return {
        "universe": uname, "policy": policy,
        "rest_window_s": ("full_life" if win is None else win),
        "signals": m["signals"], "fills": m["fills"], "matched_fills": m["matched_fills"],
        "fill_rate": m["fill_rate"], "fills_per_hour": round(fpd / 24, 2), "fills_per_day": fpd,
        "avg_fill_delay_s": m["avg_fill_delay_s"], "avg_fill_price": m["avg_fill_price"],
        "spread_captured": m["avg_spread_captured"], "adverse_selection_cost": m["adverse_selection_cost"],
        "uncond_win_rate": m["uncond_win_rate"], "filled_win_rate": m["filled_win_rate"],
        "ev_per_fill": ev, "ev_per_day": round(fpd * ev, 4), "roi": m["roi"],
        "profit_factor": m["profit_factor"], "max_drawdown": m["max_drawdown"],
        "t_stat": m["t_stat"], "ci": m["ci"], "sharpe": m["sharpe"], "significant": m["significant"],
        "avg_concurrent_positions": concurrent, "capital_required_usd": round(concurrent * STAKE_USD, 2),
    }


def run_rest_window_sweep(db: Session) -> dict:
    """Sweep resting windows × policies × universes, measuring how per-fill EV and
    adverse-selection cost evolve as resting time (and fill count) grow. Replaces the
    remaining assumption in the viability report with measured numbers. Paper/research
    only — nothing is promoted or traded."""
    spec = next(iter(_models_to_test(db)), None)
    if spec is None:
        return {"ok": False, "error": "no model / dataset too small"}
    # full-market-life trade horizon, all splits (maximise the fill sample)
    sigs_all = []
    for s in ("train", "val", "holdout"):
        sigs_all += build_signals(db, s, spec["model"], spec["feats"], max_future=None)
    if len(sigs_all) < MIN_TRADES:
        return {"ok": False, "error": "too few signals", "n": len(sigs_all)}
    durations = sorted({s["duration_minutes"] for s in sigs_all})
    rows = []
    for uname, durs in UNIVERSES.items():
        usigs = [s for s in sigs_all if s["duration_minutes"] in durs]
        if len(usigs) < MIN_TRADES:
            continue
        for policy in SWEEP_POLICIES:
            for win in REST_WINDOWS:
                rows.append(_sweep_row(uname, policy, win, usigs))
    analysis = _sweep_analysis(rows)
    sweep = {"ok": True, "generated_at": datetime.utcnow().isoformat(),
             "universes": list(UNIVERSES.keys()), "durations_present": durations,
             "rest_windows": ["full_life" if w is None else w for w in REST_WINDOWS],
             "policies": list(SWEEP_POLICIES), "markets_per_day": MARKETS_PER_DAY,
             "n_signals": len(sigs_all), "rows": rows, **analysis,
             "approximations": [
                 "fills decided from the historical 1s trade stream over the full resting window",
                 "adverse_selection_cost = unconditional_win_rate − filled_win_rate (≈ EV/fill cost; payoff swing ≈ 1)",
                 "two_sided = rest YES bid + YES ask; both cross ⇒ matched (neutral spread), one ⇒ adverse inventory",
                 "hourly BTC markets are not indexed ⇒ universe limited to 5m and 5m+15m",
                 "fills/day extrapolated via structural cadence (5m 288/day, 15m 96/day), one quote per market",
             ],
             "safety": _safety()}
    # attach onto the stored execution report (don't overwrite the headline run)
    st = _state(db)
    base = st.execution if isinstance(st.execution, dict) else {}
    base = dict(base) if base else {}
    base["sweep"] = sweep
    st.execution = base
    st.execution_built_at = datetime.utcnow()
    db.commit()
    return sweep


def data_availability(db: Session) -> dict:
    """What execution-realism data do we actually have? (Checked, not assumed.)"""
    n_book = db.scalar(select(func.count()).select_from(bm.Btc5mMarket)
                       .where(bm.Btc5mMarket.orderbook_snapshot.isnot(None))) or 0
    durs = sorted({d for (d,) in db.execute(select(lm.Btc5mLabPoint.duration_minutes).distinct()).all() if d})
    return {
        "l2_book_depth": "NONE — orderbook_snapshot is never populated; no time-series book",
        "markets_with_any_book_snapshot": n_book,
        "trade_stream": "YES — 1-second trades with price + size (usd_value) — used as queue proxy",
        "assets": "BTC only (indexer regex matches btc/bitcoin; no ETH/SOL markets indexed)",
        "durations_present_min": durs,
        "hourly_available": 60 in durs,
        "consequence": "queue position cannot be measured directly → bracket with best/mid/worst assumptions; "
                       "sample cannot expand beyond BTC 5m+15m without indexing new markets",
    }


def _fills_per_day_frac(sigs: list[dict], results: list[dict]) -> float:
    """Operational fills/day in full-order-equivalents (scaled by partial-fill fraction)."""
    by_dur: dict = {}
    for s, r in zip(sigs, results):
        by_dur.setdefault(s["duration_minutes"], [0, 0.0])
        by_dur[s["duration_minutes"]][0] += 1
        by_dur[s["duration_minutes"]][1] += (r.get("frac", 1.0) if r["filled"] else 0.0)
    fpd = 0.0
    for dur, (n, feq) in by_dur.items():
        fpd += MARKETS_PER_DAY.get(dur, 0) * (feq / n if n else 0.0)
    return round(fpd, 1)


QUEUE_TIMEOUTS = (1, 2, 3, 5, 8, 10, 15)
QUEUE_POLICIES = ("join_bid", "improve_bid", "fair_value_maker", "adaptive", "two_sided")


def _queue_gate_row(uname, policy, to, mode, sigs, q_usd) -> dict:
    res = [simulate_queue(s, policy, timeout=to, mode=mode, queue_ahead_usd=q_usd) for s in sigs]
    m = _metrics(res)
    fpd = _fills_per_day_frac(sigs, res)
    ev = m["ev_after_cost"]
    hold = _mean([r["secs_to_expiry"] * 0.5 for r in res if r["filled"]]) or 0.0
    conc = round(fpd * hold / 86400.0, 3)
    return {"universe": uname, "policy": policy, "timeout_s": to, "queue": mode,
            "quote_opportunities": m["signals"], "fills": m["fills"], "fill_rate": m["fill_rate"],
            "fills_per_day": fpd, "ev_per_fill": ev, "ev_per_day": round(fpd * ev, 4), "roi": m["roi"],
            "profit_factor": m["profit_factor"], "sharpe": m["sharpe"], "max_drawdown": m["max_drawdown"],
            "t_stat": m["t_stat"], "ci": m["ci"], "significant": m["significant"],
            "adverse_selection_cost": m["adverse_selection_cost"], "spread_captured": m["avg_spread_captured"],
            "avg_concurrent_positions": conc, "capital_required_usd": round(conc * STAKE_USD, 2),
            "avg_fill_delay_s": m["avg_fill_delay_s"]}


def _queue_breakdown(sigs, q_usd) -> dict:
    """Regime breakdown for the headline config (join_bid, 5s) under best vs worst queue."""
    def buckets(keyfn, mode):
        res = [(s, simulate_queue(s, "join_bid", timeout=5, mode=mode, queue_ahead_usd=q_usd)) for s in sigs]
        groups: dict = {}
        for s, r in res:
            groups.setdefault(keyfn(s), []).append(r)
        out = {}
        for k, v in sorted(groups.items(), key=lambda kv: str(kv[0])):
            mm = _metrics(v)
            out[str(k)] = {"n": mm["signals"], "fills": mm["fills"], "fill_rate": mm["fill_rate"],
                           "ev_per_fill": mm["ev_after_cost"], "adverse": mm["adverse_selection_cost"],
                           "significant": mm["significant"]}
        return out
    def age(s): return "young<120s_left" if s["secs_to_expiry"] > 120 else "old"
    def vol(s): return "hi_vol" if s["btc_vol"] > 0.0009 else ("lo_vol" if s["btc_vol"] < 0.0004 else "mid_vol")
    def spr(s): return "wide" if s.get("spread_w", 0) > 0.06 else "tight"
    def flow(s): return "yes_heavy" if s["flow_imbalance"] > 0.2 else ("no_heavy" if s["flow_imbalance"] < -0.2 else "balanced")
    def lead(s): return "btc_ahead" if abs(s.get("lag", 0)) > 0.08 else "in_line"
    def liq(s): return "deep" if s["volume_usd"] > 400 else ("thin" if s["volume_usd"] < 100 else "mid")
    def dur(s): return f"{s['duration_minutes']}m"
    def large(s): return "has_large" if s.get("has_large_trade") else "no_large"
    def hod(s):
        hh = s.get("hour", 0)
        return "00-06" if hh < 6 else ("06-12" if hh < 12 else ("12-18" if hh < 18 else "18-24"))
    axes = {"market_age": age, "btc_volatility": vol, "spread_width": spr, "flow_imbalance": flow,
            "btc_lead": lead, "liquidity": liq, "duration": dur, "large_trade": large, "time_of_day": hod}
    return {ax: {"best_queue": buckets(fn, "best"), "worst_queue": buckets(fn, "worst")}
            for ax, fn in axes.items()}


def run_queue_study(db: Session) -> dict:
    """Queue-position realism study for the 5s passive-maker edge. With no L2 book, it
    brackets queue position (best/mid/worst), expands the sample as far as the data
    allows (BTC 5m+15m only), sweeps timeouts 1–15s, breaks down by regime, compares
    policies and a vol-adaptive timeout, and returns a queue-realistic verdict.
    Paper/research only — promotes nothing, places no orders."""
    avail = data_availability(db)
    spec = next(iter(_models_to_test(db)), None)
    if spec is None:
        return {"ok": False, "error": "no model / dataset too small", "data_availability": avail}
    sigs = []
    for s in ("train", "val", "holdout"):
        sigs += build_signals(db, s, spec["model"], spec["feats"], max_future=None)
    if len(sigs) < MIN_TRADES:
        return {"ok": False, "error": "too few signals", "data_availability": avail, "n": len(sigs)}
    # queue-ahead estimate = median crossing-trade size (one typical resting order ahead)
    sizes = [e[2] for s in sigs for e in s["future"] if len(e) > 2 and e[2] > 0]
    q_usd = round(sorted(sizes)[len(sizes) // 2], 2) if sizes else 25.0

    univ = {"5m": [5], "5m+15m": [5, 15]}
    rows = []
    for uname, durs in univ.items():
        usigs = [s for s in sigs if s["duration_minutes"] in durs]
        if len(usigs) < MIN_TRADES:
            continue
        for policy in QUEUE_POLICIES:
            for to in QUEUE_TIMEOUTS:
                for mode in QUEUE_MODES:
                    rows.append(_queue_gate_row(uname, policy, to, mode, usigs, q_usd))

    # headline 5s comparison: join_bid / improve_bid across queue modes, biggest universe
    u = "5m+15m" if any(r["universe"] == "5m+15m" for r in rows) else "5m"
    def at(policy, to, mode):
        return next((r for r in rows if r["universe"] == u and r["policy"] == policy
                     and r["timeout_s"] == to and r["queue"] == mode), None)
    headline = {f"{pol}@5s": {mode: at(pol, 5, mode) for mode in QUEUE_MODES}
                for pol in ("join_bid", "improve_bid")}

    # vol-adaptive timeout: short rest in high vol, long rest in low vol
    adaptive = {}
    for mode in QUEUE_MODES:
        res = [simulate_queue(s, "join_bid", timeout=(2 if s["btc_vol"] > 0.0009 else 8),
                              mode=mode, queue_ahead_usd=q_usd) for s in [x for x in sigs if x["duration_minutes"] in (5, 15)]]
        mm = _metrics(res)
        adaptive[mode] = {"ev_per_fill": mm["ev_after_cost"], "fills": mm["fills"], "t_stat": mm["t_stat"],
                          "significant": mm["significant"], "adverse": mm["adverse_selection_cost"]}

    breakdown = _queue_breakdown([s for s in sigs if s["duration_minutes"] in (5, 15)], q_usd)
    analysis = _queue_verdict(rows, headline, u)
    out = {"ok": True, "generated_at": datetime.utcnow().isoformat(),
           "data_availability": avail, "queue_ahead_usd_estimate": q_usd, "our_order_shares": 5,
           "n_signals": len(sigs), "rows": rows, "headline_5s": headline,
           "vol_adaptive_timeout": adaptive, "regime_breakdown": breakdown,
           "unsupported_adaptive_cancels": [
               "cancel-if-BTC-moves-against / FV-deteriorates / flow-flips / book-imbalance-turns: need "
               "intra-window BTC + order-flow + L2-book series we do not store (only decision-time snapshot "
               "+ the trade-price stream). 'cancel-if-price-trades-through' ≈ the worst-queue model.",
               "midpoint quoting: needs a live two-sided book (no L2 book stored)",
           ],
           **analysis, "safety": _safety()}
    st = _state(db)
    base = dict(st.execution) if isinstance(st.execution, dict) else {}
    base["queue_study"] = out
    st.execution = base
    st.execution_built_at = datetime.utcnow()
    db.commit()
    return out


def _queue_verdict(rows: list[dict], headline: dict, u: str) -> dict:
    sig_worst = [r for r in rows if r["queue"] == "worst" and r["significant"] and r["ev_per_fill"] > 0 and r["fills"] >= MIN_TRADES]
    sig_mid = [r for r in rows if r["queue"] == "mid" and r["significant"] and r["ev_per_fill"] > 0 and r["fills"] >= MIN_TRADES]
    sig_best = [r for r in rows if r["queue"] == "best" and r["significant"] and r["ev_per_fill"] > 0 and r["fills"] >= MIN_TRADES]
    pos_any = [r for r in rows if r["ev_per_fill"] > 0 and r["fills"] >= MIN_TRADES]
    pos_best_only = [r for r in rows if r["queue"] == "best" and r["ev_per_fill"] > 0]
    if sig_worst:
        code, verdict = 1, "Profitable under conservative (worst-case) queue assumptions"
    elif sig_best or sig_mid:
        code, verdict = 2, "Profitable only under optimistic queue assumptions"
    elif pos_any:
        code, verdict = 3, "Promising but needs more data (positive EV, not significant under realistic queue)"
    elif pos_best_only:
        code, verdict = 4, "Not profitable after realistic queue assumptions (only the touch-fill best case is +EV)"
    else:
        code, verdict = 4, "Not profitable after realistic queue assumptions"
    jb = headline.get("join_bid@5s", {})
    headline_txt = ("5s join_bid EV/fill — best:%s mid:%s worst:%s (sig best:%s mid:%s worst:%s)" % (
        (jb.get("best") or {}).get("ev_per_fill"), (jb.get("mid") or {}).get("ev_per_fill"),
        (jb.get("worst") or {}).get("ev_per_fill"), (jb.get("best") or {}).get("significant"),
        (jb.get("mid") or {}).get("significant"), (jb.get("worst") or {}).get("significant")))
    return {"verdict_code": code, "verdict": verdict, "headline": headline_txt,
            "n_significant_positive": {"best": len(sig_best), "mid": len(sig_mid), "worst": len(sig_worst)},
            "best_config_overall": max(pos_any, key=lambda r: r["ev_per_day"], default=None)}


def _sweep_analysis(rows: list[dict]) -> dict:
    """Distil the sweep: per-fill-EV-vs-window curve, the windows that maximise total
    and risk-adjusted EV/day, where adverse selection starts to dominate, whether any
    config is significantly +EV, and the 1-of-4 verdict."""
    def curve(policy, universe):
        seq = [r for r in rows if r["policy"] == policy and r["universe"] == universe]
        seq.sort(key=lambda r: (999999 if r["rest_window_s"] == "full_life" else r["rest_window_s"]))
        return [{"rest": r["rest_window_s"], "fill_rate": r["fill_rate"], "fills_per_day": r["fills_per_day"],
                 "ev_per_fill": r["ev_per_fill"], "ev_per_day": r["ev_per_day"],
                 "adverse_selection_cost": r["adverse_selection_cost"], "spread_captured": r["spread_captured"],
                 "significant": r["significant"], "t_stat": r["t_stat"]} for r in seq]
    universe = "5m+15m" if any(r["universe"] == "5m+15m" for r in rows) else "5m"
    sig_pos = [r for r in rows if r["significant"] and r["ev_per_fill"] > 0 and r["fills"] >= MIN_TRADES]
    # window where adverse selection first exceeds captured spread (per-fill EV turns net-negative
    # from the maker side) for the directional join_bid policy
    jb = curve("join_bid", universe)
    adverse_dominates = next((c["rest"] for c in jb if c["adverse_selection_cost"] > 0
                              and c["ev_per_fill"] <= 0), None)
    best_total = max(rows, key=lambda r: r["ev_per_day"]) if rows else None
    # risk-adjusted: highest ev/day among configs with a usable sample + positive PF
    radj_pool = [r for r in rows if r["fills"] >= MIN_TRADES and r["ev_per_fill"] > 0]
    best_radj = max(radj_pool, key=lambda r: r["sharpe"] * (r["ev_per_day"] if r["ev_per_day"] > 0 else 0),
                    default=None)
    two_sided = curve("two_sided", universe)

    # verdict (1 viable at scale / 2 viable specific windows / 3 +EV but not significant /
    # 4 adverse selection destroys longer-rest fills)
    pos_ev_any = any(r["ev_per_fill"] > 0 and r["fills"] >= MIN_TRADES for r in rows)
    longrest_neg = any(r["ev_per_fill"] <= 0 for r in rows
                       if r["rest_window_s"] in ("full_life", 120) and r["policy"] in ("join_bid", "two_sided")
                       and r["fills"] >= MIN_TRADES)
    n_sig = len(sig_pos)
    if n_sig >= 4:
        code, verdict = 1, "passive market making is viable at scale"
    elif n_sig >= 1:
        code, verdict = 2, "passive market making is viable only in specific windows/regimes"
    elif pos_ev_any:
        code, verdict = 3, "positive per-fill EV but not enough sample / significance yet"
    else:
        code, verdict = 4, "adverse selection destroys longer-rest fills — passive making fails"

    best = best_total or {}
    headline = (f"best total EV/day: {best.get('policy')} @ {best.get('rest_window_s')}s on {best.get('universe')} "
                f"= {best.get('ev_per_day')}/day ({best.get('fills_per_day')} fills/day, EV/fill {best.get('ev_per_fill')}, "
                f"adverse {best.get('adverse_selection_cost')}, sig={best.get('significant')})")
    return {
        "verdict_code": code, "verdict": verdict, "headline": headline,
        "ev_vs_window_join_bid": jb, "ev_vs_window_two_sided": two_sided,
        "best_total_ev_day": best_total, "best_risk_adjusted": best_radj,
        "adverse_dominates_at_s": adverse_dominates,
        "significant_positive_configs": sig_pos,
        "answers": {
            "60s_remains_profitable_per_fill": next(
                (c["ev_per_fill"] > 0 for c in jb if c["rest"] == 60), None),
            "fill_rate_offsets_adverse": (best_total or {}).get("ev_per_day", 0) > 0,
            "window_max_total_ev_day": best.get("rest_window_s"),
            "window_max_risk_adjusted": (best_radj or {}).get("rest_window_s"),
            "adverse_dominates_at_s": adverse_dominates,
            "any_model_significant_positive": n_sig > 0,
        },
    }
