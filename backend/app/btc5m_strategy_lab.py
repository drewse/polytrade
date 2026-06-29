"""BTC 5M Independent Strategy Lab — research/paper ONLY.

Tests OUR OWN BTC 5M/15M strategies on a synchronized dataset of BTC spot price +
Polymarket price/order-flow + timing, instead of copying wallets. Builds a
feature row at several decision points per market, then generates + backtests
thousands of rule strategies with train/validation/holdout splits, rejects overfit
strategies, and ranks by robust out-of-sample performance.

100% READ-ONLY w.r.t. production: reads the indexed btc5m_* tables, fetches BTC
spot price (Binance/Coinbase, injectable), and writes only btc5m_lab_* rows. It
NEVER places orders, changes live execution/sizing/bankroll/risk, or copy ranking.
Wallet trades are used only as order-flow CONTEXT/labels — no strategy copies a
wallet.
"""
from __future__ import annotations

import itertools
import math
import os
import statistics
from datetime import datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import btc5m
from . import btc5m_models as bm
from . import btc5m_strategy_models as lm
from .settings import config

# decision points as fractions of the market life (all early — "first 30–90s"
# style entries; never the final stretch).
DECISION_FRACTIONS = (0.1, 0.2, 0.3, 0.4, 0.5)
BTC_MOVE_SCALE = 0.0015            # ~0.15% BTC move maps a 5m market toward a decided outcome
LARGE_TRADE_USD = 50.0            # a "large" BTC5m trade (these markets are small-size)


# ---------------------------------------------------------------------------
# small pure math
# ---------------------------------------------------------------------------
def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs):
    return statistics.pstdev(xs) if len(xs) > 1 else 0.0


def _slope(xs):
    n = len(xs)
    if n < 2:
        return 0.0
    mx = (n - 1) / 2.0
    my = _mean(xs)
    den = sum((i - mx) ** 2 for i in range(n))
    return (sum((i - mx) * (xs[i] - my) for i in range(n)) / den) if den else 0.0


def _clip(v, lo, hi):
    return max(lo, min(hi, v))


def _btc_norm(ret: float) -> float:
    """Map a BTC return to a directional strength in [-0.5, 0.5] (comparable to
    a Polymarket (yes-0.5) signal)."""
    return _clip(ret / BTC_MOVE_SCALE, -1.0, 1.0) * 0.5


# ---------------------------------------------------------------------------
# BTC spot price client (injectable; fail-soft)
# ---------------------------------------------------------------------------
def _binance_klines(start_ms: int, end_ms: int, interval: str = "1s") -> list[tuple[int, float]]:
    url = "https://api.binance.com/api/v3/klines"
    with httpx.Client(timeout=config.http_timeout_seconds) as c:
        r = c.get(url, params={"symbol": "BTCUSDT", "interval": interval,
                               "startTime": start_ms, "endTime": end_ms, "limit": 1000})
        r.raise_for_status()
        rows = r.json()
    return [(int(k[0]), float(k[4])) for k in rows]   # (openTime ms, close)


def _coinbase_candles(start_ms: int, end_ms: int) -> list[tuple[int, float]]:
    url = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
    with httpx.Client(timeout=config.http_timeout_seconds, headers={"User-Agent": "polytrade-lab"}) as c:
        r = c.get(url, params={"granularity": 60, "start": int(start_ms / 1000), "end": int(end_ms / 1000)})
        r.raise_for_status()
        rows = r.json()
    # coinbase: [time(s), low, high, open, close, volume] descending
    return sorted([(int(k[0]) * 1000, float(k[4])) for k in rows])


