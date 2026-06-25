"""
Market intelligence (Phase 16).

Aggregates settled paper outcomes by market category to answer "what kinds of
markets are easiest to beat?" — edge by category, market efficiency, mispricing
frequency, edge persistence, and time-to-resolution profitability. Pure function
over pre-fetched records. PAPER ONLY.

record = {category, edge, won (0/1), price, realized_return, ttr_hours}
"""
from __future__ import annotations


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _cat_stats(recs: list[dict]) -> dict:
    won = [r["won"] for r in recs]
    prices = [r["price"] for r in recs]
    rrs = [r["realized_return"] for r in recs]
    eff = 1 - _mean([abs(p - w) for p, w in zip(prices, won)])
    mispriced = _mean([1.0 if abs(p - w) > 0.4 else 0.0 for p, w in zip(prices, won)])
    persistence = _mean([1.0 if (r["edge"] > 0) == (r["realized_return"] > 0) else 0.0 for r in recs])
    return {
        "count": len(recs),
        "avg_edge": round(_mean([r["edge"] for r in recs]), 4),
        "win_rate": round(_mean(won), 4),
        "avg_realized_return": round(_mean(rrs), 4),
        "market_efficiency": round(eff, 4),
        "mispriced_frequency": round(mispriced, 4),
        "edge_persistence": round(persistence, 4),
    }


def compute(records: list[dict]) -> dict:
    if not records:
        return {"insufficient_data": True, "categories": [], "n": 0}
    by_cat: dict[str, list] = {}
    for r in records:
        by_cat.setdefault(r.get("category") or "Other", []).append(r)

    cats = [{"category": c, **_cat_stats(rs)} for c, rs in by_cat.items()]
    cats.sort(key=lambda c: c["avg_realized_return"], reverse=True)

    # time-to-resolution profitability: short vs long (median split)
    ttrs = sorted(r["ttr_hours"] for r in records if r.get("ttr_hours") is not None)
    ttr_profile = {}
    if len(ttrs) >= 4:
        med = ttrs[len(ttrs) // 2]
        short = [r["realized_return"] for r in records if (r.get("ttr_hours") or 0) <= med]
        long = [r["realized_return"] for r in records if (r.get("ttr_hours") or 0) > med]
        ttr_profile = {"median_hours": round(med, 1),
                       "short_avg_return": round(_mean(short), 4),
                       "long_avg_return": round(_mean(long), 4)}
    return {
        "paper_only": True, "insufficient_data": len(records) < 10, "n": len(records),
        "categories": cats,
        "best_categories": cats[:3],
        "worst_categories": cats[-3:][::-1],
        "overall_efficiency": round(_mean([1 - abs(r["price"] - r["won"]) for r in records]), 4),
        "mispriced_frequency": round(_mean([1.0 if abs(r["price"] - r["won"]) > 0.4 else 0.0 for r in records]), 4),
        "ttr_profitability": ttr_profile,
    }
