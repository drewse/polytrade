"""
Parameter optimization (Phase 12) + walk-forward analysis (Phase 13).

Built on the replay simulator. Optimization sweeps safe parameter grids over
historical labeled data; walk-forward rolls train/validate/forward windows to
test out-of-sample STABILITY and reject overfit parameter sets. No future
information is used in any decision (only for settling P&L). Pure. PAPER ONLY.
"""
from __future__ import annotations

import statistics

from . import simulate

# Safe parameter grids (Phase 12).
GRIDS = {
    "confidence": [70, 75, 80, 85, 90],
    "kelly": [0.10, 0.25, 0.33, 0.50],
    "liquidity": [500, 1000, 2500, 5000, 10000],
    "edge": [0.01, 0.02, 0.03, 0.05, 0.08],
}


def optimize(samples: list, param: str, values=None) -> dict:
    """Evaluate each parameter value over the full labeled dataset."""
    values = values or GRIDS.get(param, [])
    base = simulate.base_config()
    results = []
    for v in values:
        cfg = simulate.apply_param(base, param, v)
        out = simulate.run(cfg, samples)
        m = out["metrics"]
        results.append({"value": v, "n_taken": out["n_taken"],
                        "sharpe": m["sharpe"], "total_pnl": m["total_pnl"],
                        "win_rate": m["win_rate"], "expectancy": m["expectancy"],
                        "max_drawdown": m["max_drawdown"]})
    best = max(results, key=lambda r: r["sharpe"], default=None) if results else None
    return {"paper_only": True, "param": param, "values": values, "results": results,
            "best": best, "n_samples": len(samples)}


def walk_forward(samples: list, param: str, values=None, windows: int = 4) -> dict:
    """Rolling train/validate/forward. For each window pick the best `param`
    value on the TRAIN slice, then measure it on the FORWARD slice. Reports
    forward-Sharpe stability, variance, parameter stability and drift."""
    values = values or GRIDS.get(param, [])
    ordered = sorted(samples, key=lambda s: s.created_at)
    n = len(ordered)
    if n < windows * 3 or not values:
        return {"paper_only": True, "param": param, "insufficient_data": True, "n_samples": n}

    base = simulate.base_config()
    seg = n // (windows + 2)  # train≈2 segs, then validate+forward roll
    rows = []
    chosen = []
    forward_sharpes = []
    for i in range(windows):
        train = ordered[i * seg: (i + 2) * seg]
        forward = ordered[(i + 2) * seg: (i + 3) * seg]
        if not train or not forward:
            continue
        # pick best value on TRAIN only
        best_v, best_sharpe = None, -1e9
        for v in values:
            m = simulate.run(simulate.apply_param(base, param, v), train)["metrics"]
            if m["sharpe"] > best_sharpe:
                best_sharpe, best_v = m["sharpe"], v
        fwd = simulate.run(simulate.apply_param(base, param, best_v), forward)["metrics"]
        chosen.append(best_v)
        forward_sharpes.append(fwd["sharpe"])
        rows.append({"window": i + 1, "chosen_value": best_v, "train_sharpe": best_sharpe,
                     "forward_sharpe": fwd["sharpe"], "forward_pnl": fwd["total_pnl"],
                     "forward_trades": fwd["closed_positions"]})

    if not rows:
        return {"paper_only": True, "param": param, "insufficient_data": True, "n_samples": n}

    avg_fwd = round(statistics.mean(forward_sharpes), 4)
    var_fwd = round(statistics.pvariance(forward_sharpes), 4) if len(forward_sharpes) > 1 else 0.0
    # parameter stability: fraction of windows that agreed with the modal choice
    mode_v = statistics.mode(chosen) if chosen else None
    param_stability = round(sum(1 for c in chosen if c == mode_v) / len(chosen), 4)
    drift = round((chosen[-1] - chosen[0]) if all(isinstance(c, (int, float)) for c in chosen) else 0.0, 4)
    # overfit flag: good in train but forward Sharpe collapses / is unstable
    overfit = avg_fwd < 0 or var_fwd > 1.0 or param_stability < 0.5

    return {
        "paper_only": True, "param": param, "windows": rows, "n_samples": n,
        "avg_forward_sharpe": avg_fwd, "forward_sharpe_variance": var_fwd,
        "parameter_stability": param_stability, "parameter_drift": drift,
        "modal_value": mode_v, "overfit_rejected": overfit,
        "verdict": ("REJECT (unstable / overfit)" if overfit else f"ACCEPT modal {param}={mode_v}"),
    }
