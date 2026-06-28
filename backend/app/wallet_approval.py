"""Manual wallet approval + gated promotion. Operator controls (disable / enable /
approve / reject / watchlist / request-backfill) plus the Approved-Wallets view and
the gated Approval Queue.

Affects wallet ELIGIBILITY VISIBILITY + manual controls only. Never touches
execution, routing, sizing, bankroll, slippage, or open positions. Manual disable
is a HARD override (never copied even if it ranks #1). Manual approval is a
positive marker that still requires the normal safety gates.
"""
from __future__ import annotations

import os
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import deep_backfill, live_ranking, public_profile, wallet_audit
from . import wallet_approval_models as am
from .models import Wallet, WalletStat
from .wallet_audit_models import PublicWalletProfile

PROFILE_BASE = "https://polymarket.com/profile/"
ACTIONS = ("disable", "enable", "approve", "remove_approval", "reject", "watchlist", "reset", "request_backfill")


def _cfg() -> dict:
    return {
        # candidate criteria (configurable)
        "min_public_pnl": float(os.getenv("LIVE_QUEUE_MIN_PUBLIC_PNL", "0")),
        "min_roi": float(os.getenv("LIVE_QUEUE_MIN_ROI", "0.0")),
        "min_pf": float(os.getenv("LIVE_QUEUE_MIN_PF", "1.20")),
        "min_settled": int(os.getenv("LIVE_QUEUE_MIN_SETTLED", "20")),
        "min_coverage": float(os.getenv("LIVE_QUEUE_MIN_COVERAGE", "0.25")),
        "active_days": int(os.getenv("LIVE_ACTIVE_DAYS", "60")),
    }


def get_status(db: Session, address: str) -> am.WalletApproval | None:
    return db.scalar(select(am.WalletApproval).where(func.lower(am.WalletApproval.address) == address.lower()))


def status_map(db: Session) -> dict[str, am.WalletApproval]:
    return {a.address.lower(): a for a in db.scalars(select(am.WalletApproval)).all()}


def set_status(db: Session, address: str, action: str, *, by: str | None = None, note: str | None = None) -> dict:
    """Apply a manual control. Never promotes a wallet to copyable by itself."""
    if action not in ACTIONS:
        return {"ok": False, "error": f"unknown action '{action}'"}
    now = datetime.utcnow()
    if action == "request_backfill":
        prog = db.get(am.WalletBackfillProgress, address) or am.WalletBackfillProgress(address=address)
        prog.requested = True
        db.add(prog); db.commit()
        return {"ok": True, "address": address, "action": action, "requested": True}
    ap = get_status(db, address) or am.WalletApproval(address=address)
    if action == "disable":
        ap.manually_disabled = True
        ap.disabled_by = by or "operator"
        ap.disabled_at = now
        ap.note = note if note is not None else ap.note
    elif action == "enable":
        ap.manually_disabled = False
        ap.disabled_by = None
        ap.disabled_at = None
    elif action == "approve":
        ap.manually_approved = True
        ap.status = "approved"
        ap.approved_by = by or "operator"
        ap.approved_at = now
        ap.note = note if note is not None else ap.note
    elif action == "remove_approval":
        ap.manually_approved = False
        if ap.status == "approved":
            ap.status = "none"
    elif action == "reject":
        ap.status = "rejected"
        ap.manually_approved = False
        ap.note = note if note is not None else ap.note
    elif action == "watchlist":
        ap.status = "watchlist"
        ap.note = note if note is not None else ap.note
    elif action == "reset":
        ap.status = "none"
        ap.manually_approved = False
    db.add(ap); db.commit()
    return {"ok": True, "address": address, "action": action, "status": ap.status,
            "manually_approved": ap.manually_approved, "manually_disabled": ap.manually_disabled}


