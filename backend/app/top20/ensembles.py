"""
Ensemble strategies (Phase 17).

Combine the individual strategies into portfolios using different weighting
schemes, then report combined risk-adjusted performance. Pure function over
pre-computed per-strategy series + metrics. PAPER ONLY.

strategy = {key, name, returns: [per-trade returns], metrics: {sharpe, total_pnl,
            max_drawdown, forward_sharpe?}}
"""
from __future__ import annotations

from . import analytics

METHODS = ["equal_weight", "top5_sharpe", "sharpe_weighted", "risk_parity", "forward_weighted"]


def _weights(strategies: list[dict], method: str) -> dict[str, float]:
    keys = [s["key"] for s in strategies]
    if not keys:
        return {}
    if method == "equal_weight":
        w = {k: 1.0 for k in keys}
    elif method == "top5_sharpe":
        top = sorted(strategies, key=lambda s: s["metrics"].get("sharpe", 0), reverse=True)[:5]
        tk = {s["key"] for s in top}
        w = {k: (1.0 if k in tk else 0.0) for k in keys}
    elif method == "sharpe_weighted":
        w = {s["key"]: max(0.0, s["metrics"].get("sharpe", 0)) for s in strategies}
    elif method == "risk_parity":
        # inverse-drawdown weighting (lower risk => higher weight)
        w = {s["key"]: 1.0 / (s["metrics"].get("max_drawdown", 0) + 0.05) for s in strategies}
    elif method == "forward_weighted":
        w = {s["key"]: max(0.0, s["metrics"].get("forward_sharpe", s["metrics"].get("sharpe", 0)))
             for s in strategies}
    else:
        w = {k: 1.0 for k in keys}
    total = sum(w.values())
    return {k: (v / total if total else 0.0) for k, v in w.items()}


def _blend_returns(strategies: list[dict], weights: dict[str, float]) -> list[float]:
    """Weighted average of per-trade returns across strategies (aligned by index;
    a simple combined return stream for risk-adjusted stats)."""
    series = [(s["key"], s.get("returns") or []) for s in strategies if weights.get(s["key"], 0) > 0]
    if not series:
        return []
    max_len = max(len(r) for _, r in series)
    out = []
    for i in range(max_len):
        num = den = 0.0
        for key, rets in series:
            if i < len(rets):
                num += weights[key] * rets[i]
                den += weights[key]
        if den:
            out.append(num / den)
    return out


def compute(strategies: list[dict], starting_bankroll: float = 10_000.0) -> dict:
    out = []
    for method in METHODS:
        w = _weights(strategies, method)
        rets = _blend_returns(strategies, w)
        pnl = round(sum(s["metrics"].get("total_pnl", 0) * w.get(s["key"], 0) for s in strategies), 2)
        out.append({
            "method": method,
            "weights": {k: round(v, 4) for k, v in w.items() if v > 0.001},
            "n_strategies": sum(1 for v in w.values() if v > 0.001),
            "sharpe": analytics.sharpe(rets),
            "sortino": analytics.sortino(rets),
            "weighted_pnl": pnl,
            "n_returns": len(rets),
        })
    out.sort(key=lambda e: e["sharpe"], reverse=True)
    return {"paper_only": True, "ensembles": out, "methods": METHODS}
