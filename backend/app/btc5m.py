"""BTC 5M Reversal Lab — isolated research/analytics module.

Reverse-engineers the observable behaviour of consistently profitable traders on
Polymarket's BTC 5-minute markets. 100% READ-ONLY research: it reads the already
indexed Market/Trade tables and writes only to its own btc5m_* tables. It NEVER
submits orders, changes rankings/eligibility/discovery, or touches live trading.

Phases:
  1. Dataset builder          -> index_dataset()
  2. Wallet fingerprinting    -> fingerprint_wallets()  (+ Wallet IQ)
  3. Market reconstruction    -> reconstruct_features()  (feature vector / trade)
  4. Strategy reverse-eng.    -> train_models()          (compare + champion)
  5. Wallet clustering        -> assign_cluster()
  6. Consensus analysis       -> consensus()
  7. Shadow strategy (paper)  -> generate_shadow_signals() / score_shadow_signals()
  8. Continuous learning      -> refresh()  (idempotent orchestrator)
"""
from __future__ import annotations

import math
import re
import statistics
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import btc5m_ml as ml
from . import btc5m_models as bm
from .models import Market, Trade, Wallet

# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
MARKET_LIFE_SECONDS = 300            # BTC 5m markets last ~5 minutes
MIN_WALLET_TRADES = 8                # min BTC5m trades to fingerprint a wallet
PROFITABLE_MIN_ROI = 0.0            # ROI > 0 to be "profitable"
CONSENSUS_WINDOW_S = 30              # co-entry window for agreement/consensus

# Pattern set for "Bitcoin Up or Down 5m / BTC 5 Minute / BTC 5m" + equivalents.
_BTC = r"(btc|bitcoin)"
_FIVE = r"(5\s*-?\s*m(in(ute)?s?)?\b|5\s*minute|five\s*minute)"
BTC5M_RE = re.compile(rf"{_BTC}.*{_FIVE}|{_FIVE}.*{_BTC}|bitcoin\s+up\s+or\s+down", re.I)


# ---------------------------------------------------------------------------
# small math helpers (pure python; no numpy)
# ---------------------------------------------------------------------------
def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs):
    return statistics.pstdev(xs) if len(xs) > 1 else 0.0


def _slope(xs):
    """OLS slope of xs against its index (trend direction)."""
    n = len(xs)
    if n < 2:
        return 0.0
    mx = (n - 1) / 2.0
    my = _mean(xs)
    num = sum((i - mx) * (xs[i] - my) for i in range(n))
    den = sum((i - mx) ** 2 for i in range(n))
    return num / den if den else 0.0


def _ema(xs, span):
    if not xs:
        return 0.0
    k = 2.0 / (span + 1)
    e = xs[0]
    for v in xs[1:]:
        e = v * k + e * (1 - k)
    return e


def _rsi(xs):
    if len(xs) < 2:
        return 50.0
    gains, losses = [], []
    for a, b in zip(xs, xs[1:]):
        d = b - a
        (gains if d >= 0 else losses).append(abs(d))
    ag = _mean(gains) if gains else 0.0
    al = _mean(losses) if losses else 0.0
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    rs = ag / al
    return 100.0 - (100.0 / (1.0 + rs))


def _vwap(prices, weights):
    tot = sum(weights)
    if tot <= 0:
        return _mean(prices)
    return sum(p * w for p, w in zip(prices, weights)) / tot


def _clip(v, lo, hi):
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# Phase 1 — dataset builder
# ---------------------------------------------------------------------------
def is_btc5m_market(question: str | None, slug: str | None = None, category: str | None = None) -> bool:
    """Identify a BTC short-duration market from its text. Requires a Bitcoin
    reference AND a 5-minute/short-duration cue (so generic 'Bitcoin' daily markets
    are excluded)."""
    text = " ".join(x for x in (question, slug) if x)
    if not text:
        return False
    if not re.search(_BTC, text, re.I) and "up or down" not in text.lower():
        return False
    return bool(BTC5M_RE.search(text))


def _yes_no(outcome: str | None) -> str:
    o = (outcome or "").strip().lower()
    if o in ("yes", "up", "higher", "above", "long"):
        return "YES"
    if o in ("no", "down", "lower", "below", "short"):
        return "NO"
    # default: treat the first listed/affirmative outcome as YES
    return "YES" if o in ("", "yes") else "NO"


def find_btc5m_markets(db: Session, *, limit: int | None = None) -> list[Market]:
    """Scan the already-indexed Market table for BTC 5m markets (Phase 1 reuses the
    existing discovery/indexing infrastructure — it only reads it)."""
    rows = db.scalars(select(Market)).all()
    found = [m for m in rows if is_btc5m_market(m.question, m.slug, m.category)]
    found.sort(key=lambda m: m.created_at or datetime.min, reverse=True)
    return found[:limit] if limit else found


def _market_expiry(m: Market) -> datetime | None:
    if m.resolved_at:
        return m.resolved_at
    if m.created_at:
        return m.created_at + timedelta(seconds=MARKET_LIFE_SECONDS)
    return None


