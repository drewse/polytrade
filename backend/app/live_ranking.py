"""
PRODUCTION wallet ranking for LIVE trading (live-only).

The quant audit concluded: copyability over-weights win rate (money-losers can
rank high), reputation score is a better long-term-quality measure, and the edge
comes from selecting consistently PROFITABLE wallets — not from the probability
model. So live order selection uses a separate, profitability-first score and
hard profitability filters, instead of raw copyability.

This module ONLY READS existing research metrics (WalletStat, reconstructed
positions, reputation). It changes NO paper-research / replay / analytics code.
Both metrics are exposed: `copyability` (legacy) and `production_rank_score` (new).

    production_rank_score = 40% Reputation + 30% Profit Factor + 20% ROI + 10% Recency

Hard filters (a wallet failing ANY is ineligible regardless of score):
    ROI > 0 · Profit Factor > 1.20 · >= LIVE_MIN_SETTLED settled trades ·
    active within LIVE_ACTIVE_DAYS · (optional) not partial-history.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import positions as positions_mod
from .models import Market, Trade, Wallet, WalletCandidate, WalletStat
from .top20 import reputation

WEIGHTS = {"reputation": 0.40, "profit_factor": 0.30, "roi": 0.20, "recency": 0.10}


def _cfg() -> dict:
    # PRODUCTION thresholds (reverted from the loosened validation values now that
    # live execution is confirmed working). Env vars still override when present.
    return {
        "min_settled": int(os.getenv("LIVE_MIN_SETTLED", "20")),
        "min_pf": float(os.getenv("LIVE_MIN_PF", "1.20")),
        "min_roi": float(os.getenv("LIVE_MIN_ROI", "0.0")),
        "active_days": int(os.getenv("LIVE_ACTIVE_DAYS", "60")),
        "require_full_history": str(os.getenv("LIVE_REQUIRE_FULL_HISTORY", "false")).lower()
                                in ("1", "true", "yes", "on"),
        "top_n": int(os.getenv("LIVE_TOP_N_WALLETS", "20")),
    }


def _clip(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def production_score(*, reputation_score: float, profit_factor: float, roi: float,
                     recency: float) -> float:
    """Profitability-first 0..100 score (documented weights)."""
    rep_n = _clip((reputation_score or 0) / 100.0)
    pf_n = _clip(((profit_factor or 0) - 1.0) / 2.0)   # PF 1.0->0, 3.0->1
    roi_n = _clip((roi or 0) / 0.5)                    # ROI 0->0, +50%->1
    rec_n = _clip(recency or 0)
    return round(100.0 * (WEIGHTS["reputation"] * rep_n + WEIGHTS["profit_factor"] * pf_n
                          + WEIGHTS["roi"] * roi_n + WEIGHTS["recency"] * rec_n), 2)


def passes_filters(stat: WalletStat | None, wallet: Wallet | None, now: datetime,
                   cfg: dict) -> tuple[bool, str]:
    if stat is None:
        return False, "no stats"
    if (stat.realized_roi or 0) <= cfg["min_roi"]:
        return False, f"ROI {((stat.realized_roi or 0)*100):.1f}% <= {cfg['min_roi']*100:.0f}%"
    if (stat.profit_factor or 0) <= cfg["min_pf"]:
        return False, f"PF {stat.profit_factor:.2f} <= {cfg['min_pf']:.2f}"
    if (stat.num_settled or 0) < cfg["min_settled"]:
        return False, f"settled {stat.num_settled} < {cfg['min_settled']}"
    if cfg["require_full_history"] and stat.partial_history:
        return False, "partial history"
    last = wallet.last_active if wallet else None
    if last and (now - last).days > cfg["active_days"]:
        return False, f"inactive {(now - last).days}d > {cfg['active_days']}d"
    return True, "ok"


def _reputation_score(db: Session, wallet: Wallet) -> float:
    trades = db.scalars(select(Trade).where(Trade.wallet_id == wallet.id)).all()
    mids = {t.market_id for t in trades}
    markets = {m.id: m for m in db.scalars(select(Market).where(Market.id.in_(mids))).all()}
    settled = positions_mod.settled_positions(trades, markets)
    return float(reputation.compute(settled).get("reputation_score", 0.0) or 0.0)


def rank_wallets(db: Session, include_failed: bool = False) -> list[dict]:
    """Evaluate every (real) wallet. Reputation (the expensive reconstruction) is
    computed ONLY for wallets that clear the cheap stat filters. Returns rows with
    BOTH copyability (legacy) and production_rank_score (new), sorted by the new
    score (eligible first)."""
    cfg = _cfg()
    now = datetime.utcnow()
    stats = {s.wallet_id: s for s in db.scalars(select(WalletStat)).all()}
    cands = {c.wallet_id: c for c in db.scalars(select(WalletCandidate)).all()}
    wallets = {w.id: w for w in db.scalars(select(Wallet)).all()}
    rows = []
    for wid, stat in stats.items():
        w = wallets.get(wid)
        if not w or w.address.startswith("0x000000"):   # skip mock wallets
            continue
        cand = cands.get(wid)
        copyability = float(cand.copyability_score) if cand else 0.0
        passed, reason = passes_filters(stat, w, now, cfg)
        row = {
            "address": w.address, "copyability": round(copyability, 1),
            "production_rank_score": 0.0, "eligible": passed, "filter_reason": reason,
            "roi": round(stat.realized_roi or 0, 4), "profit_factor": round(stat.profit_factor or 0, 4),
            "win_rate": round(stat.win_rate or 0, 4), "num_settled": int(stat.num_settled or 0),
            "recency": round(stat.recency_score or 0, 4), "reputation_score": None,
            "partial_history": bool(stat.partial_history),
            "classification": cand.classification if cand else None,
        }
        if passed:
            rep = _reputation_score(db, w)
            row["reputation_score"] = round(rep, 1)
            row["production_rank_score"] = production_score(
                reputation_score=rep, profit_factor=stat.profit_factor or 0,
                roi=stat.realized_roi or 0, recency=stat.recency_score or 0)
        if passed or include_failed:
            rows.append(row)
    rows.sort(key=lambda r: (r["eligible"], r["production_rank_score"]), reverse=True)
    return rows


def eligible_addresses(db: Session) -> set[str]:
    """Top-N production-ranked, filter-passing wallet addresses the live executor
    is allowed to copy."""
    cfg = _cfg()
    ranked = [r for r in rank_wallets(db) if r["eligible"]]
    return {r["address"] for r in ranked[: cfg["top_n"]]}


def ranking_view(db: Session, limit: int = 20) -> dict:
    cfg = _cfg()
    ranked = rank_wallets(db, include_failed=True)
    eligible = [r for r in ranked if r["eligible"]]
    return {
        "weights": WEIGHTS,
        "filters": {"min_roi": cfg["min_roi"], "min_profit_factor": cfg["min_pf"],
                    "min_settled": cfg["min_settled"], "active_days": cfg["active_days"],
                    "require_full_history": cfg["require_full_history"]},
        "top_n_selected": cfg["top_n"],
        "eligible_count": len(eligible),
        "top": eligible[:limit],
        "rejected_sample": [r for r in ranked if not r["eligible"]][:limit],
    }
