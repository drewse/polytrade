"""
Wallet reputation with time decay (Phase 15).

Wallets evolve, so recent results matter more than months-old ones. We weight
each settled position by an exponential decay (30-day half-life): w = 0.5 **
(age_days / 30). All metrics are decay-weighted; we also report raw recent
(30d) vs lifetime so drift is visible. Pure function — no DB. PAPER ONLY.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

HALF_LIFE_DAYS = 30.0


def _decay(age_days: float) -> float:
    return 0.5 ** (max(0.0, age_days) / HALF_LIFE_DAYS)


def _wmean(pairs):  # list of (weight, value)
    sw = sum(w for w, _ in pairs)
    return sum(w * v for w, v in pairs) / sw if sw else 0.0


def compute(positions: list, now: datetime | None = None) -> dict:
    """`positions` = settled positions with .realized_pnl, .size, .timestamp,
    optional .market.category. Returns decay-weighted reputation metrics."""
    now = now or datetime.utcnow()
    if not positions:
        return {"num_settled": 0, "reputation_score": 0.0, "insufficient_data": True}

    weighted = []
    for p in positions:
        age = (now - p.timestamp).total_seconds() / 86400.0
        w = _decay(age)
        ret = (p.realized_pnl / p.size) if p.size else 0.0
        weighted.append((w, p, ret, p.realized_pnl > 0))

    dec_roi_num = sum(w * p.realized_pnl for w, p, _, _ in weighted)
    dec_roi_den = sum(w * p.size for w, p, _, _ in weighted)
    decayed_roi = round(dec_roi_num / dec_roi_den, 4) if dec_roi_den else 0.0
    decayed_win = round(_wmean([(w, 1.0 if won else 0.0) for w, _, _, won in weighted]), 4)
    rets = [(w, r) for w, _, r, _ in weighted]
    mean_r = _wmean(rets)
    var = _wmean([(w, (r - mean_r) ** 2) for w, _, r, _ in weighted])
    decayed_sharpe = round(mean_r / math.sqrt(var), 4) if var > 0 else 0.0

    def window_roi(days):
        cutoff = now - timedelta(days=days)
        ps = [p for _, p, _, _ in weighted if p.timestamp >= cutoff]
        num = sum(p.realized_pnl for p in ps)
        den = sum(p.size for p in ps)
        return round(num / den, 4) if den else 0.0, len(ps)

    recent_roi, recent_n = window_roi(7)
    roi_30d, _ = window_roi(30)
    lifetime_roi = round(sum(p.realized_pnl for _, p, _, _ in weighted) /
                         sum(p.size for _, p, _, _ in weighted), 4) if positions else 0.0
    recent_win = (sum(1 for _, p, _, won in weighted if won and (now - p.timestamp).days <= 30) /
                  max(1, sum(1 for _, p, _, _ in weighted if (now - p.timestamp).days <= 30)))
    lifetime_win = sum(1 for _, _, _, won in weighted if won) / len(weighted)
    # calibration/stability proxy: how close recent hit-rate is to lifetime.
    calibration = round(1 - abs(recent_win - lifetime_win), 3)

    # decayed category specialization
    cat: dict[str, list] = {}
    for w, p, _, _ in weighted:
        c = getattr(getattr(p, "market", None), "category", None) or "Other"
        cat.setdefault(c, []).append((w, p.realized_pnl, p.size))
    cat_roi = {c: round(sum(w * pnl for w, pnl, _ in v) / max(1e-9, sum(w * sz for w, _, sz in v)), 4)
               for c, v in cat.items()}
    best_cat = max(cat_roi.items(), key=lambda kv: kv[1]) if cat_roi else ("—", 0.0)

    # equity / drawdown from decay-ordered cumulative pnl
    ordered = sorted((p for _, p, _, _ in weighted), key=lambda p: p.timestamp)
    cum = 0.0
    peak = 0.0
    mdd = 0.0
    for p in ordered:
        cum += p.realized_pnl
        peak = max(peak, cum)
        if peak > 0:
            mdd = max(mdd, (peak - cum) / peak)

    rep = round(100 * (0.4 * min(1, max(0, 0.5 + decayed_roi)) +
                       0.3 * min(1, max(0, (decayed_sharpe + 0.5) / 2.5)) +
                       0.3 * decayed_win), 1)
    return {
        "num_settled": len(positions), "insufficient_data": len(positions) < 8,
        "reputation_score": rep,
        "decayed_roi": decayed_roi, "decayed_win_rate": decayed_win,
        "decayed_sharpe": decayed_sharpe, "calibration": calibration,
        "recent_roi_7d": recent_roi, "recent_roi_30d": roi_30d, "lifetime_roi": lifetime_roi,
        "recent_win_rate": round(recent_win, 4), "lifetime_win_rate": round(lifetime_win, 4),
        "roi_drift": round(roi_30d - lifetime_roi, 4),
        "best_category": {"category": best_cat[0], "roi": best_cat[1]},
        "category_roi": cat_roi, "max_drawdown_usd_frac": round(mdd, 4),
        "half_life_days": HALF_LIFE_DAYS,
    }
