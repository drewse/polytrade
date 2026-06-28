"""Deep historical backfill worker — improves wallet DATA QUALITY (coverage) by
paging older trade history into our store. Resumable (per-wallet cursor),
idempotent, fail-closed, rate-limited.

It NEVER places trades, never auto-approves wallets, and never touches live
execution / routing / sizing / bankroll / open positions. It only ingests trade
history and recomputes WalletStat coverage (the same path the existing backfill
uses) and updates WalletBackfillProgress.
"""
from __future__ import annotations

import os
import time
from datetime import datetime

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import public_profile
from . import wallet_approval_models as am
from .models import DiscoverySource, Trade, Wallet, WalletCandidate  # noqa: F401
from .wallet_audit_models import PublicWalletProfile

_DATA = "https://data-api.polymarket.com"
_HEADERS = {"User-Agent": "polytrade-deepbackfill/1.0"}
PAGE_SIZE = int(os.getenv("LIVE_DEEP_BACKFILL_PAGE_SIZE", "500"))
MAX_PAGES_PER_RUN = int(os.getenv("LIVE_DEEP_BACKFILL_MAX_PAGES", "8"))
COVERAGE_TARGET = float(os.getenv("LIVE_DEEP_BACKFILL_COVERAGE_TARGET", "0.85"))
_RATE_LIMIT_S = 0.3


# ---------------------------------------------------------------------------
# coverage
# ---------------------------------------------------------------------------
def _grade(coverage: float | None, exhausted: bool) -> str:
    if exhausted:
        return "complete"
    if coverage is None:
        return "unknown"
    if coverage >= 0.90:
        return "complete"
    if coverage >= 0.70:
        return "high"
    if coverage >= 0.40:
        return "medium"
    if coverage > 0.0:
        return "low"
    return "unknown"


def compute_coverage(db: Session, address: str, *, exhausted: bool = False) -> dict:
    """Internal-vs-public coverage for one wallet (read-only metric)."""
    w = db.scalar(select(Wallet).where(func.lower(Wallet.address) == address.lower()))
    internal_volume = internal_trades = internal_markets = 0
    first_t = last_t = None
    if w:
        internal_volume = float(db.scalar(select(func.coalesce(func.sum(Trade.size), 0.0))
                                          .where(Trade.wallet_id == w.id)) or 0.0)
        internal_trades = int(db.scalar(select(func.count()).select_from(Trade)
                                        .where(Trade.wallet_id == w.id)) or 0)
        internal_markets = int(db.scalar(select(func.count(func.distinct(Trade.market_id)))
                                         .where(Trade.wallet_id == w.id)) or 0)
        first_t = db.scalar(select(func.min(Trade.timestamp)).where(Trade.wallet_id == w.id))
        last_t = db.scalar(select(func.max(Trade.timestamp)).where(Trade.wallet_id == w.id))
    pub = public_profile.as_dict(db.get(PublicWalletProfile, address))
    pub_vol = (pub or {}).get("volume_all")
    pub_pred = (pub or {}).get("predictions")
    cov_vol = round(min(1.0, internal_volume / pub_vol), 5) if (pub_vol and pub_vol > 0) else None
    cov_trd = round(min(1.0, internal_markets / pub_pred), 5) if (pub_pred and pub_pred > 0) else None
    ratios = [r for r in (cov_vol, cov_trd) if r is not None]
    cov = max(ratios) if ratios else None
    return {
        "internal_volume": round(internal_volume, 2), "internal_trades": internal_trades,
        "internal_markets": internal_markets, "public_volume": pub_vol, "public_predictions": pub_pred,
        "coverage_volume": cov_vol, "coverage_trades": cov_trd, "coverage_ratio": cov,
        "coverage_grade": _grade(cov, exhausted),
        "partial_history": not (exhausted or (cov is not None and cov >= 0.80)),
        "first_internal_trade": first_t, "last_internal_trade": last_t,
    }


