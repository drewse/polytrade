"""Market Intelligence & Regime Engine V1 — isolated research/analytics on top of
the BTC 5M Reversal Lab.

It explains MARKETS (not just wallets): what kind of BTC 5m market each one is,
which regime it's in, and which wallets/strategies dominate that environment.

100% READ-ONLY: reads btc5m_* and research_* tables, writes only mi_* tables. It
NEVER places trades, changes live execution, wallet eligibility, copy trading,
rankings, bankroll, risk controls, discovery, or any production behaviour.

Phases: 1 market profiles · 2 regime engine · 3 wallet×regime · 4 strategy×regime
· 5 regime leaderboards · 6 strategy/wallet decay · 7 originality graph
· 8 position-size intelligence · 9 counterfactual simulator · 10 regime
recommendations · 11 nightly review. Orchestrated by run_intel_batch().
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import btc5m
from . import btc5m_models as bm
from . import market_intel_models as mim
from . import research_models as rm

REGIMES = ["Strong Trend", "Weak Trend", "Mean Reversion", "High Volatility",
           "Low Volatility", "Breakout", "Range Bound", "Liquidity Spike",
           "News Driven", "Whipsaw", "Mixed", "Hybrid"]

LEADERBOARD_REGIMES = ["Strong Trend", "Mean Reversion", "Breakout", "High Volatility",
                       "Low Volatility", "Range Bound", "Liquidity Spike", "News Driven",
                       "Whipsaw", "Weak Trend"]


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs):
    return statistics.pstdev(xs) if len(xs) > 1 else 0.0


def _median(xs):
    return statistics.median(xs) if xs else 0.0


def _clip(v, lo, hi):
    return max(lo, min(hi, v))


def _pct(xs, p):
    if not xs:
        return 0.0
    s = sorted(xs)
    k = _clip(int(round((p / 100.0) * (len(s) - 1))), 0, len(s) - 1)
    return s[k]


# ===========================================================================
# Phase 1 — Market Intelligence profile
# ===========================================================================
def _implied_yes(t) -> float:
    return t.price if t.direction == "YES" else 1.0 - t.price


def _direction_changes(series) -> int:
    chg, prev = 0, None
    for a, b in zip(series, series[1:]):
        d = 1 if b > a else (-1 if b < a else 0)
        if d and prev is not None and d != prev:
            chg += 1
        if d:
            prev = d
    return chg


def build_profiles(db: Session, *, prev_cache: dict | None = None) -> dict:
    """Build/refresh a permanent intelligence profile for every indexed BTC5m
    market (Phase 1) and classify its regime (Phase 2). Idempotent upsert."""
    markets = db.scalars(select(bm.Btc5mMarket).order_by(bm.Btc5mMarket.created_time.asc())).all()
    profitable = {p.wallet_address for p in db.scalars(
        select(bm.Btc5mWalletProfile).where(bm.Btc5mWalletProfile.profitable.is_(True))).all()}
    # market windows for overlap + gap timing
    windows = [(m.market_id, m.created_time, m.expiry) for m in markets if m.created_time]
    n_profiles, regimes_seen = 0, {}
    prev_created = None
    for m in markets:
        trs = db.scalars(select(bm.Btc5mTrade).where(bm.Btc5mTrade.market_id == m.market_id)
                         .order_by(bm.Btc5mTrade.timestamp.asc())).all()
        if not trs:
            continue
        series = [_implied_yes(t) for t in trs]
        usd = [t.usd_value for t in trs]
        duration = int((m.expiry - m.created_time).total_seconds()) if (m.expiry and m.created_time) else btc5m.MARKET_LIFE_SECONDS

        # price evolution
        diffs = [b - a for a, b in zip(series, series[1:])] or [0.0]
        price = {
            "opening_prob": round(series[0], 4), "closing_prob": round(series[-1], 4),
            "high": round(max(series), 4), "low": round(min(series), 4),
            "vwap": round(sum(p * w for p, w in zip(series, usd)) / (sum(usd) or 1.0), 4),
            "range": round(max(series) - min(series), 4),
            "net_move": round(series[-1] - series[0], 4),
            "prob_volatility": round(_std(diffs), 5),
            "avg_abs_move": round(_mean([abs(d) for d in diffs]), 5),
            "direction_changes": _direction_changes(series),
        }
        # volume
        nb = 5
        buckets = [0.0] * nb
        if m.created_time:
            for t in trs:
                frac = _clip((t.seconds_from_creation or 0) / max(1, duration), 0, 0.999)
                buckets[int(frac * nb)] += t.usd_value
        largest = max(usd)
        volume = {
            "opening_volume": round(usd[0], 2), "total_volume": round(sum(usd), 2),
            "volume_curve": [round(b, 2) for b in buckets], "largest_trade": round(largest, 2),
            "trade_frequency_per_min": round(len(trs) / max(1, duration / 60.0), 2),
            "trade_count": len(trs),
        }
        # orderflow
        buys = sum(1 for t in trs if t.side == "buy")
        sells = len(trs) - buys
        big_thr = _pct(usd, 90)
        big_vol = sum(t.usd_value for t in trs if t.usd_value >= big_thr)
        by_wallet: dict[str, float] = {}
        for t in trs:
            by_wallet[t.wallet_address] = by_wallet.get(t.wallet_address, 0.0) + t.usd_value
        top3 = sum(sorted(by_wallet.values(), reverse=True)[:3])
        prof_vol = sum(t.usd_value for t in trs if t.wallet_address in profitable)
        orderflow = {
            "buy_sell_imbalance": round((buys - sells) / len(trs), 4),
            "aggressor_imbalance": round((buys - sells) / len(trs), 4),  # maker/taker not indexed -> proxy
            "large_wallet_participation": round(big_vol / (sum(usd) or 1.0), 4),
            "consensus_participation": round(prof_vol / (sum(usd) or 1.0), 4),
            "top_wallet_participation": round(top3 / (sum(usd) or 1.0), 4),
            "unique_wallets": len(by_wallet),
        }
        # timing
        gap_min = None
        if m.created_time and prev_created:
            gap_min = round((m.created_time - prev_created).total_seconds() / 60.0, 1)
        overlap = 0
        if m.created_time and m.expiry:
            for mid, c, e in windows:
                if mid != m.market_id and c and e and c < m.expiry and e > m.created_time:
                    overlap += 1
        timing = {
            "hour_of_day": m.created_time.hour if m.created_time else None,
            "weekday": m.created_time.weekday() if m.created_time else None,
            "minutes_since_prev_btc_market": gap_min,
            "overlapping_markets": overlap,
        }
        if m.created_time:
            prev_created = m.created_time

        feat_means = _avg_features(trs)
        primary, secondary, conf, evidence = _classify_regime(price, volume, orderflow, feat_means)

        rec = db.get(mim.MiMarketProfile, m.market_id) or mim.MiMarketProfile(market_id=m.market_id)
        rec.question = m.question or ""
        rec.created_time = m.created_time
        rec.expiry = m.expiry
        rec.resolution_time = m.resolution_time
        rec.duration_s = duration
        rec.final_outcome = m.final_outcome
        rec.resolved = bool(m.resolved)
        rec.price = price
        rec.volume = volume
        rec.orderflow = orderflow
        rec.timing = timing
        rec.primary_regime = primary
        rec.secondary_regime = secondary
        rec.regime_confidence = conf
        rec.regime_evidence = evidence
        rec.feature_means = feat_means
        db.add(rec)
        n_profiles += 1
        regimes_seen[primary] = regimes_seen.get(primary, 0) + 1
    db.commit()
    return {"profiles": n_profiles, "regime_distribution": regimes_seen}


def _avg_features(trades) -> dict:
    out = {}
    for k in btc5m.FEATURE_NAMES:
        out[k] = round(_mean([float(t.features.get(k, 0.0)) for t in trades if t.features]), 4)
    return out


# ===========================================================================
# Phase 2 — Regime classification
# ===========================================================================
def _classify_regime(price, volume, orderflow, feat) -> tuple[str, str | None, float, dict]:
    """Heuristic, explainable regime scoring from the market profile. Returns
    (primary, secondary, confidence, evidence)."""
    net = abs(price["net_move"])
    rng = price["range"]
    vol = price["prob_volatility"]
    revert = max(0.0, rng - net)                  # moved then came back
    changes = price["direction_changes"]
    slope = abs(feat.get("trend_slope", 0.0))
    boll = abs(feat.get("boll_pos", 0.0))
    atr = feat.get("atr", 0.0)
    curve = volume.get("volume_curve", [])
    spike = (max(curve) / (_mean(curve) or 1.0)) if curve else 1.0
    big = orderflow.get("large_wallet_participation", 0.0)

    scores = {
        "Strong Trend": _clip(net * 4 + slope * 6, 0, 1) if net >= 0.15 else net * 2,
        "Weak Trend": _clip(net * 5, 0, 1) if 0.05 <= net < 0.15 else 0.0,
        "Mean Reversion": _clip(revert * 4, 0, 1),
        "High Volatility": _clip((vol - 0.03) * 18, 0, 1),
        "Low Volatility": _clip((0.03 - vol) * 25, 0, 1),
        "Breakout": _clip(max(0.0, boll - 0.5) * 1.2 + atr * 6, 0, 1),
        "Range Bound": _clip((0.10 - rng) * 6, 0, 1) if rng < 0.10 else 0.0,
        "Liquidity Spike": _clip((spike - 2.0) * 0.5, 0, 1) + min(0.3, big),
        "News Driven": _clip(big * 0.8 + (net * 2 if changes <= 1 else 0), 0, 1),
        "Whipsaw": _clip((changes - 2) * 0.2, 0, 1),
    }
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top, second = ranked[0], ranked[1]
    evidence = {"net_move": price["net_move"], "range": rng, "prob_volatility": vol,
                "direction_changes": changes, "volume_spike": round(spike, 2),
                "large_wallet_participation": big, "scores": {k: round(v, 3) for k, v in ranked[:5]}}
    if top[1] < 0.15:
        return "Mixed", None, round(0.3 + top[1], 2), evidence
    margin = top[1] - second[1]
    if margin < 0.06 and second[1] > 0.30:        # only when two regimes are BOTH clearly present
        return "Hybrid", f"{top[0]}+{second[0]}", round(0.4 + top[1] * 0.3, 2), evidence
    conf = round(_clip(0.45 + top[1] * 0.4 + margin * 0.3, 0, 0.99), 2)
    return top[0], (second[0] if second[1] > 0.2 else None), conf, evidence


# ===========================================================================
# Phase 6 — rolling decay (shared by wallets, strategies, regimes)
# ===========================================================================
def _winrate_roi(rows) -> dict:
    """rows: [{won, pnl, stake}] -> aggregate win rate + ROI."""
    n = len(rows)
    if not n:
        return {"trades": 0, "win_rate": 0.0, "roi": 0.0}
    wins = sum(1 for r in rows if r["won"])
    invested = sum(r["stake"] for r in rows) or 1.0
    return {"trades": n, "win_rate": round(wins / n, 4),
            "roi": round(sum(r["pnl"] for r in rows) / invested, 4)}


def _trend_label(windows) -> tuple[str, float]:
    """Classify Improving / Stable / Decaying / Broken from rolling windows."""
    recent = windows.get("7d") or windows.get("30d") or {}
    base = windows.get("90d") or windows.get("lifetime") or {}
    rn, bn = recent.get("trades", 0), base.get("trades", 0)
    if rn < 3 or bn < 5:
        return "stable", round(_clip(min(rn, bn) / 10.0, 0.1, 0.6), 2)
    rw, bw = recent.get("win_rate", 0.0), base.get("win_rate", 0.0)
    d = rw - bw
    conf = round(_clip(min(rn, bn) / 20.0, 0.3, 0.95), 2)
    if recent.get("win_rate", 0) < 0.35 and recent.get("roi", 0) < -0.1:
        return "broken", conf
    if d >= 0.08:
        return "improving", conf
    if d <= -0.08:
        return "decaying", conf
    return "stable", conf


def _rolling(rows, max_date) -> dict:
    """rows: [{date, won, pnl, stake}] -> {7d,30d,90d,lifetime,trend,trend_confidence}."""
    out = {}
    for label, days in (("7d", 7), ("30d", 30), ("90d", 90), ("lifetime", 100000)):
        cut = max_date - timedelta(days=days) if max_date else None
        sub = [r for r in rows if not cut or (r["date"] and r["date"] >= cut)]
        out[label] = _winrate_roi(sub)
    trend, tconf = _trend_label(out)
    out["trend"] = trend
    out["trend_confidence"] = tconf
    return out


def _dataset_max_date(db: Session):
    return db.scalar(select(func.max(bm.Btc5mTrade.timestamp)))


# ===========================================================================
# Phase 3 + 8 — wallet × regime specialization + position-size conviction
# ===========================================================================
def _regime_map(db: Session) -> dict:
    return {p.market_id: p.primary_regime for p in db.scalars(select(mim.MiMarketProfile)).all()}


def _position_size(trades, all_avg_stakes: list[float]) -> dict:
    """Phase 8 — conviction / sizing study for one wallet's buy trades."""
    sizes = [t.usd_value for t in trades if t.side == "buy"]
    if not sizes:
        return {}
    avg = _mean(sizes)
    med = _median(sizes)
    hi_thr, lo_thr = _pct(sizes, 75), _pct(sizes, 25)
    # conviction vs profitability: avg stake on winners vs losers
    win_sizes = [t.usd_value for t in trades if t.side == "buy" and t.won]
    loss_sizes = [t.usd_value for t in trades if t.side == "buy" and t.won is False]
    pctile = round(sum(1 for a in all_avg_stakes if a <= avg) / max(1, len(all_avg_stakes)), 3)
    return {
        "avg_stake": round(avg, 2), "median_stake": round(med, 2),
        "max_stake": round(max(sizes), 2), "stake_cv": round(_std(sizes) / avg, 3) if avg else 0.0,
        "stake_percentile": pctile,
        "high_conviction_trades": sum(1 for s in sizes if s >= hi_thr),
        "low_conviction_trades": sum(1 for s in sizes if s <= lo_thr),
        "avg_stake_on_wins": round(_mean(win_sizes), 2),
        "avg_stake_on_losses": round(_mean(loss_sizes), 2),
        "conviction_pays": bool(win_sizes and loss_sizes and _mean(win_sizes) > _mean(loss_sizes)),
        "oversizing": bool(max(sizes) > avg * 3),
    }


