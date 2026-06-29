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
_BTC_TIMEOUT = float(os.getenv("BTC5M_LAB_BTC_TIMEOUT", "8"))   # short — a geo-blocked source must fail fast
_dead_sources: set[str] = set()      # sources that errored THIS build run (e.g. Binance geo-block in US)


def _empty_meta(source=None) -> dict:
    return {"source": source, "resolution_s": None, "coverage_pct": 0.0,
            "missing_s": 0, "stale_s": 0, "ticks": 0, "calls": 0, "error": None}


def _kraken_1s(start: datetime, end: datetime, pair: str = "XBTUSD") -> tuple[list[tuple[int, float]], dict]:
    """TRUE 1-second price from Kraken historical trade ticks (US-reachable),
    aggregated to 1s bars (last trade price per second, forward-filled). Returns
    (series, coverage_meta). The primary 1s source. `pair` selects the asset
    (XBTUSD default; ETHUSD/SOLUSD for cross-market lead research)."""
    start_s, end_s = start.timestamp(), end.timestamp()
    since = int((start_s - 60) * 1e9)            # 60s buffer so the opening second has a price
    ticks: list[tuple[float, float]] = []
    calls = 0
    with httpx.Client(timeout=_BTC_TIMEOUT, headers={"User-Agent": "polytrade-lab"}) as c:
        while calls < 6:
            r = c.get("https://api.kraken.com/0/public/Trades", params={"pair": pair, "since": str(since)})
            r.raise_for_status()
            d = r.json()
            calls += 1
            if d.get("error"):
                if calls == 1:
                    raise RuntimeError(f"kraken: {d['error']}")
                break                                # transient (e.g. rate limit) -> use what we have
            res = d["result"]
            pair = next(k for k in res if k != "last")
            tr = res[pair]
            ticks += [(float(t[2]), float(t[0])) for t in tr]
            last = int(res["last"])
            if not tr or ticks[-1][0] >= end_s or last <= since:
                break
            since = last
    # aggregate to 1s
    sec: dict[int, float] = {}
    for ts, p in ticks:
        if ts <= end_s:
            sec[int(ts)] = p
    pre = [p for ts, p in ticks if ts < start_s]
    last_p = pre[-1] if pre else None             # opening price from the buffer
    bars: list[tuple[int, float | None]] = []
    miss = stale = covered = 0
    for s in range(int(start_s), int(end_s) + 1):
        if s in sec:
            last_p = sec[s]; covered += 1
        elif last_p is not None:
            stale += 1; covered += 1               # forward-filled -> still a valid price
        else:
            miss += 1
        bars.append((s - int(start_s), last_p))
    # back-fill any leading None with the first known price (keep offsets aligned)
    first = next((p for _, p in bars if p is not None), None)
    series = [(t, p if p is not None else first) for t, p in bars if (p is not None or first is not None)]
    total = len(bars)
    meta = {"source": "kraken_1s", "resolution_s": 1,
            "coverage_pct": round(100 * covered / total, 1) if total else 0.0,
            "missing_s": miss, "stale_s": stale, "ticks": len(ticks), "calls": calls, "error": None}
    return series, meta


def _binance_1s(start: datetime, end: datetime) -> tuple[list[tuple[int, float]], dict]:
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    with httpx.Client(timeout=_BTC_TIMEOUT) as c:
        r = c.get("https://api.binance.com/api/v3/klines",
                  params={"symbol": "BTCUSDT", "interval": "1s", "startTime": start_ms,
                          "endTime": end_ms, "limit": 1000})
        r.raise_for_status()
        rows = r.json()
    if not rows:
        return [], _empty_meta("binance_1s")
    base = int(rows[0][0])
    series = [(int((int(k[0]) - base) / 1000), float(k[4])) for k in rows]
    meta = {"source": "binance_1s", "resolution_s": 1, "coverage_pct": round(100 * len(series) / max(1, (end_ms - base) / 1000 + 1), 1),
            "missing_s": 0, "stale_s": 0, "ticks": len(rows), "calls": 1, "error": None}
    return series, meta