def index_dataset(db: Session, *, limit_markets: int | None = 50) -> dict:
    """Index BTC 5m markets + their trades into the btc5m_* tables (idempotent).
    For each trade it stores derived timing fields and a reconstructed pre-entry
    feature vector. Returns counts."""
    markets = find_btc5m_markets(db, limit=limit_markets)
    n_markets = n_trades = 0
    wallets_seen: set[str] = set()
    for m in markets:
        expiry = _market_expiry(m)
        # source trades for this market, chronological
        src = db.scalars(select(Trade).where(Trade.market_id == m.id)
                         .order_by(Trade.timestamp.asc())).all()
        # wallet address lookup
        wids = {t.wallet_id for t in src}
        waddr = {w.id: w.address for w in db.scalars(
            select(Wallet).where(Wallet.id.in_(wids))).all()} if wids else {}

        rec = db.get(bm.Btc5mMarket, m.id) or bm.Btc5mMarket(market_id=m.id)
        rec.question = m.question or ""
        rec.slug = m.slug
        rec.condition_id = m.id
        rec.token_ids = list(m.token_ids or [])
        rec.outcomes = list(m.outcomes or [])
        rec.created_time = m.created_at
        rec.resolution_time = m.resolved_at
        rec.expiry = expiry
        rec.resolved = bool(m.resolved)
        rec.final_outcome = m.resolved_outcome
        rec.volume = float(m.volume or 0.0)
        rec.liquidity = float(m.liquidity or 0.0)
        rec.trade_count = len(src)
        rec.wallet_count = len(wids)
        db.add(rec)

        prior: list[dict] = []     # accumulating market state for feature reconstruction
        for t in src:
            addr = waddr.get(t.wallet_id, f"id:{t.wallet_id}")
            wallets_seen.add(addr)
            direction = _yes_no(t.outcome)
            secs_from_creation = int((t.timestamp - m.created_at).total_seconds()) if m.created_at else None
            secs_until_expiry = int((expiry - t.timestamp).total_seconds()) if expiry else None
            shares = (t.size / t.price) if t.price else 0.0
            won = None
            realized = 0.0
            if m.resolved and m.resolved_outcome is not None:
                won = _yes_no(m.resolved_outcome) == direction
                if t.side == "buy":
                    realized = round(shares * (1.0 if won else 0.0) - t.size, 4)
            feats = reconstruct_features(prior, t, direction, secs_from_creation, secs_until_expiry, m)

            ext = t.external_id or f"src:{t.id}"
            existing = db.scalar(select(bm.Btc5mTrade).where(bm.Btc5mTrade.external_id == ext))
            row = existing or bm.Btc5mTrade(external_id=ext)
            row.source_trade_id = t.id
            row.market_id = m.id
            row.wallet_address = addr
            row.side = t.side
            row.direction = direction
            row.price = float(t.price)
            row.shares = round(shares, 4)
            row.usd_value = float(t.size)
            row.timestamp = t.timestamp
            row.seconds_from_creation = secs_from_creation
            row.seconds_until_expiry = secs_until_expiry
            row.opened_position = (t.side == "buy")
            row.realized_pnl = realized
            row.won = won
            row.features = feats
            row.label_direction = 1 if direction == "YES" else 0
            db.add(row)
            if not existing:
                n_trades += 1

            # update market state with this trade for the NEXT trade's reconstruction
            prior.append({"yes_price": t.price if direction == "YES" else 1.0 - t.price,
                          "usd": t.size, "side": t.side, "dir": direction})
        n_markets += 1
    db.commit()
    return {"markets_indexed": n_markets, "trades_indexed": n_trades,
            "wallets_seen": len(wallets_seen),
            "markets_matched": len(markets)}


# ---------------------------------------------------------------------------
# Phase 3 — market reconstruction (one feature vector per trade)
# ---------------------------------------------------------------------------
FEATURE_NAMES = [
    "market_yes_price", "time_remaining_frac", "secs_from_creation_norm",
    "prior_trades", "yes_fraction_prior", "buy_pressure",
    "ret_last", "trend_slope", "realized_vol", "ema_gap", "rsi", "macd",
    "vwap_gap", "boll_pos", "atr", "volume_prior_norm", "orderbook_imbalance",
]


def reconstruct_features(prior: list[dict], trade, direction: str,
                         secs_from_creation, secs_until_expiry, market) -> dict:
    """Reconstruct market state IMMEDIATELY BEFORE this trade, from prior trades in
    the same market (the market-implied YES-probability path) + timing. Indicators
    (EMA/VWAP/RSI/MACD/ATR/Bollinger/vol/slope) are computed over the implied-prob
    series — clearly market-derived, not raw BTC OHLC (which we don't index). No
    leakage: the current trade's own price/direction are NOT used as inputs.
    Returns {feature_name: value} for every FEATURE_NAMES key."""
    series = [p["yes_price"] for p in prior]
    weights = [p["usd"] for p in prior]
    n = len(series)
    last = series[-1] if series else 0.5
    diffs = [b - a for a, b in zip(series, series[1:])] if n >= 2 else [0.0]
    realized_vol = _std(diffs)
    ema = _ema(series, span=min(max(n, 1), 5)) if series else 0.5
    macd = (_ema(series, 3) - _ema(series, 6)) if n >= 2 else 0.0
    vwap = _vwap(series, weights) if series else 0.5
    mean_s = _mean(series) if series else 0.5
    std_s = _std(series)
    boll_pos = _clip((last - mean_s) / (2 * std_s), -1.5, 1.5) if std_s > 1e-6 else 0.0
    atr = _mean([abs(d) for d in diffs])
    buys = sum(1 for p in prior if p["side"] == "buy")
    sells = n - buys
    yes_prior = sum(1 for p in prior if p["dir"] == "YES")
    feats = {
        "market_yes_price": round(last, 4),
        "time_remaining_frac": round(_clip((secs_until_expiry or 0) / MARKET_LIFE_SECONDS, 0.0, 1.0), 4),
        "secs_from_creation_norm": round(_clip((secs_from_creation or 0) / MARKET_LIFE_SECONDS, 0.0, 2.0), 4),
        "prior_trades": round(math.log1p(n), 4),
        "yes_fraction_prior": round(yes_prior / n, 4) if n else 0.5,
        "buy_pressure": round((buys - sells) / n, 4) if n else 0.0,
        "ret_last": round(diffs[-1], 4),
        "trend_slope": round(_slope(series), 4),
        "realized_vol": round(realized_vol, 4),
        "ema_gap": round(last - ema, 4),
        "rsi": round(_rsi(series) / 100.0, 4),
        "macd": round(macd, 4),
        "vwap_gap": round(last - vwap, 4),
        "boll_pos": round(boll_pos, 4),
        "atr": round(atr, 4),
        "volume_prior_norm": round(math.log1p(sum(weights)) / 10.0, 4),
        "orderbook_imbalance": 0.0,   # book snapshots not indexed for historical 5m markets
    }
    return feats


def feature_vector(feats: dict) -> list[float]:
    return [float(feats.get(k, 0.0)) for k in FEATURE_NAMES]