def fetch_btc_series(start: datetime, end: datetime, *, fetch_fn=None) -> tuple[list[tuple[int, float]], str | None]:
    """BTC close prices over [start, end] as (t_offset_s, price). Binance 1s first,
    Coinbase 1m fallback. Returns (series, source|None). Never raises."""
    if fetch_fn is not None:
        return fetch_fn(start, end), "injected"
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    for name, fn in (("binance_1s", _binance_klines), ("coinbase_1m", _coinbase_candles)):
        try:
            raw = fn(start_ms, end_ms)
            if raw:
                base = raw[0][0]
                return [(int((t - base) / 1000), p) for t, p in raw], name
        except Exception:  # noqa: BLE001  (fail-soft; try the next source)
            continue
    return [], None


def _price_at(series: list[tuple[int, float]], t_offset: int) -> float | None:
    """BTC price at or just before t_offset seconds."""
    last = None
    for t, p in series:
        if t <= t_offset:
            last = p
        else:
            break
    return last if last is not None else (series[0][1] if series else None)


# ---------------------------------------------------------------------------
# feature engine (pure)
# ---------------------------------------------------------------------------
def btc_features(series: list[tuple[int, float]], t: int) -> dict:
    """BTC spot features at decision time t (seconds after open)."""
    p0 = series[0][1] if series else None
    pt = _price_at(series, t)
    if p0 is None or pt is None or p0 <= 0:
        return {}
    def ret(dt):
        prev = _price_at(series, t - dt)
        return (pt - prev) / prev if prev else 0.0
    window = [p for (tt, p) in series if t - 30 <= tt <= t]
    rets = [(window[i] - window[i - 1]) / window[i - 1] for i in range(1, len(window))] if len(window) > 1 else [0.0]
    return {
        "btc_ret_sofar": round((pt - p0) / p0, 6),
        "btc_ret_5s": round(ret(5), 6), "btc_ret_10s": round(ret(10), 6), "btc_ret_30s": round(ret(30), 6),
        "btc_momentum": round(_slope(window), 4),
        "btc_vol": round(_std(rets), 6),
        "btc_candle": 1 if ret(10) > 0 else (-1 if ret(10) < 0 else 0),
    }


def _yes_price(trade) -> float:
    """Implied YES probability from a trade (price is for the traded outcome)."""
    return trade.price if trade.direction == "YES" else (1.0 - trade.price)


def pm_flow_features(trades: list, t: int, btc_ret_sofar: float) -> dict:
    """Polymarket price + order-flow features from trades that occurred in [0, t]."""
    before = [tr for tr in trades if (tr.seconds_from_creation or 0) <= t]
    if not before:
        return {"pm_yes": None}
    before.sort(key=lambda tr: tr.seconds_from_creation or 0)
    yes_t = _yes_price(before[-1])
    prev = [tr for tr in before if (tr.seconds_from_creation or 0) <= t - 10]
    yes_prev = _yes_price(prev[-1]) if prev else yes_t
    # order flow (signed by YES/NO buys)
    buys = [tr for tr in before if tr.side == "buy"]
    yes_usd = sum(tr.usd_value for tr in buys if tr.direction == "YES")
    no_usd = sum(tr.usd_value for tr in buys if tr.direction == "NO")
    tot = yes_usd + no_usd
    imbalance = round((yes_usd - no_usd) / tot, 4) if tot else 0.0
    recent = [tr for tr in buys if (tr.seconds_from_creation or 0) >= t - 15]
    ry = sum(tr.usd_value for tr in recent if tr.direction == "YES")
    rn = sum(tr.usd_value for tr in recent if tr.direction == "NO")
    rtot = ry + rn
    recent_imb = round((ry - rn) / rtot, 4) if rtot else 0.0
    largest = max((tr.usd_value for tr in before), default=0.0)
    lag = round(_btc_norm(btc_ret_sofar) - (yes_t - 0.5), 4)   # >0: BTC up more than YES priced
    return {
        "pm_yes": round(yes_t, 4),
        "pm_momentum": round(yes_t - yes_prev, 4),
        "lag": lag,
        "flow_imbalance": imbalance,
        "recent_flow_imbalance": recent_imb,
        "large_trade_usd": round(largest, 2),
        "has_large_trade": 1 if largest >= LARGE_TRADE_USD else 0,
        "volume_usd": round(tot, 2),
        "trade_freq": round(len(before) / max(1, t), 3),
        # crude spread proxy: dispersion of recent trade prices (no historical book)
        "spread": round(_std([tr.price for tr in before[-8:]]) * 2, 4),
    }