def _coinbase_1m(start: datetime, end: datetime) -> tuple[list[tuple[int, float]], dict]:
    sms, ems = int(start.timestamp()), int(end.timestamp())
    with httpx.Client(timeout=_BTC_TIMEOUT, headers={"User-Agent": "polytrade-lab"}) as c:
        r = c.get("https://api.exchange.coinbase.com/products/BTC-USD/candles",
                  params={"granularity": 60, "start": sms, "end": ems})
        r.raise_for_status()
        rows = r.json()
    if not rows:
        return [], _empty_meta("coinbase_1m")
    bars = sorted([(int(k[0]), float(k[4])) for k in rows])
    base = bars[0][0]
    series = [(t - base, p) for t, p in bars]
    return series, {"source": "coinbase_1m", "resolution_s": 60, "coverage_pct": 100.0,
                    "missing_s": 0, "stale_s": 0, "ticks": len(bars), "calls": 1,
                    "error": "1-minute resolution — too coarse for BTC-lead"}


# Kraken 1s first (US-reachable historical ticks); Binance 1s next (geo-blocked in
# US, dead-skipped); Coinbase 1m last resort. 1m is flagged as inadequate.
_BTC_SOURCES = (("kraken_1s", _kraken_1s), ("binance_1s", _binance_1s), ("coinbase_1m", _coinbase_1m))


def fetch_btc_series(start: datetime, end: datetime, *, fetch_fn=None):
    """BTC prices over [start, end] as (t_offset_s, price) + coverage meta. A
    source that ERRORS is skipped for the rest of the run (so a geo-blocked source
    isn't waited on 80×). Returns (series, source|None, meta). Never raises."""
    if fetch_fn is not None:
        s = fetch_fn(start, end)
        return s, "injected", {**_empty_meta("injected"), "resolution_s": 1,
                               "coverage_pct": 100.0 if s else 0.0, "ticks": len(s)}
    for name, fn in _BTC_SOURCES:
        if name in _dead_sources:
            continue
        try:
            series, meta = fn(start, end)
        except Exception as exc:  # noqa: BLE001
            _dead_sources.add(name)
            continue
        if series:
            return series, name, meta
    return [], None, _empty_meta()