def wallet_regime(db: Session, *, min_trades: int = 6) -> dict:
    """Phase 3 (performance by regime) + Phase 6 (decay) + Phase 8 (position size)
    per wallet. Writes MiWalletRegime (originality added by originality_graph)."""
    reg = _regime_map(db)
    max_date = _dataset_max_date(db)
    all_trades = db.scalars(select(bm.Btc5mTrade)).all()
    by_wallet: dict[str, list] = {}
    for t in all_trades:
        by_wallet.setdefault(t.wallet_address, []).append(t)
    profiles = {p.wallet_address: p for p in db.scalars(select(bm.Btc5mWalletProfile)).all()}
    all_avg_stakes = [_mean([t.usd_value for t in trs if t.side == "buy"]) for trs in by_wallet.values()
                      if any(t.side == "buy" for t in trs)]

    n = 0
    for addr, trs in by_wallet.items():
        if len(trs) < min_trades:
            continue
        settled = [t for t in trs if t.side == "buy" and t.won is not None]
        # by regime
        groups: dict[str, list] = {}
        for t in settled:
            groups.setdefault(reg.get(t.market_id, "Mixed"), []).append(t)
        by_regime = {}
        for rg, ts in groups.items():
            wins = sum(1 for t in ts if t.won)
            invested = sum(t.usd_value for t in ts) or 1.0
            by_regime[rg] = {"trades": len(ts), "win_rate": round(wins / len(ts), 4),
                             "roi": round(sum(t.realized_pnl for t in ts) / invested, 4)}
        # specialization: strength of the best (>=3-trade) regime vs the mean
        cand = {rg: v for rg, v in by_regime.items() if v["trades"] >= 3}
        best = max(cand, key=lambda r: cand[r]["win_rate"], default=None)
        avg_wr = _mean([v["win_rate"] for v in by_regime.values()]) if by_regime else 0.0
        spec = round(_clip((cand[best]["win_rate"] - avg_wr) * 2 + min(cand[best]["trades"], 10) / 20, 0, 1), 3) if best else 0.0
        decay = _rolling([{"date": t.timestamp, "won": bool(t.won), "pnl": t.realized_pnl,
                           "stake": t.usd_value} for t in settled], max_date)
        prof = profiles.get(addr)
        psize = _position_size(trs, all_avg_stakes)
        if prof and prof.metrics:
            psize["sizing_behavior"] = prof.metrics.get("sizing_behavior")

        row = db.get(mim.MiWalletRegime, addr) or mim.MiWalletRegime(wallet_address=addr)
        row.profitable = bool(prof and prof.profitable)
        row.cluster = prof.cluster if prof else "Unknown"
        row.trade_count = len(trs)
        row.by_regime = by_regime
        row.best_regime = best
        row.specialization_score = spec
        row.decay = decay
        row.position_size = psize
        db.add(row)
        n += 1
    db.commit()
    return {"wallets": n}