def _regime(f: dict) -> str:
    vol, mom = abs(f.get("btc_vol", 0.0)), abs(f.get("btc_momentum", 0.0))
    if mom > 0.5 and vol > 0.0005:
        return "strong_trend"
    if vol > 0.0008:
        return "high_vol"
    if abs(f.get("btc_ret_sofar", 0.0)) < 0.0003:
        return "chop"
    return "mixed"


# ---------------------------------------------------------------------------
# dataset builder
# ---------------------------------------------------------------------------
def _duration_minutes(mk) -> int | None:
    return btc5m._parse_duration(mk.slug, mk.question) if hasattr(btc5m, "_parse_duration") else \
        _slug_duration(mk.slug, mk.question)


def _slug_duration(slug, question) -> int | None:
    import re
    for s in (slug or "", question or ""):
        m = re.search(r"(\d+)\s*m", s)
        if m:
            return int(m.group(1))
    return None


def _market_label_up(mk, btc_series) -> bool | None:
    if mk.final_outcome:
        return btc5m._yes_no(mk.final_outcome) == "YES"   # "Up" -> YES
    if btc_series and len(btc_series) >= 2:
        return btc_series[-1][1] >= btc_series[0][1]
    return None


def build_dataset(db: Session, *, limit_markets: int = 80, fetch_fn=None,
                  decision_fractions=DECISION_FRACTIONS) -> dict:
    """Build synchronized decision-point rows for resolved BTC 5m/15m markets.
    Idempotent per market (clears + rebuilds that market's points)."""
    markets = db.scalars(select(bm.Btc5mMarket).where(bm.Btc5mMarket.resolved.is_(True))
                         .order_by(bm.Btc5mMarket.created_time.desc()).limit(limit_markets)).all()
    st = _state(db)
    n_markets = n_points = 0
    source = None
    err = None
    for mk in markets:
        dur = _slug_duration(mk.slug, mk.question)
        life = (dur or 5) * 60
        start = mk.created_time
        end = mk.resolution_time or (start + timedelta(seconds=life) if start else None)
        if not (start and end):
            continue
        series, src = fetch_btc_series(start, end, fetch_fn=fetch_fn)
        source = source or src
        if not series:
            err = "btc price unavailable"
            continue
        label_up = _market_label_up(mk, series)
        if label_up is None:
            continue
        trades = db.scalars(select(bm.Btc5mTrade).where(bm.Btc5mTrade.market_id == mk.market_id)).all()
        # idempotent: clear this market's existing points
        for old in db.scalars(select(lm.Btc5mLabPoint).where(lm.Btc5mLabPoint.market_id == mk.market_id)).all():
            db.delete(old)
        for frac in decision_fractions:
            t = int(life * frac)
            bf = btc_features(series, t)
            if not bf:
                continue
            pf = pm_flow_features(trades, t, bf["btc_ret_sofar"])
            feats = {**bf, **pf, "t_offset_s": t, "secs_to_expiry": life - t,
                     "hour": (start.hour if start else 0), "duration_minutes": dur}
            db.add(lm.Btc5mLabPoint(
                market_id=mk.market_id, duration_minutes=dur, t_offset_s=t, secs_to_expiry=life - t,
                regime=_regime(feats), features=feats, pm_yes=pf.get("pm_yes"), spread=pf.get("spread"),
                btc_ret_30s=bf.get("btc_ret_30s"), flow_imbalance=pf.get("flow_imbalance"),
                label_up=label_up))
            n_points += 1
        n_markets += 1
    _assign_splits(db)
    st.markets_built = n_markets
    st.points_built = db.scalar(select(func.count()).select_from(lm.Btc5mLabPoint)) or 0
    st.btc_price_source = source
    st.btc_fetch_error = err
    st.dataset_built_at = datetime.utcnow()
    db.commit()
    return {"markets_built": n_markets, "points_built": n_points, "btc_source": source, "btc_error": err}