# ---------------------------------------------------------------------------
# Phase 2 — wallet fingerprinting + Wallet IQ
# ---------------------------------------------------------------------------
def _max_drawdown(pnls: list[float]) -> float:
    """Max peak-to-trough drawdown of the cumulative P/L curve, as a fraction of
    the peak (0..1)."""
    cum = 0.0
    peak = 0.0
    mdd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        if peak > 0:
            mdd = max(mdd, (peak - cum) / peak)
    return round(mdd, 4)


def _streaks(wins: list[bool]) -> tuple[int, int]:
    best_w = best_l = cur_w = cur_l = 0
    for w in wins:
        if w:
            cur_w += 1
            cur_l = 0
        else:
            cur_l += 1
            cur_w = 0
        best_w = max(best_w, cur_w)
        best_l = max(best_l, cur_l)
    return best_w, best_l


def _detect_sizing(sizes: list[float], wins_seq: list[bool]) -> str:
    """Classify sizing behaviour: fixed / volatility / martingale / anti-martingale."""
    if len(sizes) < 4:
        return "insufficient"
    cv = (_std(sizes) / _mean(sizes)) if _mean(sizes) else 0.0
    if cv < 0.12:
        return "fixed"
    # martingale: size UP after a loss; anti: size UP after a win
    up_after_loss = up_after_win = n_loss = n_win = 0
    for i in range(1, len(sizes)):
        bigger = sizes[i] > sizes[i - 1] * 1.05
        if not wins_seq[i - 1]:
            n_loss += 1
            up_after_loss += 1 if bigger else 0
        else:
            n_win += 1
            up_after_win += 1 if bigger else 0
    mart = (up_after_loss / n_loss) if n_loss else 0.0
    anti = (up_after_win / n_win) if n_win else 0.0
    if mart > 0.6 and mart > anti:
        return "martingale"
    if anti > 0.6 and anti > mart:
        return "anti_martingale"
    return "volatility" if cv > 0.4 else "variable"


def _fingerprint_one(trades: list[bm.Btc5mTrade], global_share: dict) -> dict:
    """Compute the full fingerprint for one wallet from its BTC5m trades."""
    trades = sorted(trades, key=lambda t: t.timestamp)
    buys = [t for t in trades if t.side == "buy"]
    settled = [t for t in buys if t.won is not None]
    pnls = [t.realized_pnl for t in settled]
    wins_seq = [bool(t.won) for t in settled]
    invested = sum(t.usd_value for t in settled) or 1.0
    realized = sum(pnls)
    wins = sum(1 for t in settled if t.won)
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    sizes = [t.usd_value for t in buys]
    entries_after_open = [t.seconds_from_creation for t in buys if t.seconds_from_creation is not None]
    secs_before_expiry = [t.seconds_until_expiry for t in buys if t.seconds_until_expiry is not None]
    yes = sum(1 for t in buys if t.direction == "YES")
    best_w, best_l = _streaks(wins_seq)

    # entry/exit timing histograms (5 buckets across the 5-min life)
    def _hist(vals):
        h = [0, 0, 0, 0, 0]
        for v in vals:
            b = int(_clip(v / MARKET_LIFE_SECONDS, 0, 0.999) * 5)
            h[min(b, 4)] += 1
        return h

    roi = round(realized / invested, 4)
    metrics = {
        "trade_count": len(trades),
        "buy_count": len(buys),
        "settled_count": len(settled),
        "roi": roi,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else (round(gross_win, 3) or 0.0),
        "win_rate": round(wins / len(settled), 4) if settled else 0.0,
        "realized_pnl": round(realized, 2),
        "avg_trade_size": round(_mean(sizes), 2),
        "largest_trade": round(max(sizes), 2) if sizes else 0.0,
        "max_drawdown": _max_drawdown(pnls),
        "trade_frequency_per_market": round(len(trades) / len({t.market_id for t in trades}), 2) if trades else 0.0,
        # timing
        "avg_entry_after_open_s": round(_mean(entries_after_open), 1),
        "avg_secs_before_expiry": round(_mean(secs_before_expiry), 1),
        "entry_timing_hist": _hist(entries_after_open),
        "exit_timing_hist": _hist([MARKET_LIFE_SECONDS - s for s in secs_before_expiry]),
        # pricing
        "avg_entry_price": round(_mean([t.price for t in buys]), 4),
        "preferred_price_lo": round(min((t.price for t in buys), default=0.0), 3),
        "preferred_price_hi": round(max((t.price for t in buys), default=0.0), 3),
        # holding
        "hold_to_settlement_pct": round(len(settled) / len(buys), 4) if buys else 0.0,
        "early_exit_pct": round(sum(1 for t in trades if t.side == "sell") / len(buys), 4) if buys else 0.0,
        # direction
        "yes_pct": round(yes / len(buys), 4) if buys else 0.0,
        "no_pct": round(1 - yes / len(buys), 4) if buys else 0.0,
        # sizing (over SETTLED buys so sizes align with win/loss outcomes)
        "sizing_behavior": _detect_sizing([t.usd_value for t in settled], wins_seq),
        # consistency
        "longest_win_streak": best_w,
        "longest_loss_streak": best_l,
        "rolling_roi_last10": round(sum(pnls[-10:]) / (sum(t.usd_value for t in settled[-10:]) or 1.0), 4),
        # specialization (from the GLOBAL trade table for this wallet)
        "btc5m_pct": global_share.get("btc5m_pct", 1.0),
        "crypto_pct": global_share.get("crypto_pct", 1.0),
    }
    return metrics


CLUSTERS = ["Momentum", "Mean Reversion", "Breakout", "Trend Following", "Volatility",
            "Scalping", "Contrarian", "News Reaction", "Orderflow", "Hybrid", "Unknown"]


