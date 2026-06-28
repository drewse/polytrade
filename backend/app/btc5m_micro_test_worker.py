"""Dedicated BTC 5M Micro-Test background worker.

A separate daemon thread (same pattern as auto_worker) that drives the low-latency
micro-test poll loop. It is INERT unless BOTH:
  * BTC5M_MICRO_TEST_ENABLED=true, and
  * the micro-test is armed.

It NEVER touches the production ingest/live workers. By default it runs PAPER
cycles only (records detected/perfect/actual price-drift twins for latency
measurement) and places NO real orders. Live placement requires an explicit,
separate opt-in (BTC5M_MICRO_TEST_WORKER_PLACE_LIVE=true) — so enabling the
worker can never by itself start real trading.

Config (env):
  BTC5M_MICRO_TEST_POLL_SECONDS=3            # poll cadence (low-latency detection)
  BTC5M_MICRO_TEST_WORKER_STARTUP_DELAY=5
  BTC5M_MICRO_TEST_WORKER_PLACE_LIVE=false   # default paper; true => may place real orders when armed
"""
from __future__ import annotations

import os
import threading
import time
import traceback
from datetime import datetime

_cycle_lock = threading.Lock()
_start_lock = threading.Lock()
_state = {
    "started": False, "thread": None, "last_cycle_at": None,
    "last_error": None, "last_result": None,
}


def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def get_config() -> dict:
    return {
        "enabled": _truthy(os.getenv("BTC5M_MICRO_TEST_ENABLED", "false")),
        "poll_seconds": max(1, int(os.getenv("BTC5M_MICRO_TEST_POLL_SECONDS", "3"))),
        "startup_delay_seconds": max(0, int(os.getenv("BTC5M_MICRO_TEST_WORKER_STARTUP_DELAY", "5"))),
        "place_live": _truthy(os.getenv("BTC5M_MICRO_TEST_WORKER_PLACE_LIVE", "false")),
    }


def run_one_cycle(wait: bool = True) -> dict:
    """Settle resolved test positions, then run a single micro-test cycle. Returns
    the cycle result (a no-op dict when disabled/disarmed). Paper unless the
    explicit live-place opt-in is set."""
    if not _cycle_lock.acquire(blocking=wait):
        return {"skipped": "a micro-test cycle is already running"}
    try:
        from . import btc5m_micro_test as umt
        from .db import session_scope
        db = session_scope()
        try:
            umt.settle(db)
            cfg = get_config()
            result = umt.run_once(db, place=cfg["place_live"])
            _state["last_cycle_at"] = datetime.utcnow()
            _state["last_error"] = None
            _state["last_result"] = result.get("reason") or (
                f"{result.get('mode')}:{'placed' if result.get('placed') else 'recorded'}"
                if result.get("ran") else "no-op")
            return result
        finally:
            db.close()
    finally:
        _cycle_lock.release()


def _safe_cycle() -> None:
    try:
        run_one_cycle(wait=True)
    except Exception as exc:  # noqa: BLE001  (never let the loop die)
        _state["last_error"] = f"{type(exc).__name__}: {exc}"
        print(f"[btc5m-micro-test-worker] cycle error: {exc}")
        traceback.print_exc()


def _loop(poll: int, delay: int) -> None:
    time.sleep(delay)
    while True:
        _safe_cycle()
        time.sleep(poll)


def start() -> bool:
    """Start the loop once. Returns False if disabled (no thread) or already
    running. The loop itself no-ops while the micro-test is disarmed."""
    cfg = get_config()
    if not cfg["enabled"]:
        print("[btc5m-micro-test-worker] disabled (BTC5M_MICRO_TEST_ENABLED is false)")
        return False
    with _start_lock:
        if _state["started"]:
            return False
        _state["started"] = True
        t = threading.Thread(target=_loop, name="btc5m-micro-test",
                             args=(cfg["poll_seconds"], cfg["startup_delay_seconds"]), daemon=True)
        _state["thread"] = t
        t.start()
        print(f"[btc5m-micro-test-worker] started (poll={cfg['poll_seconds']}s, "
              f"place_live={cfg['place_live']})")
        return True


def is_running() -> bool:
    t = _state["thread"]
    return bool(_state["started"] and t is not None and t.is_alive())


def status() -> dict:
    cfg = get_config()
    return {
        "worker_running": is_running(),
        "worker_enabled": cfg["enabled"],
        "poll_seconds": cfg["poll_seconds"],
        "place_live": cfg["place_live"],
        "last_cycle_at": _state["last_cycle_at"].isoformat() if _state["last_cycle_at"] else None,
        "last_result": _state["last_result"],
        "last_error": _state["last_error"],
    }