def _assign_splits(db: Session) -> None:
    """Chronological train/val/holdout split BY MARKET (no leakage)."""
    mids = [m for (m,) in db.execute(
        select(lm.Btc5mLabPoint.market_id).distinct()).all()]
    # order markets by their created_time
    order = {}
    for mid in mids:
        mk = db.get(bm.Btc5mMarket, mid)
        order[mid] = mk.created_time if (mk and mk.created_time) else datetime.min
    ordered = sorted(mids, key=lambda m: order[m])
    n = len(ordered)
    tr, va = int(n * 0.6), int(n * 0.8)
    split_of = {}
    for i, mid in enumerate(ordered):
        split_of[mid] = "train" if i < tr else ("val" if i < va else "holdout")
    for pt in db.scalars(select(lm.Btc5mLabPoint)).all():
        pt.split = split_of.get(pt.market_id, "train")
    db.commit()


# ---------------------------------------------------------------------------
# strategy families + backtest (pure)
# ---------------------------------------------------------------------------
STRATEGY_FAMILIES = ("btc_lead", "fade_overreaction", "flow_confirm")


def evaluate_strategy(p: dict, family: str, prm: dict) -> str | None:
    """Return 'YES' / 'NO' to enter, or None to skip. Pure over a point dict."""
    # shared filters
    if p.get("pm_yes") is None:
        return None
    if (p.get("spread") or 0.0) > prm["max_spread"]:
        return None
    if not (prm["entry_min"] <= p["t_offset_s"] <= prm["entry_max"]):
        return None
    if (p.get("secs_to_expiry") or 0) < prm["min_secs_left"]:
        return None
    yes = p["pm_yes"]
    btc = p.get("btc_ret_sofar", 0.0)

    if family == "btc_lead":
        # BTC has moved but YES hasn't fully repriced (lag) -> follow BTC
        if abs(p.get("lag", 0.0)) < prm["min_lag"]:
            return None
        if abs(btc) < prm["min_btc_move"]:
            return None
        if prm.get("require_momentum") and (btc > 0) != (p.get("btc_momentum", 0.0) > 0):
            return None
        return "YES" if btc > 0 else "NO"

    if family == "fade_overreaction":
        # YES far from 0.5 but BTC barely moved -> PM overreacted -> fade
        if abs(yes - 0.5) < prm["min_yes_dev"]:
            return None
        if abs(btc) > prm["max_btc_move"]:
            return None
        return "NO" if yes > 0.5 else "YES"

    if family == "flow_confirm":
        # order-flow imbalance is directional AND BTC confirms it
        imb = p.get("recent_flow_imbalance", 0.0)
        if abs(imb) < prm["min_imbalance"]:
            return None
        if prm.get("require_btc_confirm") and (imb > 0) != (btc > 0):
            return None
        if prm.get("require_large") and not p.get("has_large_trade"):
            return None
        return "YES" if imb > 0 else "NO"
    return None


def _trade_pnl(direction: str, yes: float, spread: float, label_up: bool, slippage: float) -> tuple[float, float, bool]:
    """Returns (profit, cost, win). Buys the chosen side at the ask (yes + half-spread
    + slippage). Binary payout 1/0 at resolution."""
    half = (spread or 0.0) / 2 + slippage
    if direction == "YES":
        pe = _clip(yes + half, 0.01, 0.99)
        win = bool(label_up)
    else:
        pe = _clip((1 - yes) + half, 0.01, 0.99)
        win = not bool(label_up)
    profit = (1.0 - pe) if win else -pe
    return profit, pe, win