def assign_cluster(metrics: dict, feat_means: dict) -> tuple[str, float]:
    """Heuristic, explainable cluster assignment from the fingerprint + average
    pre-entry features. Returns (cluster, confidence 0..1)."""
    entry = metrics.get("avg_entry_after_open_s", 150)
    yes_pct = metrics.get("yes_pct", 0.5)
    hold = metrics.get("hold_to_settlement_pct", 1.0)
    early = metrics.get("early_exit_pct", 0.0)
    trend = feat_means.get("trend_slope", 0.0)
    vol = feat_means.get("realized_vol", 0.0)
    boll = feat_means.get("boll_pos", 0.0)
    directional = abs(yes_pct - 0.5) * 2          # 0 (balanced) .. 1 (one-sided)

    scores = {
        # early entry, follows the trend direction
        "Momentum": (1 if entry < 60 else 0) * 0.6 + max(0.0, trend * 6) + directional * 0.3,
        # buys against the current move (negative boll position / counter-trend)
        "Mean Reversion": max(0.0, -boll) * 0.7 + (0.4 if abs(trend) < 0.005 else 0.0),
        # acts when price breaks its band (high |boll|) with momentum
        "Breakout": max(0.0, abs(boll) - 0.5) * 0.8 + max(0.0, trend * 4),
        # rides direction, holds to settlement
        "Trend Following": (0.5 if hold > 0.8 else 0.0) + max(0.0, abs(trend) * 5) + directional * 0.2,
        # trades the highest-volatility states (only when clearly elevated)
        "Volatility": _clip((vol - 0.04) * 12, 0.0, 0.8),
        # very fast in/out, exits early
        "Scalping": (0.6 if early > 0.4 else 0.0) + (0.4 if entry < 90 else 0.0),
        # one-sided against crowd / late entries
        "Contrarian": max(0.0, -boll) * 0.5 + (0.3 if entry > 180 else 0.0) + directional * 0.2,
        # late, reactive entries near expiry
        "News Reaction": (0.6 if entry > 200 else 0.0),
        # responds to order-flow imbalance (needs genuine two-sided flow, not just
        # an all-buy book, so it doesn't trivially win)
        "Orderflow": _clip((abs(feat_means.get("buy_pressure", 0.0)) - 0.5) * 1.2, 0.0, 0.7)
        + min(0.3, feat_means.get("orderbook_imbalance", 0.0)),
    }
    best = max(scores, key=scores.get)
    top = scores[best]
    ranked = sorted(scores.values(), reverse=True)
    margin = ranked[0] - (ranked[1] if len(ranked) > 1 else 0.0)
    if top < 0.25:
        return "Hybrid", round(_clip(0.3 + top, 0, 1), 2)
    conf = _clip(0.45 + top * 0.35 + margin * 0.3, 0.0, 0.99)
    return best, round(conf, 2)


def _wallet_iq(metrics: dict, cluster: str, feat_means: dict) -> dict:
    """Generate the human-readable Wallet IQ card."""
    entry = metrics.get("avg_entry_after_open_s", 0)
    hold_s = MARKET_LIFE_SECONDS * metrics.get("hold_to_settlement_pct", 1.0)
    avg_size = metrics.get("avg_trade_size", 0)
    confidence = "high" if avg_size >= 5 else "medium" if avg_size >= 2 else "low"
    trend = feat_means.get("trend_slope", 0.0)
    strength = "strong trending markets" if abs(trend) > 0.004 else "balanced order-flow"
    weakness = "range-bound / choppy markets" if abs(trend) > 0.004 else "fast directional breakouts"
    # copy confidence 0..100 from ROI, PF, win rate, consistency & sample size
    roi = metrics.get("roi", 0.0)
    pf = metrics.get("profit_factor", 0.0)
    wr = metrics.get("win_rate", 0.0)
    n = metrics.get("settled_count", 0)
    sample = _clip(n / 40.0, 0.2, 1.0)
    raw = (_clip(roi * 2, -0.5, 1.0) * 35 + _clip((pf - 1) / 2, 0, 1) * 30
           + _clip((wr - 0.5) * 2, 0, 1) * 20 + 15) * sample
    copy_conf = int(_clip(raw, 0, 99))
    return {
        "strategy": cluster,
        "average_entry": f"{int(entry)} seconds after market open",
        "average_hold": f"{int(hold_s // 60)}m {int(hold_s % 60):02d}s",
        "average_confidence": confidence,
        "strength": strength,
        "weakness": weakness,
        "copy_confidence": copy_conf,
    }


def _global_share(db: Session, address: str, btc5m_market_ids: set[str]) -> dict:
    """Specialization: what fraction of the wallet's GLOBAL trades are BTC5m / crypto."""
    w = db.scalar(select(Wallet).where(func.lower(Wallet.address) == address.lower()))
    if not w:
        return {"btc5m_pct": 1.0, "crypto_pct": 1.0}
    total = db.scalar(select(func.count()).select_from(Trade).where(Trade.wallet_id == w.id)) or 0
    if not total:
        return {"btc5m_pct": 1.0, "crypto_pct": 1.0}
    btc = db.scalar(select(func.count()).select_from(Trade).where(
        Trade.wallet_id == w.id, Trade.market_id.in_(btc5m_market_ids))) or 0
    return {"btc5m_pct": round(btc / total, 3), "crypto_pct": round(btc / total, 3)}


def fingerprint_wallets(db: Session) -> dict:
    """Compute fingerprints + Wallet IQ + cluster for every wallet with enough
    BTC5m history (Phase 2 + Phase 5). Idempotent upsert into wallet profiles."""
    rows = db.scalars(select(bm.Btc5mTrade)).all()
    by_wallet: dict[str, list] = {}
    for t in rows:
        by_wallet.setdefault(t.wallet_address, []).append(t)
    market_ids = {t.market_id for t in rows}

    n_profiles = n_profitable = 0
    cluster_counts: dict[str, int] = {}
    for addr, trs in by_wallet.items():
        if len(trs) < MIN_WALLET_TRADES:
            continue
        try:
            share = _global_share(db, addr, market_ids)
            metrics = _fingerprint_one(trs, share)
            feat_means = _avg_features(trs)
            cluster, conf = assign_cluster(metrics, feat_means)
            iq = _wallet_iq(metrics, cluster, feat_means)
        except Exception as exc:  # noqa: BLE001  (one messy wallet must not fail the batch)
            print(f"[btc5m] fingerprint skipped {addr[:12]}: {type(exc).__name__}: {exc}")
            continue
        profitable = metrics["roi"] > PROFITABLE_MIN_ROI and metrics["settled_count"] >= 5

        prof = db.get(bm.Btc5mWalletProfile, addr) or bm.Btc5mWalletProfile(wallet_address=addr)
        prof.trade_count = metrics["trade_count"]
        prof.settled_count = metrics["settled_count"]
        prof.roi = metrics["roi"]
        prof.profit_factor = metrics["profit_factor"]
        prof.win_rate = metrics["win_rate"]
        prof.realized_pnl = metrics["realized_pnl"]
        prof.avg_trade_size = metrics["avg_trade_size"]
        prof.profitable = profitable
        prof.cluster = cluster
        prof.cluster_confidence = conf
        prof.metrics = metrics
        prof.wallet_iq = iq
        db.add(prof)
        n_profiles += 1
        n_profitable += 1 if profitable else 0
        cluster_counts[cluster] = cluster_counts.get(cluster, 0) + 1
    db.commit()
    return {"profiles": n_profiles, "profitable": n_profitable, "clusters": cluster_counts}


