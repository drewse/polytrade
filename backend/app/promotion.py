"""Promotion Candidates — 100% READ-ONLY analytics "farm system".

Identifies wallets that are NOT currently eligible for production but look
promising from real signal history, so future promotions can be evidence-based
rather than achieved by lowering production thresholds.

This module changes NO trading logic. It only READS:
  * live_ranking.eligible_addresses / rank_wallets / _cfg  (production selection,
    used purely to EXCLUDE production wallets and to read the exact reject reason)
  * LiveSignalDecision (historical signal decisions)
  * WalletStat / Wallet / Trade / reconstructed positions

The live executor, wallet eligibility, ranking, order sizing, edge/slippage,
pause/resume/halt and risk limits are untouched and behave identically.

Future-proofing: each candidate is a self-contained dict keyed by wallet address,
so manual promote/demote, candidate-vs-production comparison, and promotion
history can be layered on later without touching live execution.
"""
from __future__ import annotations

import statistics
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import live_ranking, positions as positions_mod
from .models import LiveSignalDecision, Market, Trade, Wallet, WalletStat

# Promotion-score weights (sum to 1.0). DISTINCT from production_rank_score — this
# score is analytics-only and never feeds live wallet selection.
WEIGHTS = {
    "signal_consistency": 0.15,
    "average_edge": 0.20,
    "confidence": 0.10,
    "profit_factor": 0.20,
    "roi": 0.15,
    "settled_trades": 0.10,
    "recent_activity": 0.05,
    "market_diversity": 0.05,
}

# Decisions with this category are wallets rejected because they are not in the
# production-eligible set (below thresholds OR ranked outside the top-N).
_REJECT_CATEGORY = "wallet_not_eligible"


