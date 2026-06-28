"""Production wallet-ranking HARDENING gates (eligibility only).

Adds configurable eligibility gates derived from the Top-20 Audit findings:
public all-time P/L, partial-history, internal-coverage, and whale/market-maker
volume. These ONLY affect which wallets are eligible to copy — they never touch
execution, routing, sizing, slippage, bankroll, pause/resume/halt, or open trades.

SAFETY: defaults to AUDIT-ONLY mode (LIVE_RANKING_AUDIT_ONLY=true), in which the
gates are computed and reported but DO NOT change eligibility. Enforcement only
happens when LIVE_RANKING_AUDIT_ONLY=false. evaluate() is pure + deterministic.
"""
from __future__ import annotations

import os

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import public_profile
from .models import Trade, Wallet, WalletStat
from .wallet_audit_models import PublicWalletProfile


def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def config() -> dict:
    return {
        "audit_only": _truthy(os.getenv("LIVE_RANKING_AUDIT_ONLY", "true")),
        # 1. public all-time P/L gate
        "min_public_pnl": float(os.getenv("LIVE_MIN_PUBLIC_ALL_TIME_PNL", "0")),
        "require_public_stats": _truthy(os.getenv("LIVE_REQUIRE_PUBLIC_STATS", "false")),
        # exclude wallets whose public all-time P/L is not strictly positive
        "require_public_profitable": _truthy(os.getenv("LIVE_REQUIRE_PUBLIC_PROFITABLE", "false")),
        # 2. partial-history gate
        "allow_partial_history": _truthy(os.getenv("LIVE_ALLOW_PARTIAL_HISTORY", "false")),
        # 3. coverage gate
        "min_coverage_ratio": float(os.getenv("LIVE_MIN_COVERAGE_RATIO", "0.05")),
        # 4. whale / market-maker gate (volume; optional max single-position size)
        "max_public_volume": float(os.getenv("LIVE_MAX_PUBLIC_VOLUME", "1000000")),
        "max_position_size": float(os.getenv("LIVE_MAX_PUBLIC_POSITION_SIZE", "0")),   # 0 = disabled
    }


def coverage_ratio(internal_volume: float | None, internal_settled: int | None,
                   public: dict | None) -> float | None:
    """Best-available internal coverage estimate: max(internal_vol/public_vol,
    internal_settled/public_predictions). None when neither is computable."""
    if not public:
        return None
    ratios = []
    pv = public.get("volume_all")
    if pv and pv > 0 and internal_volume is not None:
        ratios.append(internal_volume / pv)
    pp = public.get("predictions")
    if pp and pp > 0 and internal_settled is not None:
        ratios.append((internal_settled or 0) / pp)
    return round(max(ratios), 5) if ratios else None


def _public_available(public: dict | None) -> bool:
    return bool(public and public.get("fetch_status") != "error" and public.get("pnl_all") is not None)


def evaluate(*, partial_history: bool | None, public: dict | None,
             internal_volume: float | None, internal_settled: int | None,
             cfg: dict | None = None) -> dict:
    """PURE hardened-gate verdict. Returns:
      {pass, exclusions:[{code,message}], unknowns:[...], coverage, public_available}
    `pass` reflects whether the wallet clears the hardened gates (independent of
    audit-only mode — the caller decides whether to enforce)."""
    cfg = cfg or config()
    exclusions: list[dict] = []
    unknowns: list[str] = []

    # 2. partial-history gate
    if partial_history and not cfg["allow_partial_history"]:
        exclusions.append({"code": "partial_history",
                           "message": "internal stats are partial_history (recent-window only)"})

    has_public = _public_available(public)
    cov = coverage_ratio(internal_volume, internal_settled, public)
    if not has_public:
        unknowns.append("public_stats")
        if cfg["require_public_stats"]:
            exclusions.append({"code": "missing_public_stats",
                               "message": "public stats unavailable and LIVE_REQUIRE_PUBLIC_STATS=true"})
    else:
        # 1. public all-time P/L gate
        pnl = public.get("pnl_all")
        if pnl is not None and pnl < cfg["min_public_pnl"]:
            exclusions.append({"code": "public_pnl_below_min",
                               "message": f"public all-time P/L {pnl:,.0f} < {cfg['min_public_pnl']:,.0f}"})
        elif cfg["require_public_profitable"] and pnl is not None and pnl <= 0:
            exclusions.append({"code": "public_not_profitable",
                               "message": f"public all-time P/L {pnl:,.0f} is not positive"})
        # 4. whale / market-maker gate
        vol = public.get("volume_all")
        if vol is None:
            unknowns.append("public_volume")
        elif cfg["max_public_volume"] > 0 and vol > cfg["max_public_volume"]:
            exclusions.append({"code": "whale_volume",
                               "message": f"public volume {vol:,.0f} > {cfg['max_public_volume']:,.0f} (likely MM/whale)"})
        bigpos = public.get("largest_position_size")
        if cfg["max_position_size"] > 0 and bigpos and bigpos > cfg["max_position_size"]:
            exclusions.append({"code": "large_position",
                               "message": f"largest public position {bigpos:,.0f} > {cfg['max_position_size']:,.0f}"})
        # 3. coverage gate (only when computable)
        if cov is None:
            unknowns.append("coverage")
        elif cov < cfg["min_coverage_ratio"]:
            exclusions.append({"code": "low_coverage",
                               "message": f"internal coverage {cov:.4f} < {cfg['min_coverage_ratio']:.4f}"})

    return {"pass": len(exclusions) == 0, "exclusions": exclusions, "unknowns": unknowns,
            "coverage": cov, "public_available": has_public}


def verdict_for_wallet(db: Session, wallet: Wallet, stat: WalletStat | None,
                       cfg: dict | None = None) -> dict:
    """DB convenience: load the cached public profile + internal volume for a wallet
    and run evaluate(). Read-only."""
    cfg = cfg or config()
    pub = public_profile.as_dict(db.get(PublicWalletProfile, wallet.address)) if wallet else None
    vol = db.scalar(select(func.coalesce(func.sum(Trade.size), 0.0)).where(
        Trade.wallet_id == wallet.id)) if wallet else 0.0
    return evaluate(partial_history=(stat.partial_history if stat else None), public=pub,
                    internal_volume=float(vol or 0.0),
                    internal_settled=(stat.num_settled if stat else None), cfg=cfg)