def _avg_features(trades: list[bm.Btc5mTrade]) -> dict:
    """Mean of each reconstructed feature across a wallet's trades."""
    if not trades:
        return {}
    out = {}
    for k in FEATURE_NAMES:
        out[k] = round(_mean([float(t.features.get(k, 0.0)) for t in trades if t.features]), 4)
    return out


# ---------------------------------------------------------------------------
# Phase 4 — Strategy Lab (train, compare, promote a champion) + Phase 8 learning
# ---------------------------------------------------------------------------
def _dataset_xy(trades: list[bm.Btc5mTrade]) -> tuple[list[list[float]], list[int]]:
    X = [feature_vector(t.features) for t in trades if t.features]
    y = [int(t.label_direction) for t in trades if t.features and t.label_direction is not None]
    # keep X and y aligned (only rows with both a vector and a label)
    pairs = [(feature_vector(t.features), int(t.label_direction)) for t in trades
             if t.features and t.label_direction is not None]
    if not pairs:
        return [], []
    X = [p[0] for p in pairs]
    y = [p[1] for p in pairs]
    return X, y


def _persist_leaderboard(db: Session, scope: str, result: dict) -> dict:
    """Persist a train/compare result as leaderboard rows and (re)assign the
    champion. Promotes only when the new champion's held-out F1 materially beats
    the prior champion (records WHY)."""
    if not result.get("trainable"):
        return {"scope": scope, "trainable": False, "reason": result.get("reason")}
    prior = db.scalar(select(bm.Btc5mModel).where(
        bm.Btc5mModel.scope == scope, bm.Btc5mModel.is_champion.is_(True)))
    prior_f1 = prior.f1 if prior else -1.0
    # clear old rows for this scope (keep the leaderboard to the latest run per scope)
    for old in db.scalars(select(bm.Btc5mModel).where(bm.Btc5mModel.scope == scope)).all():
        db.delete(old)
    champ_name = result["champion"]
    champ_f1 = next(m["f1"] for m in result["models"] if m["name"] == champ_name)
    promoted = champ_f1 > prior_f1 + 0.02
    note = (f"champion {champ_name} F1={champ_f1:.3f} "
            + (f"beat prior {prior.name} F1={prior_f1:.3f} (+{champ_f1 - prior_f1:.3f})"
               if prior and promoted else
               ("first champion" if not prior else f"kept (prior F1={prior_f1:.3f})")))
    for m in result["models"]:
        db.add(bm.Btc5mModel(
            name=m["name"], scope=scope, accuracy=m["accuracy"], precision=m["precision"],
            recall=m["recall"], f1=m["f1"], cv_f1=m["cv_f1"], n_train=m["n_train"],
            n_test=m["n_test"], is_champion=(m["name"] == champ_name),
            promotion_note=note if m["name"] == champ_name else None,
            feature_importance=m["feature_importance"], params=m["params"], metrics=m))
    db.commit()
    return {"scope": scope, "champion": champ_name, "champion_f1": champ_f1,
            "promoted": promoted, "note": note, "n_models": len(result["models"])}


def train_models(db: Session, *, per_wallet: int = 5) -> dict:
    """Train + compare ALL model families globally and for the top profitable
    wallets, persisting the leaderboard and champion(s). (Phase 4 + iterative
    Phase 8.)"""
    all_trades = db.scalars(select(bm.Btc5mTrade)).all()
    X, y = _dataset_xy(all_trades)
    global_res = ml.train_and_compare(X, y, feature_names=FEATURE_NAMES)
    out = {"global": _persist_leaderboard(db, "global", global_res)}

    # per-wallet models for the most profitable wallets with enough data
    profs = db.scalars(select(bm.Btc5mWalletProfile)
                       .where(bm.Btc5mWalletProfile.profitable.is_(True))
                       .order_by(bm.Btc5mWalletProfile.roi.desc())).all()
    wallet_results = []
    for prof in profs[:per_wallet]:
        wt = [t for t in all_trades if t.wallet_address == prof.wallet_address]
        wx, wy = _dataset_xy(wt)
        res = ml.train_and_compare(wx, wy, feature_names=FEATURE_NAMES)
        if res.get("trainable"):
            wallet_results.append(_persist_leaderboard(db, prof.wallet_address, res))
    out["wallets_trained"] = len(wallet_results)
    out["wallet_models"] = wallet_results
    return out


def champion(db: Session, scope: str = "global") -> bm.Btc5mModel | None:
    return db.scalar(select(bm.Btc5mModel).where(
        bm.Btc5mModel.scope == scope, bm.Btc5mModel.is_champion.is_(True)))


def _load_model(row: bm.Btc5mModel):
    """Rebuild a usable predictor from a stored leaderboard row by refitting its
    family on the current dataset (params are summaries; refit keeps it simple and
    always current)."""
    return ml.MODEL_FACTORIES.get(row.name, ml.MajorityBaseline)()