# ===========================================================================
# Phase 4 — strategy × regime heatmaps (+ decay)
# ===========================================================================
def strategy_regime(db: Session) -> dict:
    reg = _regime_map(db)
    max_date = _dataset_max_date(db)
    strategies = db.scalars(select(rm.ResearchStrategy)).all()
    n = 0
    for s in strategies:
        trades = db.scalars(select(rm.StrategyPaperTrade).where(
            rm.StrategyPaperTrade.strategy_id == s.id)).all()
        if not trades:
            continue
        groups: dict[str, list] = {}
        for t in trades:
            if t.won is None:
                continue
            groups.setdefault(reg.get(t.market_id, "Mixed"), []).append(t)
        by_regime = {}
        for rg, ts in groups.items():
            pnls = [t.realized_pnl for t in ts]
            stakes = [t.size for t in ts]
            wins = sum(1 for t in ts if t.won)
            invested = sum(stakes) or 1.0
            gw = sum(p for p in pnls if p > 0)
            gl = -sum(p for p in pnls if p < 0)
            # equity drawdown within the regime
            eq, peak, mdd = 0.0, 0.0, 0.0
            for p in pnls:
                eq += p
                peak = max(peak, eq)
                if peak > 0:
                    mdd = max(mdd, (peak - eq) / peak)
            by_regime[rg] = {
                "trades": len(ts), "win_rate": round(wins / len(ts), 4),
                "roi": round(sum(pnls) / invested, 4),
                "profit_factor": round(gw / gl, 3) if gl > 0 else round(gw, 3),
                "max_drawdown": round(mdd, 4),
                "expected_value": round(sum(pnls) / len(ts), 4),
                "confidence": round(_mean([t.confidence for t in ts]), 4),
            }
        best = max(by_regime, key=lambda r: by_regime[r]["roi"], default=None)
        worst = min(by_regime, key=lambda r: by_regime[r]["roi"], default=None)
        decay = _rolling([{"date": t.decision_at, "won": bool(t.won), "pnl": t.realized_pnl,
                           "stake": t.size} for t in trades if t.won is not None], max_date)
        row = db.get(mim.MiStrategyRegime, s.id) or mim.MiStrategyRegime(strategy_id=s.id)
        row.name = s.name
        row.archetype = s.archetype
        row.by_regime = by_regime
        row.best_regime = best
        row.worst_regime = worst
        row.decay = decay
        db.add(row)
        n += 1
    db.commit()
    return {"strategies": n}


