"""
Leaderboard scoring (Phase 7 + Phase 10: isolated scorer).

Ranks strategies by a weighted, risk-adjusted score rather than raw P&L, and
explains the ranking. Weights (documented):

    30%  Sharpe
    20%  Profit Factor
    15%  Max Drawdown        (inverted: less is better)
    15%  CAGR / total return
    10%  Win Rate
    10%  Consistency

Each metric is min-max normalized ACROSS the cohort, so the score is relative
to the field currently being compared. A strategy with no closed trades scores
0 (insufficient evidence). `explain_pair` says why #i ranks above #j.
"""
from __future__ import annotations

WEIGHTS = {
    "sharpe": 0.30,
    "profit_factor": 0.20,
    "max_drawdown": 0.15,   # inverted below
    "cagr": 0.15,
    "win_rate": 0.10,
    "consistency": 0.10,
}
_LOWER_IS_BETTER = {"max_drawdown"}


def _field(m: dict, key: str) -> float:
    if key == "cagr":
        return float(m.get("annualized_return") or m.get("total_return") or 0.0)
    return float(m.get(key, 0.0) or 0.0)


def _normalize(values: list[float], lower_is_better: bool) -> list[float]:
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.5 for _ in values]
    norm = [(v - lo) / (hi - lo) for v in values]
    return [1.0 - n for n in norm] if lower_is_better else norm


def rank(strategies: list[dict]) -> list[dict]:
    """`strategies` = list of {id, key, name, metrics}. Returns ranked list with
    score, normalized components, reason, strengths, weaknesses."""
    if not strategies:
        return []
    cohort = [s["metrics"] or {} for s in strategies]
    has_trades = [bool((m.get("closed_positions") or 0) > 0) for m in cohort]

    # normalized component per metric across the cohort
    comp_norm: dict[str, list[float]] = {}
    for key in WEIGHTS:
        vals = [_field(m, key) for m in cohort]
        comp_norm[key] = _normalize(vals, key in _LOWER_IS_BETTER)

    ranked = []
    for i, s in enumerate(strategies):
        if not has_trades[i]:
            score = 0.0
            comps = {k: 0.0 for k in WEIGHTS}
        else:
            comps = {k: round(comp_norm[k][i], 4) for k in WEIGHTS}
            score = round(sum(WEIGHTS[k] * comps[k] for k in WEIGHTS) * 100, 2)
        m = cohort[i]
        ranked.append({
            "id": s["id"], "key": s["key"], "name": s["name"],
            "score": score, "components": comps,
            "metrics": m,
            "strengths": _describe(comps, m, want="strength"),
            "weaknesses": _describe(comps, m, want="weakness"),
            "has_trades": has_trades[i],
        })

    ranked.sort(key=lambda r: r["score"], reverse=True)
    for pos, r in enumerate(ranked):
        r["rank"] = pos + 1
        r["reason"] = _reason(r)
    return ranked


_PRETTY = {
    "sharpe": "Sharpe", "profit_factor": "profit factor", "max_drawdown": "low drawdown",
    "cagr": "return", "win_rate": "win rate", "consistency": "consistency",
}


def _describe(comps: dict, m: dict, want: str) -> list[str]:
    items = sorted(comps.items(), key=lambda kv: kv[1], reverse=(want == "strength"))
    out = []
    for key, norm in items[:2]:
        if want == "strength" and norm < 0.55:
            continue
        if want == "weakness" and norm > 0.45:
            continue
        raw = _field(m, key)
        if key == "max_drawdown":
            out.append(f"{_PRETTY[key]} ({raw*100:.1f}% DD)")
        elif key in ("cagr", "win_rate", "consistency"):
            out.append(f"{_PRETTY[key]} ({raw*100:.1f}%)")
        else:
            out.append(f"{_PRETTY[key]} ({raw:.2f})")
    return out


def _reason(r: dict) -> str:
    if not r["has_trades"]:
        return "No closed trades yet — insufficient evidence to rank."
    top = max(r["components"].items(), key=lambda kv: kv[1])
    return (f"Ranked #{r['rank']} with score {r['score']:.0f}/100; "
            f"strongest on {_PRETTY[top[0]]}.")


def explain_pair(higher: dict, lower: dict) -> str:
    """Explain why `higher` outranks `lower` by the biggest weighted gaps."""
    gaps = []
    for k in WEIGHTS:
        diff = (higher["components"].get(k, 0) - lower["components"].get(k, 0)) * WEIGHTS[k]
        gaps.append((k, diff))
    gaps.sort(key=lambda kv: kv[1], reverse=True)
    drivers = [f"{_PRETTY[k]} (+{d*100:.0f} pts weighted)" for k, d in gaps[:3] if d > 0]
    lead = f"{higher['name']} ranks above {lower['name']} "
    if not drivers:
        return lead + "by a narrow overall margin."
    return lead + "mainly on " + ", ".join(drivers) + "."