# ---------------------------------------------------------------------------
# Phase 6 — consensus analysis (correlation, agreement, leader/follower)
# ---------------------------------------------------------------------------
def consensus(db: Session, *, profitable_only: bool = True) -> dict:
    """Which wallets agree before profitable moves. Builds a co-occurrence /
    agreement matrix, leader→follower lags, and top consensus groups."""
    profs = db.scalars(select(bm.Btc5mWalletProfile)).all()
    if profitable_only:
        profs = [p for p in profs if p.profitable]
    addrs = [p.wallet_address for p in profs]
    aset = set(addrs)
    trades = [t for t in db.scalars(select(bm.Btc5mTrade)).all()
              if t.wallet_address in aset and t.side == "buy"]
    # group entries by market
    by_market: dict[str, list] = {}
    for t in trades:
        by_market.setdefault(t.market_id, []).append(t)

    pair_same = {}   # (a,b) -> co-entries SAME direction within window
    pair_total = {}  # (a,b) -> co-entries in same market
    pair_won = {}    # (a,b) -> times both entered same dir AND won
    lag = {}         # (a,b) -> list of (b_time - a_time) when both present
    for mid, ts in by_market.items():
        ts.sort(key=lambda x: x.timestamp)
        for i in range(len(ts)):
            for j in range(len(ts)):
                if i == j or ts[i].wallet_address == ts[j].wallet_address:
                    continue
                a, b = ts[i], ts[j]
                key = (a.wallet_address, b.wallet_address)
                dt = (b.timestamp - a.timestamp).total_seconds()
                if 0 <= dt <= CONSENSUS_WINDOW_S:
                    pair_total[key] = pair_total.get(key, 0) + 1
                    if a.direction == b.direction:
                        pair_same[key] = pair_same.get(key, 0) + 1
                        lag.setdefault(key, []).append(dt)
                        if a.won and b.won:
                            pair_won[key] = pair_won.get(key, 0) + 1

    # agreement + leader/follower edges
    edges = []
    for key, total in pair_total.items():
        same = pair_same.get(key, 0)
        if total < 2:
            continue
        agree = round(same / total, 3)
        avg_lag = round(_mean(lag.get(key, [0])), 1)
        joint_win = round(pair_won.get(key, 0) / same, 3) if same else 0.0
        edges.append({"leader": key[0], "follower": key[1], "co_entries": total,
                      "agreement": agree, "avg_lag_s": avg_lag, "joint_win_rate": joint_win})
    edges.sort(key=lambda e: (-e["agreement"], -e["co_entries"]))

    # consensus groups: greedily merge strongly-agreeing pairs (undirected)
    strong = [e for e in edges if e["agreement"] >= 0.6 and e["co_entries"] >= 2]
    groups = []
    used = set()
    for e in strong:
        a, b = e["leader"], e["follower"]
        placed = False
        for g in groups:
            if a in g["wallets"] or b in g["wallets"]:
                g["wallets"].update([a, b])
                g["scores"].append(e["joint_win_rate"])
                placed = True
                break
        if not placed:
            groups.append({"wallets": {a, b}, "scores": [e["joint_win_rate"]]})
    group_out = []
    for g in groups:
        wl = sorted(g["wallets"])
        group_out.append({"wallets": wl, "size": len(wl),
                          "consensus_score": round(_mean(g["scores"]), 3),
                          "profitable_together_pct": round(_mean(g["scores"]) * 100, 1)})
    group_out.sort(key=lambda g: (-g["consensus_score"], -g["size"]))

    # leaders (low avg lag, high out-agreement) vs followers (positive lag)
    out_lag: dict[str, list] = {}
    for e in edges:
        out_lag.setdefault(e["leader"], []).append(e["avg_lag_s"])
    leaders = sorted(({"wallet": w, "avg_lead_s": round(_mean(v), 1), "links": len(v)}
                      for w, v in out_lag.items()), key=lambda d: -d["links"])[:8]
    followers = sorted(edges, key=lambda e: -e["avg_lag_s"])[:8]
    return {
        "wallets": addrs,
        "edges": edges[:60],
        "consensus_groups": group_out[:12],
        "leaders": leaders,
        "followers": [{"wallet": e["follower"], "follows": e["leader"],
                       "lag_s": e["avg_lag_s"], "agreement": e["agreement"]} for e in followers],
        "independent": [a for a in addrs if a not in
                        {e["leader"] for e in edges} | {e["follower"] for e in edges}],
    }


# ---------------------------------------------------------------------------
# Phase 7 — shadow strategy (paper only; NEVER places real orders)
# ---------------------------------------------------------------------------
def _market_state_features(db: Session, market_id: str) -> dict | None:
    """Reconstruct the latest market state for a market from its indexed trades
    (used to predict the next action). Returns a feature dict or None."""
    trs = sorted([t for t in db.scalars(select(bm.Btc5mTrade).where(
        bm.Btc5mTrade.market_id == market_id)).all()], key=lambda t: t.timestamp)
    if not trs:
        return None
    prior = [{"yes_price": t.price if t.direction == "YES" else 1.0 - t.price,
              "usd": t.usd_value, "side": t.side, "dir": t.direction} for t in trs]
    m = db.get(bm.Btc5mMarket, market_id)
    last = trs[-1]
    return reconstruct_features(prior, last, last.direction,
                                last.seconds_from_creation, last.seconds_until_expiry, m)