# ===========================================================================
# Phase 7 — originality graph (leaders rank above followers)
# ===========================================================================
def originality_graph(db: Session) -> dict:
    cons = btc5m.consensus(db)
    edges = cons.get("edges", [])
    independent = set(cons.get("independent", []))
    leads: dict[str, int] = {}
    follows: dict[str, int] = {}
    follow_delays: dict[str, list] = {}
    follow_leaders: dict[str, dict] = {}
    for e in edges:
        L, F, co = e["leader"], e["follower"], e.get("co_entries", 1)
        leads[L] = leads.get(L, 0) + co
        follows[F] = follows.get(F, 0) + co
        follow_delays.setdefault(F, []).append(e.get("avg_lag_s", 0.0))
        follow_leaders.setdefault(F, {})
        follow_leaders[F][L] = follow_leaders[F].get(L, 0) + co

    addrs = set(leads) | set(follows) | independent
    updated = 0
    graph_nodes = []
    for a in addrs:
        ld, fl = leads.get(a, 0), follows.get(a, 0)
        total = ld + fl
        if a in independent:
            role, score = "independent", 0.8
        elif total == 0:
            role, score = "unknown", 0.5
        elif ld >= fl * 1.5:
            role, score = "leader", _clip(0.6 + ld / (total + 1), 0, 1)
        elif fl >= ld * 1.5:
            role, score = "follower", _clip(0.4 - fl / (total + 5), 0, 1)
        else:
            role, score = "mixed", 0.5
        delays = follow_delays.get(a, [])
        leaders_followed = follow_leaders.get(a, {})
        repeated = round(max(leaders_followed.values()) / fl, 3) if fl else 0.0
        orig = {
            "role": role, "leads": ld, "follows": fl,
            "avg_reaction_delay_s": round(_mean(delays), 1) if delays else None,
            "repeated_follow_pct": repeated,
            "independent": a in independent,
            "follows_leaders": sorted(leaders_followed, key=lambda k: -leaders_followed[k])[:3],
        }
        row = db.get(mim.MiWalletRegime, a)
        if row is not None:
            row.originality = orig
            row.originality_score = round(score, 3)
            db.add(row)
            updated += 1
        graph_nodes.append({"wallet": a, "role": role, "score": round(score, 3),
                            "leads": ld, "follows": fl})
    db.commit()
    graph_nodes.sort(key=lambda n: -n["score"])
    return {"nodes": len(graph_nodes), "updated": updated,
            "leaders": [n for n in graph_nodes if n["role"] == "leader"][:10],
            "followers": [n for n in graph_nodes if n["role"] == "follower"][:10],
            "edges": [{"leader": e["leader"], "follower": e["follower"],
                       "lag_s": e.get("avg_lag_s"), "agreement": e.get("agreement")} for e in edges[:50]]}


