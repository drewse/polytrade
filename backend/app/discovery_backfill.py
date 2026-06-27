"""Discovery Backfill Queue Worker.

Turns Discovery 2.0 wallets (discovery_sources, needs_backfill=true) into usable
WalletStat by backfilling their trade history in PRIORITY ORDER, reusing the
EXISTING backfill logic (services.backfill_wallet -> recompute_wallet_stats).

SAFETY:
  * Creates Wallet/Trade/WalletStat (the whole point) but does NOT touch live
    order execution, eligibility RULES, ranking LOGIC, order sizing, slippage,
    order mode, pause/resume/halt, open positions, active trades, or risk limits.
  * No wallet is ever FORCED eligible and no live trade is triggered here. The
    production eligible set may change ONLY naturally — via the unchanged ranking
    once real stats exist.
  * Idempotent + safe to rerun (completed wallets leave the queue), rate-limited,
    and fail-closed (a wallet's API error is recorded, never crashes the batch).
"""
from __future__ import annotations

import time
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import services
from .models import DiscoverySource, Wallet, WalletStat


def _has_stats(db: Session, address: str) -> bool:
    w = db.scalar(select(Wallet).where(func.lower(Wallet.address) == address.lower()))
    return bool(w and db.get(WalletStat, w.id))


def _queue(db: Session) -> list[dict]:
    """Distinct wallets needing backfill, ordered: backfill_priority desc,
    discovery_score desc, oldest first_seen first."""
    rows = db.scalars(select(DiscoverySource).where(DiscoverySource.needs_backfill == True)).all()  # noqa: E712
    agg: dict[str, dict] = {}
    for r in rows:
        cur = agg.get(r.wallet_address)
        if cur is None:
            agg[r.wallet_address] = {"wallet": r.wallet_address, "priority": r.backfill_priority,
                                     "score": r.discovery_score, "first_seen": r.first_seen}
        else:
            cur["priority"] = max(cur["priority"], r.backfill_priority)
            cur["score"] = max(cur["score"], r.discovery_score)
            if r.first_seen and (cur["first_seen"] is None or r.first_seen < cur["first_seen"]):
                cur["first_seen"] = r.first_seen
    return sorted(agg.values(),
                  key=lambda w: (-w["priority"], -w["score"], w["first_seen"] or datetime.max))


def _set_rows(db: Session, address: str, **vals) -> None:
    for r in db.scalars(select(DiscoverySource).where(DiscoverySource.wallet_address == address)).all():
        for k, v in vals.items():
            setattr(r, k, v)


def run_backfill_batch(db: Session, *, batch: int = 5, backfill_fn=None, rate_limit_s: float = 0.4) -> dict:
    """Process the top `batch` queued wallets in priority order. Idempotent."""
    backfill_fn = backfill_fn or services.backfill_wallet
    queue = _queue(db)[:max(1, batch)]
    results = []
    for w in queue:
        addr = w["wallet"]
        _set_rows(db, addr, backfill_status="running", last_backfill_attempt_at=datetime.utcnow())
        db.commit()
        try:
            res = backfill_fn(db, addr) or {}
            if res.get("ok"):
                imported = int(res.get("trades_inserted", res.get("trades_fetched", 0)) or 0)
                has = _has_stats(db, addr)
                _set_rows(db, addr, backfill_status="completed", backfill_completed_at=datetime.utcnow(),
                          trades_imported=imported, stats_updated=has, needs_backfill=False, backfill_error=None)
                results.append({"wallet": addr, "ok": True, "trades_imported": imported, "stats_updated": has})
            else:
                err = str(res.get("error", "backfill failed"))[:500]
                _set_rows(db, addr, backfill_status="failed", backfill_error=err)   # needs_backfill stays True -> retry
                results.append({"wallet": addr, "ok": False, "error": err})
        except Exception as exc:  # noqa: BLE001  (fail closed; never crash the batch)
            db.rollback()
            _set_rows(db, addr, backfill_status="failed", backfill_error=str(exc)[:500])
            results.append({"wallet": addr, "ok": False, "error": str(exc)})
        db.commit()
        if rate_limit_s:
            time.sleep(rate_limit_s)   # rate-limit external API calls
    return {
        "batch_size": batch,
        "wallets_processed": len(results),
        "completed": sum(1 for r in results if r["ok"]),
        "failed": sum(1 for r in results if not r["ok"]),
        "trades_imported": sum(r.get("trades_imported", 0) for r in results if r["ok"]),
        "stats_updated": sum(1 for r in results if r.get("stats_updated")),
        "results": results,
        "queue_remaining": max(0, len(_queue(db))),
        "note": "Backfill only — no wallet forced eligible; eligibility may change only via the "
                "unchanged ranking once stats exist; no live trade triggered.",
    }


def backfill_status(db: Session, *, recent: int = 15) -> dict:
    """READ-ONLY queue status: per-wallet status counts, currently running, latest
    errors, last run time, recently completed wallets."""
    by_wallet: dict[str, list] = {}
    for r in db.scalars(select(DiscoverySource)).all():
        by_wallet.setdefault(r.wallet_address, []).append(r)

    counts = {"pending": 0, "running": 0, "completed": 0, "failed": 0, "skipped": 0}
    running, errors, completed = [], [], []
    last_run = None
    for addr, rs in by_wallet.items():
        st = rs[0].backfill_status or "pending"
        counts[st] = counts.get(st, 0) + 1
        att = max((r.last_backfill_attempt_at for r in rs if r.last_backfill_attempt_at), default=None)
        if att and (last_run is None or att > last_run):
            last_run = att
        if st == "running":
            running.append(addr)
        if st == "failed":
            err = next((r.backfill_error for r in rs if r.backfill_error), None)
            errors.append({"wallet": addr, "error": err, "at": att.isoformat() if att else None})
        comp = max((r.backfill_completed_at for r in rs if r.backfill_completed_at), default=None)
        if st == "completed" and comp:
            completed.append({"wallet": addr, "completed_at": comp,
                              "trades_imported": max((r.trades_imported for r in rs), default=0),
                              "stats_updated": any(r.stats_updated for r in rs)})
    completed.sort(key=lambda c: c["completed_at"], reverse=True)
    errors.sort(key=lambda e: e["at"] or "", reverse=True)
    return {
        "counts": counts,
        "pending": counts["pending"], "running": counts["running"],
        "completed": counts["completed"], "failed": counts["failed"], "skipped": counts["skipped"],
        "currently_running": running,
        "latest_errors": errors[:recent],
        "recently_completed": [{**c, "completed_at": c["completed_at"].isoformat()} for c in completed[:recent]],
        "last_run": last_run.isoformat() if last_run else None,
        "read_only": True,
    }