def backtest(points: list[dict], family: str, prm: dict, *, slippage: float = 0.0) -> dict:
    """Backtest a strategy over decision-point dicts. Pure."""
    profits, costs, wins, edges, spreads, drifts = [], [], [], [], [], []
    by_regime: dict[str, list] = {}
    by_dur: dict[str, list] = {}
    cum, peak, mdd = 0.0, 0.0, 0.0
    for p in points:
        d = evaluate_strategy(p, family, prm)
        if d is None:
            continue
        profit, cost, win = _trade_pnl(d, p["pm_yes"], p.get("spread", 0.0), p["label_up"], slippage)
        profits.append(profit); costs.append(cost); wins.append(1 if win else 0)
        edges.append(profit / cost if cost else 0.0)
        spreads.append((p.get("spread") or 0.0) / 2)
        drifts.append(abs(p.get("pm_momentum", 0.0)))   # proxy for latency sensitivity
        by_regime.setdefault(p.get("regime") or "?", []).append(profit / cost if cost else 0.0)
        by_dur.setdefault(str(p.get("duration_minutes") or "?"), []).append(profit / cost if cost else 0.0)
        cum += profit
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    n = len(profits)
    gross_win = sum(x for x in profits if x > 0)
    gross_loss = -sum(x for x in profits if x < 0)
    return {
        "trades": n,
        "win_rate": round(sum(wins) / n, 4) if n else 0.0,
        "roi": round(sum(profits) / sum(costs), 4) if costs and sum(costs) else 0.0,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else (round(gross_win, 3) if gross_win else 0.0),
        "max_drawdown": round(mdd, 4),
        "avg_edge": round(_mean(edges), 4),
        "spread_cost": round(_mean(spreads), 4),
        "latency_sensitivity": round(_mean(drifts), 4),
        "by_regime": {k: round(_mean(v), 4) for k, v in by_regime.items()},
        "by_duration": {k: round(_mean(v), 4) for k, v in by_dur.items()},
    }


# ---------------------------------------------------------------------------
# strategy search (train/val/holdout + overfit rejection + robust ranking)
# ---------------------------------------------------------------------------
def _grid(family: str) -> list[dict]:
    base = dict(max_spread=[0.02, 0.05, 0.10], entry_min=[20, 40], entry_max=[150, 300],
                min_secs_left=[30, 60])
    if family == "btc_lead":
        space = {**base, "min_lag": [0.08, 0.15, 0.25], "min_btc_move": [0.0003, 0.0008],
                 "require_momentum": [False, True]}
    elif family == "fade_overreaction":
        space = {**base, "min_yes_dev": [0.15, 0.25, 0.35], "max_btc_move": [0.0003, 0.0006]}
    else:  # flow_confirm
        space = {**base, "min_imbalance": [0.3, 0.5, 0.7], "require_btc_confirm": [False, True],
                 "require_large": [False, True]}
    keys = list(space.keys())
    return [dict(zip(keys, combo)) for combo in itertools.product(*[space[k] for k in keys])]


def _point_dicts(db: Session, split: str | None = None):
    q = select(lm.Btc5mLabPoint)
    if split:
        q = q.where(lm.Btc5mLabPoint.split == split)
    out = []
    for pt in db.scalars(q).all():
        f = dict(pt.features or {})
        f.update({"regime": pt.regime, "duration_minutes": pt.duration_minutes,
                  "t_offset_s": pt.t_offset_s, "secs_to_expiry": pt.secs_to_expiry,
                  "pm_yes": pt.pm_yes, "spread": pt.spread, "label_up": pt.label_up})
        out.append(f)
    return out