# ===========================================================================
# Phase 9 — counterfactual timing simulator (research only)
# ===========================================================================
def _price_at(times, yes_series, t):
    """Linear-interpolate the market-implied YES price at time t (seconds)."""
    if not times:
        return None
    if t <= times[0]:
        return yes_series[0]
    if t >= times[-1]:
        return yes_series[-1]
    for i in range(1, len(times)):
        if t <= times[i]:
            t0, t1 = times[i - 1], times[i]
            y0, y1 = yes_series[i - 1], yes_series[i]
            f = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            return y0 + (y1 - y0) * f
    return yes_series[-1]


SHIFTS = [-20, -10, -5, 5, 10, 20]


def counterfactual(db: Session, *, sample: int = 250, scope: str = "global") -> dict:
    """For a sample of settled buy trades, test entering N seconds earlier/later by
    re-pricing against the market's implied-probability path (outcome is known).
    Reports timing sensitivity, the optimal shift, and the expected improvement."""
    q = select(bm.Btc5mTrade).where(bm.Btc5mTrade.side == "buy", bm.Btc5mTrade.won.isnot(None))
    if scope != "global":
        q = q.where(bm.Btc5mTrade.wallet_address == scope)
    trades = db.scalars(q.order_by(bm.Btc5mTrade.timestamp.desc()).limit(sample)).all()
    # per-market implied-yes time series
    series_cache: dict[str, tuple] = {}

    def _series(mid):
        if mid not in series_cache:
            ts = db.scalars(select(bm.Btc5mTrade).where(bm.Btc5mTrade.market_id == mid)
                            .order_by(bm.Btc5mTrade.timestamp.asc())).all()
            times = [t.seconds_from_creation or 0 for t in ts]
            yes = [_implied_yes(t) for t in ts]
            series_cache[mid] = (times, yes)
        return series_cache[mid]

    deltas = {s: [] for s in SHIFTS}
    tested = 0
    for t in trades:
        times, yes = _series(t.market_id)
        if len(times) < 2:
            continue
        base_t = t.seconds_from_creation or 0
        stake = t.usd_value
        entry = t.price
        if entry <= 0:
            continue
        base_pnl = (stake / entry) * (1.0 if t.won else 0.0) - stake
        tested += 1
        for s in SHIFTS:
            y = _price_at(times, yes, base_t + s)
            if y is None:
                continue
            p = y if t.direction == "YES" else 1.0 - y
            p = _clip(p, 0.01, 0.99)
            pnl = (stake / p) * (1.0 if t.won else 0.0) - stake
            deltas[s].append(pnl - base_pnl)
    sensitivity = {str(s): round(_mean(v), 4) for s, v in deltas.items() if v}
    optimal = max(sensitivity, key=lambda k: sensitivity[k]) if sensitivity else "0"
    improvement = round(sensitivity.get(optimal, 0.0), 4) if sensitivity else 0.0
    row = mim.MiCounterfactual(scope=scope, trades_tested=tested, optimal_shift_s=int(optimal),
                               expected_improvement=improvement, timing_sensitivity=sensitivity,
                               detail={"shifts": SHIFTS, "sample": sample})
    db.add(row)
    db.commit()
    return {"trades_tested": tested, "optimal_shift_s": int(optimal),
            "expected_improvement": improvement, "timing_sensitivity": sensitivity}


# ===========================================================================
# Phase 10 — regime recommendation engine (informational only)
# ===========================================================================
def _regime_best(db: Session):
    """Aggregate the best wallets/strategies/clusters per regime."""
    wallets = db.scalars(select(mim.MiWalletRegime)).all()
    strats = db.scalars(select(mim.MiStrategyRegime)).all()
    by_regime_wallets: dict[str, list] = {}
    by_regime_clusters: dict[str, dict] = {}
    for w in wallets:
        for rg, v in (w.by_regime or {}).items():
            if v["trades"] >= 3:
                by_regime_wallets.setdefault(rg, []).append((w.wallet_address, v["win_rate"], v["roi"], w.cluster))
                by_regime_clusters.setdefault(rg, {})[w.cluster] = by_regime_clusters.setdefault(rg, {}).get(w.cluster, 0) + 1
    by_regime_strats: dict[str, list] = {}
    for s in strats:
        for rg, v in (s.by_regime or {}).items():
            if v["trades"] >= 3:
                by_regime_strats.setdefault(rg, []).append((s.name, v["roi"], v["win_rate"]))
    return by_regime_wallets, by_regime_strats, by_regime_clusters