# ---------------------------------------------------------------------------
# Approved Wallets view
# ---------------------------------------------------------------------------
def _row(db: Session, address: str, rank_row: dict | None, ap: am.WalletApproval | None,
         rank: int | None) -> dict:
    w = db.scalar(select(Wallet).where(func.lower(Wallet.address) == address.lower()))
    stat = db.scalar(select(WalletStat).where(WalletStat.wallet_id == w.id)) if w else None
    pub = public_profile.as_dict(db.get(PublicWalletProfile, address))
    prog = db.get(am.WalletBackfillProgress, address)
    warnings = []
    if rank_row is not None:
        # reuse the audit's warning engine via a light internal dict
        internal = {"roi": rank_row.get("roi"), "profit_factor": rank_row.get("profit_factor"),
                    "partial_history": rank_row.get("partial_history"),
                    "num_settled": rank_row.get("num_settled"), "max_drawdown": (stat.max_drawdown if stat else None),
                    "last_active": (w.last_active.isoformat() if (w and w.last_active) else None),
                    "volume": (prog.internal_volume if prog else None)}
        warnings = wallet_audit._warnings(rank_row, internal, {}, pub, {"level": (prog.coverage_grade if prog else "unknown")})
    disabled = bool(ap and ap.manually_disabled)
    return {
        "address": address, "profile_url": f"{PROFILE_BASE}{address}",
        "display_name": (pub or {}).get("display_name") or (pub or {}).get("pseudonym"),
        "enabled": not disabled,
        "approval_status": (ap.status if ap else "none"),
        "manually_approved": bool(ap and ap.manually_approved),
        "manually_disabled": disabled,
        "disabled_by": (ap.disabled_by if ap else None),
        "approved_by": (ap.approved_by if ap else None),
        "note": (ap.note if ap else None),
        "production_rank": rank,
        "production_rank_score": (rank_row or {}).get("production_rank_score"),
        "roi": (rank_row or {}).get("roi"), "profit_factor": (rank_row or {}).get("profit_factor"),
        "win_rate": (rank_row or {}).get("win_rate"), "num_settled": (rank_row or {}).get("num_settled"),
        "public_all_time_pnl": (pub or {}).get("pnl_all"), "public_volume": (pub or {}).get("volume_all"),
        "coverage_ratio": (prog.coverage_ratio if prog else None),
        "coverage_grade": (prog.coverage_grade if prog else "unknown"),
        "warning_count": len(warnings), "warnings": warnings,
        "last_active": (w.last_active.isoformat() if (w and w.last_active) else None),
        "reason_selected": (rank_row or {}).get("filter_reason"),
        "reason_disabled": (ap.note if (disabled and ap) else None),
        "copyable": bool(rank_row and rank_row.get("eligible") and not disabled),
        "why_not_copyable": _why_not(rank_row, ap, disabled),
    }


def _why_not(rank_row, ap, disabled) -> str | None:
    if disabled:
        return "manually disabled (hard override)"
    if ap and ap.status == "rejected":
        return "manually rejected"
    if ap and ap.status == "watchlist":
        return "watchlist (monitored only)"
    if rank_row and not rank_row.get("eligible"):
        return rank_row.get("filter_reason")
    return None


def approved_wallets(db: Session) -> dict:
    """The wallets currently in/around production copy eligibility, with manual
    controls + the data needed to decide. Read-only."""
    ranked = live_ranking.rank_wallets(db, include_failed=True)
    rank_by_addr = {r["address"].lower(): r for r in ranked}
    eligible_set = {a.lower() for a in live_ranking.eligible_addresses(db)}
    rank_of = {}
    for i, r in enumerate([x for x in ranked if x["eligible"]]):
        rank_of[r["address"].lower()] = i + 1
    appr = status_map(db)
    # universe: currently eligible + any manually approved/disabled wallet
    universe = set(eligible_set) | {a for a, ap in appr.items() if ap.manually_approved or ap.manually_disabled
                                    or ap.status in ("approved", "rejected", "watchlist")}
    rows = [_row(db, rank_by_addr.get(a, {}).get("address", a), rank_by_addr.get(a),
                 appr.get(a), rank_of.get(a)) for a in universe]
    rows.sort(key=lambda r: (r["production_rank"] is None, r["production_rank"] or 1e9))
    return {
        "generated_at": datetime.utcnow().isoformat(),
        "copied_count": sum(1 for r in rows if r["copyable"]),
        "disabled_count": sum(1 for r in rows if r["manually_disabled"]),
        "approved_count": sum(1 for r in rows if r["manually_approved"]),
        "require_manual_approval": live_ranking._require_manual_approval(),
        "wallets": rows,
        "safety": "manual disable is a HARD override; approval still requires safety gates",
    }