def _robust_score(train: dict, val: dict, hold: dict) -> float:
    """Reward positive, CONSISTENT out-of-sample edge; penalize train/holdout gap."""
    if hold["trades"] < 8:
        return -1.0
    consistency = 1.0 - _clip(abs(train["roi"] - hold["roi"]), 0, 1)
    oos = min(val["roi"], hold["roi"])
    sample = _clip(hold["trades"] / 40.0, 0.2, 1.0)
    return round((oos * 100) * consistency * sample + hold["avg_edge"] * 20, 2)


def run_search(db: Session, *, families=STRATEGY_FAMILIES, min_train_trades: int = 12,
               max_overfit_gap: float = 0.15, slippage: float = 0.01, top_keep: int = 60) -> dict:
    """Generate + backtest the parameter grid for each family across train/val/
    holdout; reject overfit; rank by robust out-of-sample score."""
    train = _point_dicts(db, "train")
    val = _point_dicts(db, "val")
    hold = _point_dicts(db, "holdout")
    if not (train and hold):
        return {"ok": False, "error": "dataset not built / too small", "tested": 0}
    # clear previous strategies
    for old in db.scalars(select(lm.Btc5mLabStrategy)).all():
        db.delete(old)
    results = []
    tested = 0
    for fam in families:
        for prm in _grid(fam):
            tested += 1
            tr = backtest(train, fam, prm, slippage=slippage)
            if tr["trades"] < min_train_trades or tr["roi"] <= 0:
                continue                                   # no in-sample edge -> skip
            va = backtest(val, fam, prm, slippage=slippage)
            ho = backtest(hold, fam, prm, slippage=slippage)
            overfit, reason = False, None
            if ho["trades"] < 8:
                overfit, reason = True, "too few holdout trades"
            elif va["roi"] <= 0 or ho["roi"] <= 0:
                overfit, reason = True, "negative out-of-sample ROI"
            elif (tr["roi"] - ho["roi"]) > max_overfit_gap:
                overfit, reason = True, f"train/holdout gap {tr['roi'] - ho['roi']:.2f}"
            score = _robust_score(tr, va, ho)
            results.append({"family": fam, "params": prm, "train": tr, "val": va, "holdout": ho,
                            "overfit": overfit, "reason": reason, "score": score})
    results.sort(key=lambda r: -r["score"])
    accepted = 0
    for i, r in enumerate(results[:top_keep]):
        ho = r["holdout"]
        status = "rejected" if r["overfit"] else ("accepted" if r["score"] > 0 else "candidate")
        accepted += 1 if status == "accepted" else 0
        db.add(lm.Btc5mLabStrategy(
            name=f"{r['family']}#{i + 1}", family=r["family"], params=r["params"],
            trades=ho["trades"], win_rate=ho["win_rate"], roi=ho["roi"],
            profit_factor=ho["profit_factor"], max_drawdown=ho["max_drawdown"], avg_edge=ho["avg_edge"],
            robust_score=r["score"], overfit=r["overfit"], rejected_reason=r["reason"], status=status,
            metrics={"train": r["train"], "val": r["val"], "holdout": ho}))
    st = _state(db)
    st.strategies_tested = tested
    st.strategies_accepted = accepted
    st.last_search_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "tested": tested, "kept": min(len(results), top_keep), "accepted": accepted}


# ---------------------------------------------------------------------------
# analyses (for the report + dashboard)
# ---------------------------------------------------------------------------
def _corr(xs, ys):
    n = len(xs)
    if n < 3:
        return 0.0
    mx, my = _mean(xs), _mean(ys)
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    den = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    return round(num / den, 4) if den else 0.0