def recommendations(db: Session, *, limit_markets: int = 25) -> dict:
    rw, rs, rc = _regime_best(db)
    profiles = db.scalars(select(mim.MiMarketProfile).order_by(
        mim.MiMarketProfile.created_time.desc()).limit(limit_markets)).all()
    cons = btc5m.consensus(db)
    cons_strength = round(len(cons.get("consensus_groups", [])) / 5.0, 3)
    generated = 0
    for p in profiles:
        if db.scalar(select(mim.MiRecommendation).where(mim.MiRecommendation.market_id == p.market_id)):
            continue
        rg = p.primary_regime
        # analog markets: same regime, nearest by net_move + volatility
        analogs = []
        for q in db.scalars(select(mim.MiMarketProfile).where(
                mim.MiMarketProfile.primary_regime == rg,
                mim.MiMarketProfile.market_id != p.market_id)).all():
            d = abs((q.price or {}).get("net_move", 0) - (p.price or {}).get("net_move", 0)) \
                + abs((q.price or {}).get("prob_volatility", 0) - (p.price or {}).get("prob_volatility", 0))
            analogs.append((q.market_id, round(d, 4), q.final_outcome))
        analogs.sort(key=lambda x: x[1])
        wl = sorted(rw.get(rg, []), key=lambda x: (-x[1], -x[2]))[:5]
        sl = sorted(rs.get(rg, []), key=lambda x: -x[1])[:5]
        cl = sorted((rc.get(rg, {}) or {}).items(), key=lambda x: -x[1])[:3]
        edge = round(_mean([w[2] for w in wl]), 4) if wl else 0.0
        rconf = round(_clip(0.3 + len(wl) * 0.08 + p.regime_confidence * 0.3, 0, 0.95), 2)
        db.add(mim.MiRecommendation(
            market_id=p.market_id, market_question=p.question, regime=rg,
            analog_markets=[{"market_id": a[0], "distance": a[1], "outcome": a[2]} for a in analogs[:5]],
            best_wallets=[{"wallet": w[0], "win_rate": w[1], "roi": w[2], "cluster": w[3]} for w in wl],
            best_strategies=[{"name": s[0], "roi": s[1], "win_rate": s[2]} for s in sl],
            best_clusters=[{"cluster": c[0], "count": c[1]} for c in cl],
            consensus_strength=cons_strength, expected_edge=edge, research_confidence=rconf))
        generated += 1
    db.commit()
    return {"generated": generated}


# ===========================================================================
# Phase 6 (history) — append-only decay snapshots
# ===========================================================================
def _snapshot_decay(db: Session) -> int:
    n = 0
    for w in db.scalars(select(mim.MiWalletRegime).where(
            mim.MiWalletRegime.profitable.is_(True))).all():
        d = (w.decay or {}).get("7d", {})
        db.add(mim.MiDecaySnapshot(kind="wallet", entity=w.wallet_address, window="7d",
               trades=d.get("trades", 0), win_rate=d.get("win_rate", 0.0), roi=d.get("roi", 0.0),
               trend=(w.decay or {}).get("trend", "stable")))
        n += 1
    for s in db.scalars(select(mim.MiStrategyRegime)).all():
        d = (s.decay or {}).get("7d", {})
        db.add(mim.MiDecaySnapshot(kind="strategy", entity=s.name, window="7d",
               trades=d.get("trades", 0), win_rate=d.get("win_rate", 0.0), roi=d.get("roi", 0.0),
               trend=(s.decay or {}).get("trend", "stable")))
        n += 1
    db.commit()
    return n


# ===========================================================================
# Phase 11 — nightly research review (permanent)
# ===========================================================================
def nightly_review(db: Session) -> dict:
    profiles = db.scalars(select(mim.MiMarketProfile)).all()
    dist: dict[str, int] = {}
    for p in profiles:
        dist[p.primary_regime] = dist.get(p.primary_regime, 0) + 1
    wallets = db.scalars(select(mim.MiWalletRegime)).all()
    strats = db.scalars(select(mim.MiStrategyRegime)).all()
    leaders = sorted(wallets, key=lambda w: -w.originality_score)[:5]
    decaying_w = [w.wallet_address for w in wallets if (w.decay or {}).get("trend") in ("decaying", "broken")]
    decaying_s = [s.name for s in strats if (s.decay or {}).get("trend") in ("decaying", "broken")]
    top_by_regime = {}
    rw, rs, _ = _regime_best(db)
    for rg in LEADERBOARD_REGIMES:
        wl = sorted(rw.get(rg, []), key=lambda x: -x[1])[:3]
        if wl:
            top_by_regime[rg] = [{"wallet": w[0], "win_rate": w[1]} for w in wl]
    # best / worst regime by avg wallet ROI
    reg_roi = {}
    for w in wallets:
        for rg, v in (w.by_regime or {}).items():
            reg_roi.setdefault(rg, []).append(v["roi"])
    reg_avg = {rg: round(_mean(v), 4) for rg, v in reg_roi.items() if v}
    cf = db.scalar(select(mim.MiCounterfactual).order_by(mim.MiCounterfactual.created_at.desc()))
    report = {
        "new_markets": len(profiles),
        "regimes_discovered": sorted(dist.keys()),
        "regime_distribution": dist,
        "top_wallets_by_regime": top_by_regime,
        "top_strategies_by_regime": {rg: [{"name": s[0], "roi": s[1]} for s in sorted(rs.get(rg, []), key=lambda x: -x[1])[:3]]
                                     for rg in LEADERBOARD_REGIMES if rs.get(rg)},
        "decay_warnings": {"wallets": decaying_w[:10], "strategies": decaying_s[:10]},
        "new_leader_wallets": [{"wallet": w.wallet_address, "originality": w.originality_score,
                                "role": (w.originality or {}).get("role")} for w in leaders],
        "copied_wallet_detection": [w.wallet_address for w in wallets
                                    if (w.originality or {}).get("role") == "follower"][:10],
        "best_counterfactual_improvement": ({"optimal_shift_s": cf.optimal_shift_s,
                                             "expected_improvement": cf.expected_improvement} if cf else None),
        "most_profitable_regime": (max(reg_avg, key=reg_avg.get) if reg_avg else None),
        "worst_regime": (min(reg_avg, key=reg_avg.get) if reg_avg else None),
        "research_recommendations": _intel_recommendations(dist, reg_avg, decaying_s),
    }
    summary = (f"{report['new_markets']} markets across {len(dist)} regimes; "
               f"most profitable regime: {report['most_profitable_regime']}; "
               f"{len(decaying_w)} wallets + {len(decaying_s)} strategies decaying; "
               f"{len(leaders)} leader wallets.")
    row = mim.MiNightlyReview(summary=summary, report=report)
    db.add(row)
    db.commit()
    return {"id": row.id, "summary": summary, "report": report}