# ---------------------------------------------------------------------------
# Gated Approval Queue
# ---------------------------------------------------------------------------
def _recommendation_score(rank_row, pub, coverage) -> float:
    roi = (rank_row.get("roi") or 0)
    pf = (rank_row.get("profit_factor") or 0)
    cov = coverage or 0
    pnl = (pub or {}).get("pnl_all") or 0
    score = (_clip(roi * 2, 0, 1) * 30 + _clip((pf - 1) / 2, 0, 1) * 25
             + _clip(cov or 0, 0, 1) * 25 + (15 if pnl > 0 else 0)
             + _clip((rank_row.get("num_settled") or 0) / 100, 0, 1) * 5)
    return round(_clip(score, 0, 100), 1)


def _clip(v, lo, hi):
    return max(lo, min(hi, v))


def approval_queue(db: Session) -> dict:
    """Wallets that look genuinely profitable (deeper data + positive public P/L)
    and are NOT yet approved/disabled/rejected — presented for MANUAL approval.
    Nothing here is copied until explicitly approved."""
    cfg = _cfg()
    ranked = {r["address"].lower(): r for r in live_ranking.rank_wallets(db, include_failed=True)}
    appr = status_map(db)
    eligible_set = {a.lower() for a in live_ranking.eligible_addresses(db)}
    progs = {p.address.lower(): p for p in db.scalars(select(am.WalletBackfillProgress)).all()}
    pubs = {p.address.lower(): public_profile.as_dict(p) for p in db.scalars(select(PublicWalletProfile)).all()}
    now = datetime.utcnow()
    candidates = []
    for addr, r in ranked.items():
        ap = appr.get(addr)
        if ap and (ap.manually_disabled or ap.status in ("approved", "rejected")):
            continue
        pub = pubs.get(addr)
        prog = progs.get(addr)
        cov = prog.coverage_ratio if prog else r.get("coverage_ratio")
        grade = prog.coverage_grade if prog else None
        # criteria
        if (r.get("roi") or 0) <= cfg["min_roi"]:
            continue
        if (r.get("profit_factor") or 0) < cfg["min_pf"]:
            continue
        if (r.get("num_settled") or 0) < cfg["min_settled"]:
            continue
        pnl = (pub or {}).get("pnl_all")
        if pnl is not None and pnl <= cfg["min_public_pnl"]:
            continue                                         # obvious lifetime loser excluded
        cov_ok = (grade in ("medium", "high", "complete")) or (cov is not None and cov >= cfg["min_coverage"])
        why_not_auto = []
        if not cov_ok:
            why_not_auto.append("coverage below target — request deeper backfill")
        if pnl is None:
            why_not_auto.append("public stats not yet fetched")
        why_not_auto.append("manual approval required (no wallet auto-promotes)")
        candidates.append({
            "address": r["address"], "profile_url": f"{PROFILE_BASE}{r['address']}",
            "display_name": (pub or {}).get("display_name") or (pub or {}).get("pseudonym"),
            "recommendation_score": _recommendation_score(r, pub, cov),
            "public_all_time_pnl": pnl, "public_volume": (pub or {}).get("volume_all"),
            "roi": r.get("roi"), "profit_factor": r.get("profit_factor"), "win_rate": r.get("win_rate"),
            "num_settled": r.get("num_settled"),
            "coverage_ratio": cov, "coverage_grade": grade or "unknown",
            "max_drawdown": None, "cluster": r.get("classification"),
            "currently_eligible": addr in eligible_set,
            "watchlisted": bool(ap and ap.status == "watchlist"),
            "why_recommended": (f"internal ROI {((r.get('roi') or 0)*100):.0f}% / PF {r.get('profit_factor')}, "
                                f"{r.get('num_settled')} settled" + (f", public all-time +{pnl:,.0f}" if pnl and pnl > 0 else "")),
            "why_not_auto_approved": "; ".join(why_not_auto),
            "coverage_ok": cov_ok,
        })
    candidates.sort(key=lambda c: -c["recommendation_score"])
    return {
        "generated_at": now.isoformat(),
        "criteria": cfg,
        "candidates": candidates,
        "count": len(candidates),
        "safety": "GATED — no wallet becomes copyable automatically; manual approval required",
    }
