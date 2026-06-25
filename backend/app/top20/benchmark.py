"""
Probability-model benchmark harness (Phase 29).

Establishes a baseline BEFORE any ML. Scores several probability estimators
against the realized outcomes in the labeled feature-vector dataset:

  * current     — the statistical estimator's stored probability
  * market      — the market-implied probability (entry price)
  * wallet_only — the wallet's historical win rate
  * edge_only   — price + observed edge (clipped)
  * historical  — the constant base rate (mean outcome)
  * random      — 0.5 for everything

Metrics: Brier score, log loss, calibration error, ROC AUC, reliability bins.
Pure functions. PAPER ONLY.

sample = {"y": 0/1, "current": p, "market": p, "wallet": p, "edge": p}
"""
from __future__ import annotations

import math

EPS = 1e-9


def _clip(p, lo=0.01, hi=0.99):
    return max(lo, min(hi, p))


def brier(ys, ps):
    return round(sum((p - y) ** 2 for y, p in zip(ys, ps)) / len(ys), 4) if ys else 0.0


def log_loss(ys, ps):
    if not ys:
        return 0.0
    s = 0.0
    for y, p in zip(ys, ps):
        p = _clip(p, EPS, 1 - EPS)
        s += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return round(s / len(ys), 4)


def roc_auc(ys, ps):
    """Rank-based AUC (Mann-Whitney). 0.5 = no skill."""
    pos = [p for y, p in zip(ys, ps) if y == 1]
    neg = [p for y, p in zip(ys, ps) if y == 0]
    if not pos or not neg:
        return None
    wins = ties = 0
    for pp in pos:
        for pn in neg:
            if pp > pn:
                wins += 1
            elif pp == pn:
                ties += 1
    return round((wins + 0.5 * ties) / (len(pos) * len(neg)), 4)


def calibration(ys, ps, bins=10):
    """Reliability diagram bins + expected calibration error (ECE)."""
    buckets = [[] for _ in range(bins)]
    for y, p in zip(ys, ps):
        idx = min(bins - 1, int(_clip(p, 0, 0.999) * bins))
        buckets[idx].append((y, p))
    diagram, ece, n = [], 0.0, len(ys)
    for i, b in enumerate(buckets):
        if not b:
            diagram.append({"bin": round((i + 0.5) / bins, 3), "n": 0, "pred": None, "actual": None})
            continue
        mp = sum(p for _, p in b) / len(b)
        ma = sum(y for y, _ in b) / len(b)
        diagram.append({"bin": round((i + 0.5) / bins, 3), "n": len(b),
                        "pred": round(mp, 3), "actual": round(ma, 3)})
        ece += (len(b) / n) * abs(mp - ma)
    return {"ece": round(ece, 4), "diagram": diagram}


def _score(ys, ps):
    return {"brier": brier(ys, ps), "log_loss": log_loss(ys, ps),
            "auc": roc_auc(ys, ps), "calibration_error": calibration(ys, ps)["ece"]}


def compute(samples: list[dict]) -> dict:
    if not samples:
        return {"insufficient_data": True, "n": 0, "estimators": {}}
    ys = [s["y"] for s in samples]
    base_rate = sum(ys) / len(ys)
    estimators = {
        "current": [_clip(s["current"]) for s in samples],
        "market": [_clip(s["market"]) for s in samples],
        "wallet_only": [_clip(s["wallet"]) for s in samples],
        "edge_only": [_clip(s["edge"]) for s in samples],
        "historical": [base_rate for _ in samples],
        "random": [0.5 for _ in samples],
    }
    out = {name: _score(ys, ps) for name, ps in estimators.items()}
    ranked = sorted(out.items(), key=lambda kv: kv[1]["brier"])  # lower Brier = better
    return {
        "paper_only": True, "insufficient_data": len(samples) < 30, "n": len(samples),
        "base_rate": round(base_rate, 4),
        "estimators": out,
        "best_by_brier": ranked[0][0],
        "current_vs_market": {
            "current_brier": out["current"]["brier"], "market_brier": out["market"]["brier"],
            "current_beats_market": out["current"]["brier"] < out["market"]["brier"],
        },
        "reliability": calibration(ys, estimators["current"]),
    }
