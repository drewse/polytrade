"""Background fill-reconciliation worker.

Periodically reconciles live executions flagged `fill_pending_reconciliation`
against the venue's ACTUAL fills (via live.reconcile_pending). ACCOUNTING ONLY —
it never places, routes, cancels, or modifies orders; it only corrects recorded
fill price / cost basis / exposure / realized P&L / bankroll once the venue's
execution records become available.

Mirrors the auto_worker daemon pattern: one guarded loop, one pass at a time, a
failed pass is logged and the loop continues. Disabled by setting
LIVE_FILL_RECONCILER_ENABLED=false.
"""
from __future__ import annotations

import os
import threading
import time
import traceback
from datetime import datetime

_cycle_lock = threading.Lock()
_start_lock = threading.Lock()
_state = {"started": False, "thread": None, "last_run_at": None,
          "last_error": None, "last_reconciled": 0}


def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def get_config() -> dict:
    return {
        "enabled": _truthy(os.getenv("LIVE_FILL_RECONCILER_ENABLED", "true")),
        # only run when there are real orders to reconcile (polymarket executor)
        "interval_seconds": max(30, int(os.getenv("LIVE_FILL_RECONCILER_INTERVAL_S", "120"))),
        "startup_delay_seconds": max(0, int(os.getenv("LIVE_FILL_RECONCILER_DELAY_S", "20"))),
    }


def run_one_pass(wait: bool = True) -> dict:
    acquired = _cycle_lock.acquire(blocking=wait)
    if not acquired:
        return {"skipped": "a reconciliation pass is already running"}
    try:
        from . import live
        from .db import session_scope
        db = session_scope()
        try:
            if live.pending_reconciliation_count(db) == 0:
                return {"reconciled": 0, "note": "nothing pending"}
            out = live.reconcile_pending(db)
            _state["last_run_at"] = datetime.utcnow()
            _state["last_error"] = None
            _state["last_reconciled"] = out.get("reconciled", 0)
            return out
        finally:
            db.close()
    finally:
        _cycle_lock.release()


def _safe_pass() -> None:
    try:
        run_one_pass(wait=True)
    except Exception as exc:  # noqa: BLE001
        _state["last_error"] = f"{type(exc).__name__}: {exc}"
        print(f"[fill-reconciler] pass error: {exc}")
        traceback.print_exc()


def _loop(interval: int, delay: int) -> None:
    time.sleep(delay)
    while True:
        _safe_pass()
        time.sleep(interval)


def start() -> bool:
    cfg = get_config()
    if not cfg["enabled"]:
        print("[fill-reconciler] disabled (LIVE_FILL_RECONCILER_ENABLED is false)")
        return False
    with _start_lock:
        if _state["started"]:
            return False
        _state["started"] = True
        t = threading.Thread(target=_loop, name="fill-reconciler",
                             args=(cfg["interval_seconds"], cfg["startup_delay_seconds"]), daemon=True)
        _state["thread"] = t
        t.start()
        print(f"[fill-reconciler] started (interval={cfg['interval_seconds']}s)")
        return True


def status() -> dict:
    cfg = get_config()
    t = _state["thread"]
    return {
        "enabled": cfg["enabled"],
        "running": bool(_state["started"] and t is not None and t.is_alive()),
        "interval_seconds": cfg["interval_seconds"],
        "last_run_at": _state["last_run_at"].isoformat() if _state["last_run_at"] else None,
        "last_reconciled": _state["last_reconciled"], "last_error": _state["last_error"],
    }