def _intel_recommendations(dist, reg_avg, decaying_s):
    recs = []
    if reg_avg:
        best = max(reg_avg, key=reg_avg.get)
        recs.append(f"Concentrate research on the '{best}' regime (highest avg wallet ROI).")
    if decaying_s:
        recs.append(f"Investigate {len(decaying_s)} decaying strategies before relying on them.")
    if dist.get("Mixed", 0) > sum(dist.values()) * 0.4:
        recs.append("Many markets are 'Mixed' — tighten regime thresholds or add features.")
    return recs[:6]


# ===========================================================================
# Orchestrator (Phase 1-11) — idempotent, reproducible
# ===========================================================================
def run_intel_batch(db: Session, *, refresh_lab: bool = True, limit_markets: int | None = 150) -> dict:
    """One Market-Intelligence cycle. Optionally refreshes the Lab first, then
    builds profiles, classifies regimes, computes wallet/strategy specialization,
    decay, originality, position-size intel, counterfactuals, recommendations, and
    a permanent nightly review. Read-only w.r.t. production."""
    lab = {}
    if refresh_lab:
        lab = btc5m.refresh(db, limit_markets=limit_markets, train=True)
    prof = build_profiles(db)
    wr = wallet_regime(db)
    sr = strategy_regime(db)
    og = originality_graph(db)
    cf = counterfactual(db)
    rec = recommendations(db)
    snaps = _snapshot_decay(db)
    review = nightly_review(db)
    return {
        "lab_champion": lab.get("champion"),
        "profiles": prof, "wallet_regime": wr, "strategy_regime": sr,
        "originality": {"nodes": og["nodes"], "leaders": len(og["leaders"])},
        "counterfactual": cf, "recommendations": rec, "decay_snapshots": snaps,
        "nightly_review_id": review["id"], "nightly_summary": review["summary"],
    }


# ===========================================================================
# Read APIs for the frontend
# ===========================================================================
def dashboard(db: Session) -> dict:
    n_profiles = db.scalar(select(func.count()).select_from(mim.MiMarketProfile)) or 0
    dist: dict[str, int] = {}
    for p in db.scalars(select(mim.MiMarketProfile)).all():
        dist[p.primary_regime] = dist.get(p.primary_regime, 0) + 1
    n_wallets = db.scalar(select(func.count()).select_from(mim.MiWalletRegime)) or 0
    n_strats = db.scalar(select(func.count()).select_from(mim.MiStrategyRegime)) or 0
    leaders = db.scalars(select(mim.MiWalletRegime).order_by(
        mim.MiWalletRegime.originality_score.desc()).limit(5)).all()
    last = db.scalar(select(mim.MiNightlyReview).order_by(mim.MiNightlyReview.created_at.desc()))
    cf = db.scalar(select(mim.MiCounterfactual).order_by(mim.MiCounterfactual.created_at.desc()))
    return {
        "markets_classified": n_profiles,
        "regimes_discovered": len(dist),
        "regime_distribution": dist,
        "wallets_profiled": n_wallets,
        "strategies_profiled": n_strats,
        "leader_wallets": [{"wallet": w.wallet_address, "originality": w.originality_score,
                            "role": (w.originality or {}).get("role")} for w in leaders],
        "counterfactual": ({"optimal_shift_s": cf.optimal_shift_s,
                            "expected_improvement": cf.expected_improvement} if cf else None),
        "last_review": ({"summary": last.summary, "created_at": last.created_at.isoformat()} if last else None),
        "safety": "read-only market intelligence — never trades or changes production",
    }


def markets(db: Session, *, regime: str | None = None, limit: int = 200) -> list[dict]:
    q = select(mim.MiMarketProfile).order_by(mim.MiMarketProfile.created_time.desc())
    if regime:
        q = q.where(mim.MiMarketProfile.primary_regime == regime)
    rows = db.scalars(q.limit(limit)).all()
    return [{"market_id": p.market_id, "question": p.question, "regime": p.primary_regime,
             "secondary_regime": p.secondary_regime, "regime_confidence": p.regime_confidence,
             "resolved": p.resolved, "final_outcome": p.final_outcome,
             "net_move": (p.price or {}).get("net_move"), "prob_volatility": (p.price or {}).get("prob_volatility"),
             "total_volume": (p.volume or {}).get("total_volume"),
             "created_time": p.created_time.isoformat() if p.created_time else None} for p in rows]