def lag_analysis(db: Session) -> dict:
    """Does BTC lead Polymarket repricing? Correlate the BTC-vs-YES lag with the
    eventual resolution, and the YES catch-up after a BTC move."""
    pts = _point_dicts(db)
    lag = [p.get("lag", 0.0) for p in pts if p.get("pm_yes") is not None]
    up = [1 if p["label_up"] else 0 for p in pts if p.get("pm_yes") is not None]
    catchup = _mean([abs(p.get("pm_momentum", 0.0)) for p in pts if abs(p.get("btc_ret_sofar", 0.0)) > 0.0005])
    return {"n": len(lag), "lag_vs_resolution_corr": _corr(lag, up),
            "avg_yes_catchup_after_btc_move": round(catchup, 4),
            "interpretation": "positive corr => when BTC has moved more than YES priced (lag>0), "
                              "the market resolves in BTC's direction => BTC leads PM"}


def large_trade_analysis(db: Session) -> dict:
    pts = _point_dicts(db)
    big = [p for p in pts if p.get("has_large_trade")]
    small = [p for p in pts if not p.get("has_large_trade")]
    def hit(ps):
        # does flow direction (imbalance) predict resolution?
        ok = [1 if ((p.get("flow_imbalance", 0) > 0) == p["label_up"]) else 0 for p in ps if p.get("pm_yes") is not None]
        return round(_mean(ok), 4)
    return {"n_large": len(big), "large_trade_dir_hit_rate": hit(big),
            "baseline_dir_hit_rate": hit(small),
            "interpretation": "if large-trade hit rate >> baseline, large trades predict short-term direction"}


def flow_imbalance_analysis(db: Session) -> dict:
    pts = [p for p in _point_dicts(db) if p.get("pm_yes") is not None]
    imb = [p.get("flow_imbalance", 0.0) for p in pts]
    up = [1 if p["label_up"] else 0 for p in pts]
    return {"n": len(pts), "flow_vs_resolution_corr": _corr(imb, up),
            "interpretation": "positive corr => YES-buy order-flow imbalance predicts an Up resolution"}


def edge_decay(db: Session) -> dict:
    """Best accepted strategy's edge by entry-time bucket (does the edge decay as
    the market ages?)."""
    best = db.scalar(select(lm.Btc5mLabStrategy).where(lm.Btc5mLabStrategy.overfit.is_(False))
                     .order_by(lm.Btc5mLabStrategy.robust_score.desc()))
    if not best:
        return {"buckets": [], "strategy": None}
    pts = _point_dicts(db)
    buckets = []
    for lo, hi in ((0, 60), (60, 120), (120, 240), (240, 10000)):
        seg = [p for p in pts if lo <= p["t_offset_s"] < hi]
        m = backtest(seg, best.family, best.params, slippage=0.01)
        buckets.append({"entry_window_s": f"{lo}-{hi if hi < 10000 else '+'}", "trades": m["trades"],
                        "roi": m["roi"], "avg_edge": m["avg_edge"]})
    return {"strategy": best.name, "buckets": buckets}