def _clip(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def _promotion_score(*, signals, avg_edge, avg_conf, pf, roi, settled, recent_7d,
                     distinct_markets, concentration, last_active_days) -> float:
    """Deterministic 0..100 promotion score: weighted positives, then
    multiplicative penalties for thin / risky / stale histories."""
    consistency = _clip((signals or 0) / 30.0)          # ~30 signals -> full
    edge_n = _clip((avg_edge or 0) / 0.30)              # avg edge 0.30 -> full
    conf_n = _clip((avg_conf or 0) / 100.0)
    pf_n = _clip(((pf or 0) - 1.0) / 2.0)               # PF 1->0, 3->1
    roi_n = _clip((roi or 0) / 0.50)                    # ROI +50% -> full
    settled_n = _clip((settled or 0) / 50.0)
    recent_n = _clip((recent_7d or 0) / 5.0)
    diversity_n = _clip((distinct_markets or 0) / 10.0)

    base = 100.0 * (
        WEIGHTS["signal_consistency"] * consistency
        + WEIGHTS["average_edge"] * edge_n
        + WEIGHTS["confidence"] * conf_n
        + WEIGHTS["profit_factor"] * pf_n
        + WEIGHTS["roi"] * roi_n
        + WEIGHTS["settled_trades"] * settled_n
        + WEIGHTS["recent_activity"] * recent_n
        + WEIGHTS["market_diversity"] * diversity_n
    )
    penalty = 1.0
    if (signals or 0) < 5 or (settled or 0) < 5:
        penalty *= 0.5                                  # very small history
    if (concentration or 0) > 0.80 or (distinct_markets or 0) <= 1:
        penalty *= 0.7                                  # single-market dependence
    if last_active_days is not None and last_active_days > 30:
        penalty *= 0.6                                  # stale
    if (roi or 0) < 0:
        penalty *= 0.3                                  # negative ROI
    if (pf or 0) < 1.0:
        penalty *= 0.4                                  # poor profit factor
    return round(_clip(base * penalty, 0.0, 100.0), 1)


def _status(score, *, pf, roi, settled) -> str:
    """strong (⭐ production-ready) | near (🟡 close) | watch (🔵 insufficient)."""
    if score >= 70 and (pf or 0) >= 1.10 and (roi or 0) > 0 and (settled or 0) >= 15:
        return "strong"
    if score >= 50:
        return "near"
    return "watch"


def _trend(decs) -> str:
    """improving | stable | declining — from average edge over time."""
    edges = [float(d.edge or 0) for d in sorted(decs, key=lambda x: x.created_at or datetime.min)]
    if len(edges) < 6:
        return "stable"
    half = len(edges) // 2
    older = statistics.mean(edges[:half]) or 0.0
    recent = statistics.mean(edges[half:]) or 0.0
    if older and recent > older * 1.10:
        return "improving"
    if older and recent < older * 0.90:
        return "declining"
    return "stable"


def _longest_profitable_streak(trades, markets) -> int:
    """Longest run of consecutive profitable SETTLED positions (chronological)."""
    if not trades or not markets:
        return 0
    sp = positions_mod.settled_positions(trades, markets)
    best = cur = 0
    for p in sorted(sp, key=lambda x: x.timestamp or datetime.min):
        if p.realized_pnl > 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def promotion_candidates(db: Session, *, min_signals: int = 2, limit: int = 200) -> dict:
    """Compute promotion candidates from historical decision data. READ-ONLY."""
    now = datetime.utcnow()
    cfg = live_ranking._cfg()

    # production set to EXCLUDE + full ranked snapshot (read-only, unchanged logic)
    eligible = live_ranking.eligible_addresses(db)
    ranked = live_ranking.rank_wallets(db, include_failed=True)
    by_addr = {r["address"]: r for r in ranked}
    passing = [r for r in ranked if r["eligible"]]                  # filter-passers, sorted by score
    outside_topn = {r["address"] for r in passing[cfg["top_n"]:]}   # pass filters but ranked out

    # group eligibility-rejected decisions per wallet (EXCLUDING production wallets)
    decs = db.scalars(select(LiveSignalDecision).where(
        LiveSignalDecision.category == _REJECT_CATEGORY)).all()
    by_wallet: dict[str, list] = {}
    for d in decs:
        a = d.wallet_address
        if not a or a in eligible:        # production wallets never appear
            continue
        by_wallet.setdefault(a, []).append(d)
    cands = {a: ds for a, ds in by_wallet.items() if len(ds) >= min_signals}

    # batch-load wallet rows + stats + trades + markets for the candidate set
    wallets = {w.address: w for w in db.scalars(
        select(Wallet).where(Wallet.address.in_(set(cands)))).all()}
    wid_to_addr = {w.id: a for a, w in wallets.items()}
    stats = {s.wallet_id: s for s in db.scalars(
        select(WalletStat).where(WalletStat.wallet_id.in_(wid_to_addr)))} if wid_to_addr else {}
    trades_by_wid: dict[int, list] = {}
    if wid_to_addr:
        for t in db.scalars(select(Trade).where(Trade.wallet_id.in_(wid_to_addr))).all():
            trades_by_wid.setdefault(t.wallet_id, []).append(t)
    mids = {t.market_id for ts in trades_by_wid.values() for t in ts}
    markets = {m.id: m for m in db.scalars(select(Market).where(Market.id.in_(mids)))} if mids else {}

    out = []
    for addr, ds in cands.items():
        w = wallets.get(addr)
        row = by_addr.get(addr) or {}
        st = stats.get(w.id) if w else None
        wtrades = trades_by_wid.get(w.id, []) if w else []

        ts = sorted([d.created_at for d in ds if d.created_at])
        first_seen, last_seen = (ts[0], ts[-1]) if ts else (None, None)
        span_days = max(1.0, (last_seen - first_seen).total_seconds() / 86400.0) if (first_seen and last_seen) else 1.0
        recent_7d = sum(1 for t in ts if (now - t).days < 7)
        recent_30d = sum(1 for t in ts if (now - t).days < 30)

        edges = [float(d.edge or 0) for d in ds]
        confs = [float(d.confidence or 0) for d in ds if d.confidence is not None]
        pscores = [float(d.production_score or 0) for d in ds if d.production_score is not None]
        avg_edge = round(statistics.mean(edges), 4) if edges else 0.0

        roi = row.get("roi")
        pf = row.get("profit_factor")
        win = row.get("win_rate")
        settled = row.get("num_settled")
        last_active = w.last_active if w else None
        last_active_days = (now - last_active).days if last_active else None

        # activity / market diversity (from real trades)
        mkt_exposure: dict[str, float] = {}
        for t in wtrades:
            mkt_exposure[t.market_id] = mkt_exposure.get(t.market_id, 0.0) + float(t.size or 0)
        distinct_markets = len(mkt_exposure)
        total_exp = sum(mkt_exposure.values())
        biggest = max(mkt_exposure.values(), default=0.0)
        concentration = round(biggest / total_exp, 3) if total_exp else 0.0

        # EXACT reject reason (never guessed)
        if addr in outside_topn:
            reason = "Outside production top-N"
        elif row and not row.get("eligible", False):
            reason = row.get("filter_reason") or "below production thresholds"
        else:
            reason = "no settled stats"

        score = _promotion_score(
            signals=len(ds), avg_edge=avg_edge, avg_conf=(statistics.mean(confs) if confs else 0),
            pf=pf, roi=roi, settled=settled, recent_7d=recent_7d,
            distinct_markets=distinct_markets, concentration=concentration,
            last_active_days=last_active_days)
        status = _status(score, pf=pf, roi=roi, settled=settled)

        out.append({
            "wallet": addr,
            "promotion_score": score,
            "status": status,
            # basic
            "first_seen": first_seen.isoformat() if first_seen else None,
            "last_seen": last_seen.isoformat() if last_seen else None,
            "signals_seen": len(ds),
            "recent_signals_7d": recent_7d,
            "recent_signals_30d": recent_30d,
            # signal quality
            "average_edge": avg_edge,
            "median_edge": round(statistics.median(edges), 4) if edges else 0.0,
            "maximum_edge": round(max(edges), 4) if edges else 0.0,
            "average_confidence": round(statistics.mean(confs), 1) if confs else 0.0,
            "average_production_score": round(statistics.mean(pscores), 2) if pscores else 0.0,
            # wallet statistics (if available)
            "roi": roi,
            "profit_factor": pf,
            "win_rate": win,
            "settled_trades": settled,
            "avg_trade_size": round(st.avg_trade_size, 2) if st and st.avg_trade_size is not None else None,
            "max_drawdown": round(st.max_drawdown, 4) if st and st.max_drawdown is not None else None,
            "last_active": last_active.isoformat() if last_active else None,
            "reputation_score": None,   # not precomputed for non-eligible wallets
            # activity
            "distinct_markets": distinct_markets,
            "biggest_market_exposure": round(biggest, 2),
            "market_concentration": concentration,
            # performance
            "avg_signal_frequency_per_day": round(len(ds) / span_days, 3),
            "longest_profitable_streak": _longest_profitable_streak(wtrades, markets),
            "recent_trend": _trend(ds),
            # eligibility
            "reason_rejected": reason,
        })

    out.sort(key=lambda c: (c["promotion_score"], c["signals_seen"]), reverse=True)
    out = out[:limit]
    return {
        "candidates": out,
        "summary": {
            "total_candidates": len(out),
            "strong": sum(1 for c in out if c["status"] == "strong"),
            "near": sum(1 for c in out if c["status"] == "near"),
            "watch": sum(1 for c in out if c["status"] == "watch"),
            "production_wallets_excluded": len(eligible),
        },
        "thresholds": {"min_profit_factor": cfg["min_pf"], "min_settled": cfg["min_settled"],
                       "min_roi": cfg["min_roi"], "top_n": cfg["top_n"]},
        "weights": WEIGHTS,
        "read_only": True,
    }