def generate_shadow_signals(db: Session, *, min_confidence: float = 0.6) -> dict:
    """For each BTC5m market without a shadow signal yet, run the champion model on
    its reconstructed state and emit a paper prediction (action / confidence /
    edge / consensus). Idempotent (one signal per market)."""
    champ = champion(db, "global")
    if champ is None:
        return {"generated": 0, "reason": "no champion model yet"}
    all_trades = db.scalars(select(bm.Btc5mTrade)).all()
    X, y = _dataset_xy(all_trades)
    if len(X) < 6 or len(set(y)) < 2:
        return {"generated": 0, "reason": "insufficient training data"}
    model = _load_model(champ)
    model.fit(X, y)

    cons = consensus(db)
    profitable = {p.wallet_address for p in db.scalars(select(bm.Btc5mWalletProfile)
                  .where(bm.Btc5mWalletProfile.profitable.is_(True))).all()}

    generated = 0
    markets = db.scalars(select(bm.Btc5mMarket)).all()
    for mk in markets:
        if db.scalar(select(bm.Btc5mShadowSignal).where(bm.Btc5mShadowSignal.market_id == mk.market_id)):
            continue
        feats = _market_state_features(db, mk.market_id)
        if not feats:
            continue
        p_yes = model.predict_proba([feature_vector(feats)])[0]
        conf = abs(p_yes - 0.5) * 2
        action = "NO_TRADE"
        edge = 0.0
        if conf >= min_confidence:
            mkt_price = feats.get("market_yes_price", 0.5)
            if p_yes >= 0.5:
                action, edge = "BUY_YES", round(p_yes - mkt_price, 4)
            else:
                action, edge = "BUY_NO", round((1 - p_yes) - (1 - mkt_price), 4)
        # supporting wallets: profitable wallets that traded this market in the action's dir
        mt = [t for t in all_trades if t.market_id == mk.market_id and t.wallet_address in profitable]
        want = "YES" if action == "BUY_YES" else "NO" if action == "BUY_NO" else None
        support = sorted({t.wallet_address for t in mt if want is None or t.direction == want})
        cstrength = round(len(support) / max(1, len(profitable)), 3) if profitable else 0.0

        db.add(bm.Btc5mShadowSignal(
            market_id=mk.market_id, market_question=mk.question, action=action,
            confidence=round(conf, 4), expected_edge=edge, predicted_probability=round(p_yes, 4),
            model_name=champ.name, supporting_wallets=support, consensus_strength=cstrength,
            resolved=False))
        generated += 1
    db.commit()
    return {"generated": generated, "champion": champ.name}


def score_shadow_signals(db: Session) -> dict:
    """Mark shadow signals correct/incorrect once their market has resolved + book
    a paper P/L. (No real money — paper only.)"""
    scored = 0
    for sig in db.scalars(select(bm.Btc5mShadowSignal).where(
            bm.Btc5mShadowSignal.resolved.is_(False))).all():
        mk = db.get(bm.Btc5mMarket, sig.market_id)
        if not (mk and mk.resolved and mk.final_outcome is not None):
            continue
        won_yes = _yes_no(mk.final_outcome) == "YES"
        if sig.action == "NO_TRADE":
            sig.correct = None
            sig.realized_pnl = 0.0
        else:
            picked_yes = sig.action == "BUY_YES"
            sig.correct = (picked_yes == won_yes)
            # $1 paper stake at the market-implied price -> normalized payoff
            price = max(0.01, min(0.99, sig.predicted_probability if picked_yes else 1 - sig.predicted_probability))
            sig.realized_pnl = round((1.0 - price) if sig.correct else -price, 4)
        sig.resolved = True
        db.add(sig)
        scored += 1
    db.commit()
    return {"scored": scored}


def shadow_performance(db: Session) -> dict:
    sigs = db.scalars(select(bm.Btc5mShadowSignal)).all()
    acted = [s for s in sigs if s.action != "NO_TRADE"]
    resolved = [s for s in acted if s.resolved and s.correct is not None]
    wins = sum(1 for s in resolved if s.correct)
    pnl = round(sum(s.realized_pnl for s in resolved), 4)
    return {
        "total_signals": len(sigs),
        "actionable": len(acted),
        "no_trade": len(sigs) - len(acted),
        "resolved": len(resolved),
        "hit_rate": round(wins / len(resolved), 4) if resolved else 0.0,
        "paper_pnl": pnl,
        "avg_confidence": round(_mean([s.confidence for s in acted]), 4) if acted else 0.0,
    }


# ---------------------------------------------------------------------------
# Phase 8 — continuous-learning orchestrator (idempotent; safe to rerun)
# ---------------------------------------------------------------------------
def refresh(db: Session, *, limit_markets: int | None = 50, train: bool = True) -> dict:
    """One research cycle: index -> fingerprint -> train+promote -> score+generate
    shadow signals -> log a research note. Read-only w.r.t. production; writes only
    btc5m_* tables. Idempotent — rerunning re-indexes and recomputes safely."""
    ds = index_dataset(db, limit_markets=limit_markets)
    fp = fingerprint_wallets(db)
    tr = {"global": {"trainable": False}}
    if train:
        tr = train_models(db)
    score = score_shadow_signals(db)
    gen = generate_shadow_signals(db)
    champ = champion(db, "global")

    note_body = (f"indexed {ds['markets_indexed']} markets / {ds['trades_indexed']} new trades; "
                 f"{fp['profiles']} wallets ({fp['profitable']} profitable); "
                 f"champion={champ.name if champ else 'none'} "
                 f"F1={champ.f1 if champ else 0:.3f}; "
                 f"shadow +{gen.get('generated', 0)} signals, scored {score['scored']}.")
    db.add(bm.Btc5mResearchNote(kind="batch", title="research cycle", body=note_body,
                                data={"dataset": ds, "fingerprint": fp, "training": tr,
                                      "shadow_generated": gen, "shadow_scored": score}))
    # if a promotion happened this run, log it explicitly
    if tr.get("global", {}).get("promoted"):
        db.add(bm.Btc5mResearchNote(kind="promotion", title="champion promoted",
                                    body=tr["global"]["note"], data=tr["global"]))
    db.commit()
    return {"dataset": ds, "fingerprint": fp, "training": tr,
            "shadow_generated": gen, "shadow_scored": score,
            "champion": champ.name if champ else None,
            "champion_f1": champ.f1 if champ else 0.0}


# ---------------------------------------------------------------------------
# Read APIs for the dashboard + frontend sections
# ---------------------------------------------------------------------------
def _prof_dict(p: bm.Btc5mWalletProfile) -> dict:
    return {"wallet": p.wallet_address, "trade_count": p.trade_count,
            "settled_count": p.settled_count, "roi": p.roi, "profit_factor": p.profit_factor,
            "win_rate": p.win_rate, "realized_pnl": p.realized_pnl,
            "avg_trade_size": p.avg_trade_size, "profitable": p.profitable,
            "cluster": p.cluster, "cluster_confidence": p.cluster_confidence,
            "wallet_iq": p.wallet_iq, "metrics": p.metrics}


def wallet_profiles(db: Session, *, limit: int = 200) -> list[dict]:
    profs = db.scalars(select(bm.Btc5mWalletProfile)
                       .order_by(bm.Btc5mWalletProfile.roi.desc()).limit(limit)).all()
    return [_prof_dict(p) for p in profs]