# ---------------------------------------------------------------------------
# priority queue
# ---------------------------------------------------------------------------
def _priority_queue(db: Session) -> list[tuple[str, int]]:
    """Ordered (address, priority) — production wallets first, then approved,
    strong candidates, discovery, profitable public wallets."""
    from . import live_ranking
    from .wallet_audit_models import PublicWalletProfile
    pri: dict[str, int] = {}

    def bump(addr, p):
        if addr:
            pri[addr.lower()] = max(pri.get(addr.lower(), 0), p)

    for a in live_ranking.eligible_addresses(db):
        bump(a, 100)
    for ap in db.scalars(select(am.WalletApproval).where(am.WalletApproval.manually_approved.is_(True))).all():
        bump(ap.address, 90)
    for pr in db.scalars(select(am.WalletBackfillProgress).where(am.WalletBackfillProgress.requested.is_(True))).all():
        bump(pr.address, 95)
    cands = {c.wallet_id: c for c in db.scalars(select(WalletCandidate).where(
        WalletCandidate.classification.in_(("elite_candidate", "good_candidate")))).all()}
    waddr = {w.id: w.address for w in db.scalars(select(Wallet).where(Wallet.id.in_(cands.keys()))).all()} if cands else {}
    for wid in cands:
        bump(waddr.get(wid), 70)
    for ds in db.scalars(select(DiscoverySource)).all():
        bump(ds.wallet_address, 60)
    for pp in db.scalars(select(PublicWalletProfile).where(PublicWalletProfile.pnl_all > 0)).all():
        bump(pp.address, 50)
    # de-prioritize wallets already exhausted (unless re-requested)
    done = {p.address.lower() for p in db.scalars(select(am.WalletBackfillProgress).where(
        am.WalletBackfillProgress.exhausted.is_(True), am.WalletBackfillProgress.requested.is_(False))).all()}
    out = [(a, p) for a, p in pri.items() if a not in done]
    out.sort(key=lambda kv: -kv[1])
    return out


# ---------------------------------------------------------------------------
# paged fetch (default = data-api; injectable for tests)
# ---------------------------------------------------------------------------
def _default_fetch(address: str, offset: int, limit: int):
    from .polymarket_client import parse_trades
    r = httpx.get(f"{_DATA}/trades", params={"user": address, "limit": limit, "offset": offset},
                  timeout=15, headers=_HEADERS)
    r.raise_for_status()
    return parse_trades(r.json())


# ---------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------
def _backfill_one(db: Session, address: str, *, max_pages: int, page_size: int, fetch_fn) -> dict:
    from . import services
    from .polymarket_client import LivePolymarketClient

    wallet = services.get_or_create_wallet(db, address)
    prog = db.get(am.WalletBackfillProgress, address) or am.WalletBackfillProgress(address=address)
    prog.status = "running"
    prog.last_run_at = datetime.utcnow()
    db.add(prog); db.commit()

    offset = prog.cursor_offset or 0
    pages = 0
    inserted = 0
    exhausted = prog.exhausted
    error = None
    try:
        for _ in range(max_pages):
            dtos = fetch_fn(address, offset, page_size)
            if not dtos:
                exhausted = True
                break
            # market metadata + resolution (best-effort) then upsert trades
            needed = list({d.market_id for d in dtos})
            try:
                client = LivePolymarketClient()
                try:
                    for mdto in client.get_markets_by_conditions(needed):
                        services.upsert_market(db, mdto)
                finally:
                    client.close()
            except Exception as exc:  # noqa: BLE001  (best-effort; trades still ingest)
                print(f"[deep-backfill] market meta fetch failed {address[:10]}: {exc}")
            from .models import Market
            db.flush()
            have = {m for (m,) in db.execute(select(Market.id).where(Market.id.in_(needed))).all()}
            for mid in set(needed) - have:
                db.add(Market(id=mid, question=f"(market {mid[:10]}…)", outcomes=["Yes", "No"], prices=[0.5, 0.5]))
            db.commit()
            for d in dtos:
                if services.insert_trade(db, d, wallet) is not None:
                    inserted += 1
            db.commit()
            offset += len(dtos)
            pages += 1
            if len(dtos) < page_size:
                exhausted = True
                break
            cov = compute_coverage(db, address, exhausted=exhausted)
            if cov["coverage_ratio"] is not None and cov["coverage_ratio"] >= COVERAGE_TARGET:
                break
            if fetch_fn is _default_fetch and _RATE_LIMIT_S:
                time.sleep(_RATE_LIMIT_S)
    except Exception as exc:  # noqa: BLE001  (fail-closed per wallet)
        error = str(exc)[:300]

    cov = compute_coverage(db, address, exhausted=exhausted)
    # recompute stats with the now-deeper history (partial only if not exhausted/covered)
    try:
        services.recompute_wallet_stats(db, wallet, partial=cov["partial_history"], reconstruct=True)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        error = error or f"recompute: {str(exc)[:200]}"

    prog.cursor_offset = offset
    prog.pages_fetched = (prog.pages_fetched or 0) + pages
    prog.exhausted = exhausted
    prog.internal_volume = cov["internal_volume"]
    prog.internal_trades = cov["internal_trades"]
    prog.internal_markets = cov["internal_markets"]
    prog.public_volume = cov["public_volume"]
    prog.public_predictions = cov["public_predictions"]
    prog.coverage_volume = cov["coverage_volume"]
    prog.coverage_trades = cov["coverage_trades"]
    prog.coverage_ratio = cov["coverage_ratio"]
    prog.coverage_grade = cov["coverage_grade"]
    prog.partial_history = cov["partial_history"]
    prog.first_internal_trade = cov["first_internal_trade"]
    prog.last_internal_trade = cov["last_internal_trade"]
    prog.requested = False
    prog.status = "failed" if error else "completed"
    prog.error = error
    db.add(prog); db.commit()
    return {"address": address, "pages_fetched": pages, "trades_inserted": inserted,
            "coverage_grade": cov["coverage_grade"], "coverage_ratio": cov["coverage_ratio"],
            "exhausted": exhausted, "status": prog.status, "error": error}


