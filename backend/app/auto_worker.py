"""
In-process auto-ingest worker (single-service Railway deployment).

Runs `services.run_ingest_cycle` on a fixed interval in a daemon thread started
at FastAPI startup, so live data refreshes itself without manual /api/ingest/run.
Guarantees:
  * one loop only (duplicate-start guarded),
  * one ingest cycle at a time (shared lock with the manual endpoint),
  * a failed cycle is logged and the loop continues,
  * never blocks the API (background daemon thread).

STRICTLY PAPER ONLY — this only drives the existing paper ingest cycle. No
orders, keys, signing, or exchange connectivity.

Config (env vars, production-safe defaults):
  AUTO_INGEST_ENABLED=true
  AUTO_INGEST_INTERVAL_SECONDS=30
  AUTO_INGEST_STARTUP_DELAY_SECONDS=5
"""
from __future__ import annotations

import os
import threading
import time
import traceback
from datetime import datetime

# Module-level state (single process).
_cycle_lock = threading.Lock()     # ensures ONE ingest cycle at a time
_start_lock = threading.Lock()     # ensures ONE loop is ever started
_state = {
    "started": False,
    "thread": None,
    "last_cycle_at": None,   # datetime of last successful cycle
    "last_error": None,      # str of last cycle error
}


def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def get_config() -> dict:
    return {
        "enabled": _truthy(os.getenv("AUTO_INGEST_ENABLED", "true")),
        "interval_seconds": max(5, int(os.getenv("AUTO_INGEST_INTERVAL_SECONDS", "30"))),
        "startup_delay_seconds": max(0, int(os.getenv("AUTO_INGEST_STARTUP_DELAY_SECONDS", "5"))),
    }


def run_one_cycle(wait: bool = True) -> dict:
    """Run a single ingest cycle under the shared lock. `wait=False` returns a
    'busy' marker instead of blocking when a cycle is already running."""
    acquired = _cycle_lock.acquire(blocking=wait)
    if not acquired:
        return {"skipped": "an ingest cycle is already running"}
    try:
        from . import services
        from .db import session_scope
        db = session_scope()
        try:
            result = services.run_ingest_cycle(db)
            _state["last_cycle_at"] = datetime.utcnow()
            _state["last_error"] = None
            return result
        finally:
            db.close()
    finally:
        _cycle_lock.release()


def _safe_cycle() -> None:
    """Run one cycle, swallowing+logging any error so the loop never dies."""
    try:
        run_one_cycle(wait=True)
    except Exception as exc:  # noqa: BLE001
        _state["last_error"] = f"{type(exc).__name__}: {exc}"
        print(f"[auto-worker] cycle error: {exc}")
        traceback.print_exc()


def _loop(interval: int, delay: int) -> None:
    time.sleep(delay)
    while True:
        _safe_cycle()
        time.sleep(interval)


def start() -> bool:
    """Start the loop once. Returns True if started, False if disabled or already
    running (duplicate-start guard)."""
    cfg = get_config()
    if not cfg["enabled"]:
        print("[auto-worker] disabled (AUTO_INGEST_ENABLED is false)")
        return False
    with _start_lock:
        if _state["started"]:
            return False
        _state["started"] = True
        t = threading.Thread(target=_loop, name="auto-ingest",
                             args=(cfg["interval_seconds"], cfg["startup_delay_seconds"]),
                             daemon=True)
        _state["thread"] = t
        t.start()
        print(f"[auto-worker] started (interval={cfg['interval_seconds']}s, "
              f"delay={cfg['startup_delay_seconds']}s)")
        return True


def is_running() -> bool:
    t = _state["thread"]
    return bool(_state["started"] and t is not None and t.is_alive())


def status() -> dict:
    cfg = get_config()
    return {
        "auto_ingest_enabled": cfg["enabled"],
        "auto_ingest_interval_seconds": cfg["interval_seconds"],
        "worker_running": is_running(),
        "last_worker_error": _state["last_error"],
        "last_worker_cycle_at": _state["last_cycle_at"],
    }