def all_btc_sources_dead() -> bool:
    return all(name in _dead_sources for name, _ in _BTC_SOURCES)


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
    """BTC spot features at decision time t (seconds after open). With a true 1s
    series these short-horizon returns are meaningful (the whole point of the 1s
    source) — over a 1m series they collapse."""
    p0 = series[0][1] if series else None
    pt = _price_at(series, t)
    if p0 is None or pt is None or p0 <= 0:
        return {}

    def ret(dt):
        prev = _price_at(series, t - dt)
        return (pt - prev) / prev if prev else 0.0

    w30 = [p for (tt, p) in series if t - 30 <= tt <= t]
    w10 = [p for (tt, p) in series if t - 10 <= tt <= t]
    rets = [(w30[i] - w30[i - 1]) / w30[i - 1] for i in range(1, len(w30))] if len(w30) > 1 else [0.0]
    hi = max((p for tt, p in series if t - 30 <= tt <= t), default=pt)
    lo = min((p for tt, p in series if t - 30 <= tt <= t), default=pt)
    return {
        "btc_ret_sofar": round((pt - p0) / p0, 6),
        "btc_ret_1s": round(ret(1), 6), "btc_ret_2s": round(ret(2), 6), "btc_ret_3s": round(ret(3), 6),
        "btc_ret_5s": round(ret(5), 6), "btc_ret_10s": round(ret(10), 6), "btc_ret_20s": round(ret(20), 6),
        "btc_ret_30s": round(ret(30), 6), "btc_ret_60s": round(ret(60), 6),
        "btc_momentum": round(_slope(w10), 4),                 # short-horizon slope (1s)
        "btc_acceleration": round(_slope(w10) - _slope(w30), 4),
        "btc_vol": round(_std(rets), 6),
        "btc_candle": 1 if ret(5) > 0 else (-1 if ret(5) < 0 else 0),
        "btc_breakout": 1 if pt >= hi - 1e-9 else (-1 if pt <= lo + 1e-9 else 0),
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
    # forward YES at t+L (latency sensitivity: what we'd pay entering L seconds late)
    def yes_at(tt):
        upto = [tr for tr in trades if (tr.seconds_from_creation or 0) <= tt]
        upto.sort(key=lambda tr: tr.seconds_from_creation or 0)
        return round(_yes_price(upto[-1]), 4) if upto else round(yes_t, 4)
    return {
        "pm_yes": round(yes_t, 4),
        "yes_lat_1": yes_at(t + 1), "yes_lat_2": yes_at(t + 2),
        "yes_lat_3": yes_at(t + 3), "yes_lat_5": yes_at(t + 5),
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


def wallet_features(trades: list, t: int, profitable: set[str]) -> dict:
    """Wallet-behavior as a FEATURE, not a copy target. Net directional flow of
    *profitable* wallets up to t — i.e. what experienced traders chose under this
    market state. The model learns from this label; it never blindly copies it."""
    before = [tr for tr in trades if (tr.seconds_from_creation or 0) <= t and tr.side == "buy"]
    pw = [tr for tr in before if (tr.wallet_address or "").lower() in profitable]
    if not pw:
        return {"wallet_signal": 0.0, "wallet_recent_signal": 0.0,
                "wallet_trade_count": 0, "wallet_present": 0}
    yes = sum(tr.usd_value for tr in pw if tr.direction == "YES")
    no = sum(tr.usd_value for tr in pw if tr.direction == "NO")
    tot = yes + no
    rec = [tr for tr in pw if (tr.seconds_from_creation or 0) >= t - 30]
    ry = sum(tr.usd_value for tr in rec if tr.direction == "YES")
    rn = sum(tr.usd_value for tr in rec if tr.direction == "NO")
    rtot = ry + rn
    return {
        "wallet_signal": round((yes - no) / tot, 4) if tot else 0.0,
        "wallet_recent_signal": round((ry - rn) / rtot, 4) if rtot else 0.0,
        "wallet_trade_count": len(pw),
        "wallet_present": 1 if pw else 0,
    }


def _profitable_wallets(db: Session) -> set[str]:
    rows = db.scalars(select(bm.Btc5mWalletProfile.wallet_address)
                      .where(bm.Btc5mWalletProfile.profitable.is_(True))).all()
    return {(a or "").lower() for a in rows}


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


def _market_lag_profile(btc_series, trades, life, max_lag: int = 10) -> dict:
    """Per-market BTC->YES lead/lag: does BTC's recent 3s return predict the YES
    price change over the NEXT k seconds? Returns {lag_k: corr}. A positive corr
    peaking at k>0 means BTC LEADS Polymarket by ~k seconds."""
    price_by = {toff: p for (toff, p) in btc_series if p is not None and 0 <= toff <= life}
    if len(price_by) < 10:
        return {}
    bp, lastp = [], None
    yes_by = {}
    for tr in sorted(trades, key=lambda x: x.seconds_from_creation or 0):
        s = tr.seconds_from_creation or 0
        if 0 <= s <= life:
            yes_by[s] = _yes_price(tr)
    yes, lasty = [], 0.5
    for s in range(0, life + 1):
        if s in price_by:
            lastp = price_by[s]
        bp.append(lastp)
        if s in yes_by:
            lasty = yes_by[s]
        yes.append(lasty)
    if any(p is None for p in bp[:5]):
        return {}
    prof = {}
    for k in range(0, max_lag + 1):
        xs, ys = [], []
        for t in range(3, life - k):
            if bp[t] is None or bp[t - 3] is None:
                continue
            xs.append((bp[t] - bp[t - 3]) / bp[t - 3])       # BTC 3s return at t
            ys.append(yes[t + k] - yes[t])                    # YES change over next k
        prof[k] = _corr(xs, ys)
    return prof


def build_dataset(db: Session, *, limit_markets: int = 40, fetch_fn=None,
                  decision_fractions=DECISION_FRACTIONS) -> dict:
    """Build synchronized decision-point rows for resolved BTC 5m/15m markets, with
    a true 1s BTC series. Idempotent per market. Also computes the BTC->YES lag
    profile + source-quality coverage."""
    markets = db.scalars(select(bm.Btc5mMarket).where(bm.Btc5mMarket.resolved.is_(True))
                         .order_by(bm.Btc5mMarket.created_time.desc()).limit(limit_markets)).all()
    st = _state(db)
    n_markets = n_points = 0
    source = None
    err = None
    profitable = _profitable_wallets(db)
    if fetch_fn is None:
        _dead_sources.clear()
    attempts = 0
    cov_sum = miss_sum = stale_sum = res_max = cov_n = 0
    lag_sum: dict[int, float] = {}
    lag_n = 0
    for mk in markets:
        dur = _slug_duration(mk.slug, mk.question)
        life = (dur or 5) * 60
        start = mk.created_time
        end = mk.resolution_time or (start + timedelta(seconds=life) if start else None)
        if not (start and end):
            continue
        attempts += 1
        series, src, meta = fetch_btc_series(start, end, fetch_fn=fetch_fn)
        source = source or src
        if not series:
            err = "btc price unavailable"
            if fetch_fn is None and all_btc_sources_dead() and attempts >= 3:
                break
            continue
        cov_sum += meta.get("coverage_pct", 0.0); miss_sum += meta.get("missing_s", 0)
        stale_sum += meta.get("stale_s", 0); res_max = max(res_max, meta.get("resolution_s") or 0); cov_n += 1
        label_up = _market_label_up(mk, series)
        if label_up is None:
            continue
        trades = db.scalars(select(bm.Btc5mTrade).where(bm.Btc5mTrade.market_id == mk.market_id)).all()
        prof = _market_lag_profile(series, trades, life)
        if prof:
            for k, v in prof.items():
                lag_sum[k] = lag_sum.get(k, 0.0) + v
            lag_n += 1
        for old in db.scalars(select(lm.Btc5mLabPoint).where(lm.Btc5mLabPoint.market_id == mk.market_id)).all():
            db.delete(old)
        for frac in decision_fractions:
            t = int(life * frac)
            bf = btc_features(series, t)
            if not bf:
                continue
            pf = pm_flow_features(trades, t, bf["btc_ret_sofar"])
            wf = wallet_features(trades, t, profitable)
            feats = {**bf, **pf, **wf, "t_offset_s": t, "secs_to_expiry": life - t,
                     "hour": (start.hour if start else 0), "duration_minutes": dur, "btc_source": src}
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
    st.btc_resolution_s = res_max or None
    st.btc_coverage_pct = round(cov_sum / cov_n, 1) if cov_n else 0.0
    st.btc_missing_s = miss_sum
    st.btc_stale_s = stale_sum
    st.lag_profile = {str(k): round(v / lag_n, 4) for k, v in sorted(lag_sum.items())} if lag_n else {}
    st.dataset_built_at = datetime.utcnow()
    db.commit()
    return {"markets_built": n_markets, "points_built": n_points, "btc_source": source,
            "btc_resolution_s": res_max or None, "btc_coverage_pct": st.btc_coverage_pct, "btc_error": err}


def _assign_splits(db: Session) -> None:
    """Chronological train/val/holdout split BY MARKET (no leakage)."""
    db.flush()                                           # prod session is autoflush=False
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
STRATEGY_FAMILIES = ("btc_lead", "btc_momentum", "btc_reversal", "fade_overreaction", "flow_confirm")


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
        # BTC moved over a SHORT (1s-resolution) horizon but YES hasn't repriced
        bret = p.get(f"btc_ret_{prm['horizon']}s", 0.0)
        if abs(bret) < prm["min_btc_move"]:
            return None
        if abs(p.get("pm_momentum", 0.0)) > prm["max_pm_move"]:    # YES already moved -> too late
            return None
        if prm.get("require_lag") and abs(p.get("lag", 0.0)) < 0.08:
            return None
        return "YES" if bret > 0 else "NO"

    if family == "btc_momentum":
        # BTC trending over 3s AND 10s (aligned) -> continuation
        r3, r10 = p.get("btc_ret_3s", 0.0), p.get("btc_ret_10s", 0.0)
        if abs(r10) < prm["min_btc_move"] or (r3 > 0) != (r10 > 0):
            return None
        if prm.get("require_breakout") and p.get("btc_breakout", 0) == 0:
            return None
        return "YES" if r10 > 0 else "NO"

    if family == "btc_reversal":
        # short BTC move REVERSES the 30s trend, PM still stale -> trade the reversal
        r5, r30 = p.get("btc_ret_5s", 0.0), p.get("btc_ret_30s", 0.0)
        if abs(r5) < prm["min_btc_move"] or (r5 > 0) == (r30 > 0):
            return None
        if abs(p.get("pm_momentum", 0.0)) > prm["max_pm_move"]:
            return None
        return "YES" if r5 > 0 else "NO"

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


def backtest(points: list[dict], family: str, prm: dict, *, slippage: float = 0.0, latency: int = 0) -> dict:
    """Backtest a strategy over decision-point dicts. Pure. `latency` (s) enters at
    the YES price `latency` seconds later (yes_lat_L) to measure entry-latency cost."""
    profits, costs, wins, edges, spreads, drifts = [], [], [], [], [], []
    by_regime: dict[str, list] = {}
    by_dur: dict[str, list] = {}
    cum, peak, mdd = 0.0, 0.0, 0.0
    for p in points:
        d = evaluate_strategy(p, family, prm)
        if d is None:
            continue
        entry_yes = p.get(f"yes_lat_{latency}", p["pm_yes"]) if latency else p["pm_yes"]
        profit, cost, win = _trade_pnl(d, entry_yes if entry_yes is not None else p["pm_yes"],
                                       p.get("spread", 0.0), p["label_up"], slippage)
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
        space = {**base, "horizon": [2, 3, 5], "min_btc_move": [0.0002, 0.0005, 0.001],
                 "max_pm_move": [0.02, 0.05], "require_lag": [False, True]}
    elif family == "btc_momentum":
        space = {**base, "min_btc_move": [0.0003, 0.0008, 0.0015], "require_breakout": [False, True]}
    elif family == "btc_reversal":
        space = {**base, "min_btc_move": [0.0003, 0.0008], "max_pm_move": [0.02, 0.05]}
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


def btc_source_quality(db: Session) -> dict:
    st = _state(db)
    return {"source": st.btc_price_source, "resolution_s": st.btc_resolution_s,
            "coverage_pct": st.btc_coverage_pct, "missing_s": st.btc_missing_s,
            "stale_s": st.btc_stale_s, "fetch_error": st.btc_fetch_error,
            "is_true_1s": st.btc_resolution_s == 1}


def lag_report(db: Session) -> dict:
    """BTC -> Polymarket lead/lag from the 1s cross-correlation profile: at which
    lag (seconds) does BTC's recent move best predict the YES price change?"""
    st = _state(db)
    prof = {int(k): v for k, v in (st.lag_profile or {}).items()}
    if not prof:
        return {"profile": {}, "peak_lag_s": None, "peak_corr": None, "btc_leads": False}
    peak_lag = max(prof, key=lambda k: prof[k])
    peak_corr = prof[peak_lag]
    # BTC leads if the cross-corr peaks at a POSITIVE lag with a meaningful corr,
    # and that peak beats the contemporaneous (lag 0) value.
    btc_leads = peak_lag > 0 and peak_corr >= 0.05 and peak_corr > prof.get(0, 0) + 0.01
    return {"profile": {str(k): prof[k] for k in sorted(prof)},
            "peak_lag_s": peak_lag, "peak_corr": round(peak_corr, 4),
            "lag0_corr": round(prof.get(0, 0), 4), "btc_leads": btc_leads,
            "resolution_s": st.btc_resolution_s,
            "interpretation": "peak cross-corr at lag k>0 => BTC's move predicts YES's move k seconds later => BTC leads"}


def latency_curve(db: Session, strat) -> list[dict]:
    """Holdout ROI of a strategy entering 0/1/2/3/5 seconds late (entry-latency cost)."""
    hold = _point_dicts(db, "holdout")
    out = []
    for L in (0, 1, 2, 3, 5):
        m = backtest(hold, strat.family, strat.params, slippage=0.01, latency=L)
        out.append({"latency_s": L, "roi": m["roi"], "trades": m["trades"], "avg_edge": m["avg_edge"]})
    return out


# ---------------------------------------------------------------------------
# report — which opportunity is best?
# ---------------------------------------------------------------------------
def build_report(db: Session) -> dict:
    accepted = db.scalars(select(lm.Btc5mLabStrategy).where(lm.Btc5mLabStrategy.overfit.is_(False),
                          lm.Btc5mLabStrategy.roi > 0).order_by(lm.Btc5mLabStrategy.robust_score.desc())).all()
    lag = lag_analysis(db)
    large = large_trade_analysis(db)
    flow = flow_imbalance_analysis(db)
    src_q = btc_source_quality(db)
    lagr = lag_report(db)
    best = accepted[0] if accepted else None
    by_family = {}
    for s in accepted:
        by_family.setdefault(s.family, []).append(s.robust_score)
    fam_best = {k: max(v) for k, v in by_family.items()}

    # 4-option verdict: btc_lead_edge / order_flow_edge_only / no_durable_edge /
    # data_insufficient.
    btc_families = {"btc_lead", "btc_momentum", "btc_reversal"}
    if not src_q["is_true_1s"] or src_q["coverage_pct"] < 70 or (_state(db).points_built or 0) < 60:
        code, verdict = 4, "data still insufficient"
        headline = (f"data still insufficient — BTC source {src_q['source']} "
                    f"({src_q['resolution_s']}s res, {src_q['coverage_pct']}% coverage)")
    elif best is None:
        code, verdict = 3, "no durable edge"
        if lagr.get("btc_leads"):
            headline = (f"BTC leads Polymarket by ~{lagr.get('peak_lag_s')}s (cross-corr rises to "
                        f"{lagr.get('peak_corr')} at lag {lagr.get('peak_lag_s')}s, ~0 contemporaneous) — a REAL "
                        "lead, but too weak to overcome spread/slippage: no durable tradeable edge")
        else:
            headline = "no durable edge found (no strategy survived holdout with positive edge after costs)"
    elif best.family in btc_families:
        code, verdict = 1, "BTC-lead edge found"
        headline = (f"BTC-lead edge: '{best.name}' (holdout ROI {best.roi:.1%}, {best.trades} trades); "
                    f"BTC leads PM by ~{lagr.get('peak_lag_s')}s (peak corr {lagr.get('peak_corr')})")
    elif best.family == "flow_confirm":
        code, verdict, headline = 2, "order-flow edge only", \
            f"order-flow edge only: '{best.name}' (holdout ROI {best.roi:.1%})"
    else:
        code, verdict, headline = 3, "no durable edge", \
            f"edge present but not a BTC-lead/flow edge ('{best.family}')"

    report = {
        "verdict_code": code, "verdict": verdict, "headline": headline,
        "btc_source_quality": src_q,
        "lag_report": lagr,
        "best_strategy": ({"name": best.name, "family": best.family, "params": best.params,
                           "holdout_roi": best.roi, "holdout_trades": best.trades,
                           "win_rate": best.win_rate, "profit_factor": best.profit_factor,
                           "max_drawdown": best.max_drawdown, "avg_edge": best.avg_edge,
                           "robust_score": best.robust_score, "metrics": best.metrics,
                           "latency_curve": latency_curve(db, best)} if best else None),
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
        "btc_source_quality": {"source": st.btc_price_source, "resolution_s": st.btc_resolution_s,
                               "coverage_pct": st.btc_coverage_pct, "missing_s": st.btc_missing_s,
                               "stale_s": st.btc_stale_s, "is_true_1s": st.btc_resolution_s == 1},
        "lag_profile": st.lag_profile or {},
        "dataset_built_at": st.dataset_built_at.isoformat() if st.dataset_built_at else None,
        "strategies_tested": st.strategies_tested, "strategies_accepted": st.strategies_accepted,
        "last_search_at": st.last_search_at.isoformat() if st.last_search_at else None,
        "report": st.report,
        "safety": "BTC 5M Independent Strategy Lab — research/paper only; never trades or touches "
                  "live execution / copy trading / bankroll",
    }