# ---------------------------------------------------------------------------
# report — which opportunity is best?
# ---------------------------------------------------------------------------
def build_report(db: Session) -> dict:
    accepted = db.scalars(select(lm.Btc5mLabStrategy).where(lm.Btc5mLabStrategy.overfit.is_(False),
                          lm.Btc5mLabStrategy.roi > 0).order_by(lm.Btc5mLabStrategy.robust_score.desc())).all()
    lag = lag_analysis(db)
    large = large_trade_analysis(db)
    flow = flow_imbalance_analysis(db)
    best = accepted[0] if accepted else None
    by_family = {}
    for s in accepted:
        by_family.setdefault(s.family, []).append(s.robust_score)
    fam_best = {k: max(v) for k, v in by_family.items()}

    # classify
    verdicts = []
    if best is None:
        code, headline = 5, "no durable edge found (no strategy survived holdout with positive edge)"
    else:
        fam = best.family
        if fam == "btc_lead" and lag["lag_vs_resolution_corr"] > 0.1:
            code, headline = 1, "BTC price leads Polymarket repricing"
        elif fam == "flow_confirm" and flow["flow_vs_resolution_corr"] > 0.1:
            code, headline = 2, "Polymarket order flow predicts resolution"
        elif large["large_trade_dir_hit_rate"] > large["baseline_dir_hit_rate"] + 0.08:
            code, headline = 3, "large trades predict short-term movement"
        elif fam == "fade_overreaction":
            code, headline = 4, "mean reversion after overreaction"
        elif fam == "btc_lead":
            code, headline = 1, "BTC price leads Polymarket repricing"
        elif fam == "flow_confirm":
            code, headline = 2, "Polymarket order flow predicts resolution"
        else:
            code, headline = 5, "edge present but not cleanly attributable"
    report = {
        "verdict_code": code, "headline": headline,
        "best_strategy": ({"name": best.name, "family": best.family, "params": best.params,
                           "holdout_roi": best.roi, "holdout_trades": best.trades,
                           "win_rate": best.win_rate, "profit_factor": best.profit_factor,
                           "robust_score": best.robust_score} if best else None),
        "family_best_scores": fam_best,
        "lag_analysis": lag, "large_trade_analysis": large, "flow_imbalance_analysis": flow,
        "n_accepted": len(accepted),
        "safety": "research/paper only — never places orders or touches live trading",
    }
    st = _state(db)
    st.report = report
    db.commit()
    return report


# ---------------------------------------------------------------------------
# read APIs / state
# ---------------------------------------------------------------------------
def _state(db: Session) -> lm.Btc5mLabState:
    st = db.get(lm.Btc5mLabState, 1)
    if st is None:
        st = lm.Btc5mLabState(id=1)
        db.add(st)
        db.commit()
    return st


def leaderboard(db: Session, *, limit: int = 40) -> dict:
    rows = db.scalars(select(lm.Btc5mLabStrategy).order_by(lm.Btc5mLabStrategy.robust_score.desc())
                      .limit(limit)).all()
    def row(s):
        return {"name": s.name, "family": s.family, "params": s.params, "status": s.status,
                "trades": s.trades, "win_rate": s.win_rate, "roi": s.roi,
                "profit_factor": s.profit_factor, "max_drawdown": s.max_drawdown,
                "avg_edge": s.avg_edge, "robust_score": s.robust_score, "overfit": s.overfit,
                "rejected_reason": s.rejected_reason, "metrics": s.metrics}
    return {"accepted": [row(s) for s in rows if not s.overfit and s.status == "accepted"],
            "rejected": [row(s) for s in rows if s.overfit],
            "all": [row(s) for s in rows]}


def status(db: Session) -> dict:
    st = _state(db)
    n_pts = db.scalar(select(func.count()).select_from(lm.Btc5mLabPoint)) or 0
    by_split = {s: (db.scalar(select(func.count()).select_from(lm.Btc5mLabPoint)
                              .where(lm.Btc5mLabPoint.split == s)) or 0) for s in ("train", "val", "holdout")}
    by_dur = {}
    for (d,) in db.execute(select(lm.Btc5mLabPoint.duration_minutes).distinct()).all():
        by_dur[str(d)] = db.scalar(select(func.count()).select_from(lm.Btc5mLabPoint)
                                   .where(lm.Btc5mLabPoint.duration_minutes == d)) or 0
    return {
        "markets_built": st.markets_built, "points_built": n_pts, "by_split": by_split,
        "by_duration": by_dur, "btc_price_source": st.btc_price_source, "btc_fetch_error": st.btc_fetch_error,
        "dataset_built_at": st.dataset_built_at.isoformat() if st.dataset_built_at else None,
        "strategies_tested": st.strategies_tested, "strategies_accepted": st.strategies_accepted,
        "last_search_at": st.last_search_at.isoformat() if st.last_search_at else None,
        "report": st.report,
        "safety": "BTC 5M Independent Strategy Lab — research/paper only; never trades or touches "
                  "live execution / copy trading / bankroll",
    }