def run_deep_backfill(db: Session, *, batch: int = 3, max_pages: int | None = None,
                      page_size: int | None = None, fetch_fn=None) -> dict:
    """Backfill the highest-priority wallets that still need coverage. Idempotent +
    resumable (per-wallet cursor). Never auto-approves, never trades."""
    fetch_fn = fetch_fn or _default_fetch
    max_pages = max_pages or MAX_PAGES_PER_RUN
    page_size = page_size or PAGE_SIZE
    queue = _priority_queue(db)
    results = []
    for address, priority in queue[:batch]:
        # ensure a progress row exists with the priority for the dashboard
        prog = db.get(am.WalletBackfillProgress, address) or am.WalletBackfillProgress(address=address)
        prog.priority = priority
        db.add(prog); db.commit()
        results.append(_backfill_one(db, address, max_pages=max_pages, page_size=page_size, fetch_fn=fetch_fn))
    return {
        "batch": batch, "queued_total": len(queue), "processed": len(results),
        "completed": sum(1 for r in results if r["status"] == "completed"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "trades_inserted": sum(r["trades_inserted"] for r in results),
        "results": results,
        "note": "data-quality only — no wallet auto-approved, no orders placed",
    }


def backfill_status(db: Session) -> dict:
    rows = db.scalars(select(am.WalletBackfillProgress)).all()
    from . import live_ranking
    prod = {a.lower() for a in live_ranking.eligible_addresses(db)}
    covs = [r.coverage_ratio for r in rows if r.coverage_ratio is not None]
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    prod_low = sorted([r for r in rows if r.address.lower() in prod and (r.coverage_ratio is not None)],
                      key=lambda r: r.coverage_ratio)[:10]
    queue = _priority_queue(db)
    return {
        "queued": len(queue),
        "tracked": len(rows),
        "by_status": by_status,
        "running": [r.address for r in rows if r.status == "running"],
        "completed": by_status.get("completed", 0),
        "failed": by_status.get("failed", 0),
        "average_coverage": round(sum(covs) / len(covs), 4) if covs else None,
        "page_size": page_size_default(), "max_pages_per_run": MAX_PAGES_PER_RUN,
        "coverage_target": COVERAGE_TARGET,
        "top_low_coverage_production": [{"address": r.address, "coverage_ratio": r.coverage_ratio,
                                         "grade": r.coverage_grade} for r in prod_low],
        "next_queue": [{"address": a, "priority": p} for a, p in queue[:15]],
        "safety": "read-from-venue + ingest history only — never trades or auto-approves",
    }


def page_size_default() -> int:
    return PAGE_SIZE