def market_detail(db: Session, market_id: str) -> dict | None:
    p = db.get(mim.MiMarketProfile, market_id)
    if not p:
        return None
    rec = db.scalar(select(mim.MiRecommendation).where(mim.MiRecommendation.market_id == market_id))
    return {"market_id": p.market_id, "question": p.question, "primary_regime": p.primary_regime,
            "secondary_regime": p.secondary_regime, "regime_confidence": p.regime_confidence,
            "regime_evidence": p.regime_evidence, "duration_s": p.duration_s, "resolved": p.resolved,
            "final_outcome": p.final_outcome, "price": p.price, "volume": p.volume,
            "orderflow": p.orderflow, "timing": p.timing, "feature_means": p.feature_means,
            "recommendation": ({"regime": rec.regime, "best_wallets": rec.best_wallets,
                                "best_strategies": rec.best_strategies, "best_clusters": rec.best_clusters,
                                "analog_markets": rec.analog_markets, "expected_edge": rec.expected_edge,
                                "research_confidence": rec.research_confidence} if rec else None)}


def regime_distribution(db: Session) -> dict:
    dist: dict[str, dict] = {}
    for p in db.scalars(select(mim.MiMarketProfile)).all():
        d = dist.setdefault(p.primary_regime, {"count": 0, "resolved": 0, "avg_confidence": []})
        d["count"] += 1
        d["resolved"] += 1 if p.resolved else 0
        d["avg_confidence"].append(p.regime_confidence)
    out = []
    for rg, d in dist.items():
        out.append({"regime": rg, "count": d["count"], "resolved": d["resolved"],
                    "avg_confidence": round(_mean(d["avg_confidence"]), 2)})
    out.sort(key=lambda x: -x["count"])
    return {"regimes": out, "total": sum(x["count"] for x in out)}


def wallet_specialization(db: Session, *, limit: int = 100) -> list[dict]:
    rows = db.scalars(select(mim.MiWalletRegime).order_by(
        mim.MiWalletRegime.specialization_score.desc()).limit(limit)).all()
    return [{"wallet": w.wallet_address, "cluster": w.cluster, "profitable": w.profitable,
             "best_regime": w.best_regime, "specialization_score": w.specialization_score,
             "by_regime": w.by_regime, "decay": w.decay, "originality": w.originality,
             "originality_score": w.originality_score, "position_size": w.position_size,
             "trade_count": w.trade_count} for w in rows]


def strategy_specialization(db: Session) -> list[dict]:
    rows = db.scalars(select(mim.MiStrategyRegime)).all()
    return [{"strategy_id": s.strategy_id, "name": s.name, "archetype": s.archetype,
             "best_regime": s.best_regime, "worst_regime": s.worst_regime,
             "by_regime": s.by_regime, "decay": s.decay} for s in rows]


def regime_leaderboards(db: Session) -> dict:
    rw, _, _ = _regime_best(db)
    out = {}
    for rg in LEADERBOARD_REGIMES:
        ranked = sorted(rw.get(rg, []), key=lambda x: (-x[1], -x[2]))[:10]
        out[rg] = [{"wallet": w[0], "win_rate": w[1], "roi": w[2], "cluster": w[3]} for w in ranked]
    return {"leaderboards": out}


def decay_analysis(db: Session) -> dict:
    wallets = db.scalars(select(mim.MiWalletRegime)).all()
    strats = db.scalars(select(mim.MiStrategyRegime)).all()
    def _summ(rows, name_attr):
        return [{"entity": getattr(r, name_attr), "trend": (r.decay or {}).get("trend"),
                 "trend_confidence": (r.decay or {}).get("trend_confidence"),
                 "win_rate_7d": ((r.decay or {}).get("7d") or {}).get("win_rate"),
                 "win_rate_lifetime": ((r.decay or {}).get("lifetime") or {}).get("win_rate")}
                for r in rows if (r.decay or {}).get("lifetime", {}).get("trades", 0) >= 3]
    return {"wallets": sorted(_summ(wallets, "wallet_address"), key=lambda x: x["trend"] or ""),
            "strategies": sorted(_summ(strats, "name"), key=lambda x: x["trend"] or "")}


def originality(db: Session, *, limit: int = 100) -> dict:
    rows = db.scalars(select(mim.MiWalletRegime).order_by(
        mim.MiWalletRegime.originality_score.desc()).limit(limit)).all()
    return {"wallets": [{"wallet": w.wallet_address, "originality_score": w.originality_score,
                         **(w.originality or {})} for w in rows if w.originality]}


def counterfactual_results(db: Session, *, limit: int = 10) -> dict:
    rows = db.scalars(select(mim.MiCounterfactual).order_by(
        mim.MiCounterfactual.created_at.desc()).limit(limit)).all()
    return {"results": [{"scope": r.scope, "trades_tested": r.trades_tested,
                         "optimal_shift_s": r.optimal_shift_s, "expected_improvement": r.expected_improvement,
                         "timing_sensitivity": r.timing_sensitivity,
                         "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]}


def market_recommendations(db: Session, *, limit: int = 50) -> list[dict]:
    rows = db.scalars(select(mim.MiRecommendation).order_by(
        mim.MiRecommendation.created_at.desc()).limit(limit)).all()
    return [{"market_id": r.market_id, "market": r.market_question, "regime": r.regime,
             "best_wallets": r.best_wallets, "best_strategies": r.best_strategies,
             "best_clusters": r.best_clusters, "analog_markets": r.analog_markets,
             "consensus_strength": r.consensus_strength, "expected_edge": r.expected_edge,
             "research_confidence": r.research_confidence} for r in rows]


def nightly_reviews(db: Session, *, limit: int = 30) -> list[dict]:
    rows = db.scalars(select(mim.MiNightlyReview).order_by(
        mim.MiNightlyReview.created_at.desc()).limit(limit)).all()
    return [{"id": r.id, "summary": r.summary, "report": r.report,
             "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]