def wallet_iq_cards(db: Session, *, limit: int = 50) -> list[dict]:
    profs = db.scalars(select(bm.Btc5mWalletProfile)
                       .where(bm.Btc5mWalletProfile.profitable.is_(True))
                       .order_by(bm.Btc5mWalletProfile.roi.desc()).limit(limit)).all()
    return [{"wallet": p.wallet_address, "roi": p.roi, "profit_factor": p.profit_factor,
             "win_rate": p.win_rate, "cluster": p.cluster, **p.wallet_iq} for p in profs]


def clusters(db: Session) -> dict:
    profs = db.scalars(select(bm.Btc5mWalletProfile)).all()
    groups: dict[str, list] = {}
    for p in profs:
        groups.setdefault(p.cluster, []).append(p)
    out = []
    for name, members in groups.items():
        out.append({
            "cluster": name, "count": len(members),
            "avg_confidence": round(_mean([m.cluster_confidence for m in members]), 2),
            "avg_roi": round(_mean([m.roi for m in members]), 3),
            "profitable": sum(1 for m in members if m.profitable),
            "wallets": [m.wallet_address for m in sorted(members, key=lambda x: -x.roi)[:12]],
        })
    out.sort(key=lambda c: -c["count"])
    return {"clusters": out, "total_wallets": len(profs)}


def leaderboard(db: Session, *, scope: str = "global") -> list[dict]:
    rows = db.scalars(select(bm.Btc5mModel).where(bm.Btc5mModel.scope == scope)
                      .order_by(bm.Btc5mModel.f1.desc())).all()
    return [{"name": r.name, "accuracy": r.accuracy, "precision": r.precision,
             "recall": r.recall, "f1": r.f1, "cv_f1": r.cv_f1, "n_train": r.n_train,
             "n_test": r.n_test, "is_champion": r.is_champion, "note": r.promotion_note,
             "overfit_gap": (r.metrics or {}).get("overfit_gap"),
             "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]


def feature_importance(db: Session, *, scope: str = "global") -> list[dict]:
    champ = champion(db, scope)
    return champ.feature_importance if champ else []


def shadow_signals(db: Session, *, limit: int = 50) -> list[dict]:
    rows = db.scalars(select(bm.Btc5mShadowSignal)
                      .order_by(bm.Btc5mShadowSignal.created_at.desc()).limit(limit)).all()
    return [{"id": s.id, "market_id": s.market_id, "market": s.market_question,
             "action": s.action, "confidence": s.confidence, "expected_edge": s.expected_edge,
             "predicted_probability": s.predicted_probability, "model": s.model_name,
             "supporting_wallets": s.supporting_wallets, "consensus_strength": s.consensus_strength,
             "resolved": s.resolved, "correct": s.correct, "realized_pnl": s.realized_pnl,
             "created_at": s.created_at.isoformat() if s.created_at else None} for s in rows]


def research_notes(db: Session, *, limit: int = 40) -> list[dict]:
    rows = db.scalars(select(bm.Btc5mResearchNote)
                      .order_by(bm.Btc5mResearchNote.created_at.desc()).limit(limit)).all()
    return [{"id": n.id, "kind": n.kind, "title": n.title, "body": n.body,
             "created_at": n.created_at.isoformat() if n.created_at else None} for n in rows]


def dataset_summary(db: Session) -> dict:
    n_markets = db.scalar(select(func.count()).select_from(bm.Btc5mMarket)) or 0
    n_trades = db.scalar(select(func.count()).select_from(bm.Btc5mTrade)) or 0
    n_resolved = db.scalar(select(func.count()).select_from(bm.Btc5mMarket)
                           .where(bm.Btc5mMarket.resolved.is_(True))) or 0
    n_wallets = db.scalar(select(func.count(func.distinct(bm.Btc5mTrade.wallet_address)))) or 0
    recent = db.scalars(select(bm.Btc5mMarket).order_by(
        bm.Btc5mMarket.indexed_at.desc()).limit(20)).all()
    return {
        "markets_indexed": n_markets, "markets_resolved": n_resolved,
        "trades_indexed": n_trades, "wallets": n_wallets,
        "feature_names": FEATURE_NAMES,
        "recent_markets": [{"market_id": m.market_id, "question": m.question,
                            "resolved": m.resolved, "final_outcome": m.final_outcome,
                            "trade_count": m.trade_count, "wallet_count": m.wallet_count,
                            "volume": m.volume} for m in recent],
    }


def dashboard(db: Session) -> dict:
    """The BTC 5M Reversal dashboard aggregate."""
    n_markets = db.scalar(select(func.count()).select_from(bm.Btc5mMarket)) or 0
    n_trades = db.scalar(select(func.count()).select_from(bm.Btc5mTrade)) or 0
    n_wallets = db.scalar(select(func.count()).select_from(bm.Btc5mWalletProfile)) or 0
    n_profitable = db.scalar(select(func.count()).select_from(bm.Btc5mWalletProfile)
                             .where(bm.Btc5mWalletProfile.profitable.is_(True))) or 0
    n_models = db.scalar(select(func.count()).select_from(bm.Btc5mModel)) or 0
    champ = champion(db, "global")
    cl = clusters(db)
    largest = cl["clusters"][0] if cl["clusters"] else None
    cons = consensus(db)
    latest = shadow_signals(db, limit=8)
    top_feats = (champ.feature_importance[:6] if champ else [])
    return {
        "wallet_count": n_wallets,
        "profitable_wallets": n_profitable,
        "trade_count": n_trades,
        "markets_indexed": n_markets,
        "models_trained": n_models,
        "best_model": champ.name if champ else None,
        "best_model_accuracy": champ.accuracy if champ else None,
        "best_model_f1": champ.f1 if champ else None,
        "top_features": top_feats,
        "largest_cluster": largest,
        "consensus_opportunities": cons["consensus_groups"][:5],
        "leader_wallets": cons["leaders"][:5],
        "follower_wallets": cons["followers"][:5],
        "latest_signals": latest,
        "shadow_performance": shadow_performance(db),
        "safety": "read-only research — never submits orders or affects live trading",
    }
