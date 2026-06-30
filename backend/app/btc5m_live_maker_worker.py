"""BTC 5M live-maker worker — drives run_cycle during an armed LIVE session.

Extra-safe: the thread only STARTS if BTC5M_LIVE_MAKER_ENABLED=true, and even then it
no-ops every cycle unless a session is armed. So a normal deploy (ENABLED=false) runs
NO live-maker thread at all. Shadow dry-runs are driven manually via the run-cycle
endpoint and never need this worker.

Config (env):
  BTC5M_LIVE_MAKER_ENABLED=false           # master switch (off => no thread, no live path)
  BTC5M_LIVE_MAKER_POLL_SECONDS=2          # fast poll for latency/fill measurement
  BTC5M_LIVE_MAKER_WORKER_STARTUP_DELAY=10
"""
from __future__ import annotations

import os
import threading
import time
import traceback
from datetime import datetime

_cycle_lock = threading.Lock()
_start_lock = threading.Lock()
_state = {"started": False, "thread": None, "last_cycle_at": None, "last_error": None, "last_result": None}


def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def get_config() -> dict:
    return {"enabled": _truthy(os.getenv("BTC5M_LIVE_MAKER_ENABLED", "false")),
            "poll_seconds": max(1, int(os.getenv("BTC5M_LIVE_MAKER_POLL_SECONDS", "2"))),
            "startup_delay_seconds": max(0, int(os.getenv("BTC5M_LIVE_MAKER_WORKER_STARTUP_DELAY", "10")))}


def run_one_cycle(wait: bool = True) -> dict:
    if not _cycle_lock.acquire(blocking=wait):
        return {"skipped": "a cycle is already running"}
    try:
        from . import btc5m_live_maker as maker
        from .db import session_scope
        db = session_scope()
        try:
            r = maker.run_cycle(db)
            _state["last_cycle_at"] = datetime.utcnow()
            _state["last_error"] = None
            _state["last_result"] = r.get("skipped") or f"posted={bool(r.get('posted'))} reconciled={r.get('reconciled')}"
            return r
        finally:
            db.close()
    finally:
        _cycle_lock.release()


def _safe_cycle() -> None:
    try:
        run_one_cycle(wait=True)
    except Exception as exc:  # noqa: BLE001  (never let the loop die; fail closed elsewhere)
        _state["last_error"] = f"{type(exc).__name__}: {exc}"
        print(f"[btc5m-live-maker-worker] cycle error: {exc}")
        traceback.print_exc()


def _loop(poll: int, delay: int) -> None:
    time.sleep(delay)
    while True:
        _safe_cycle()
        time.sleep(poll)


def start() -> bool:
    cfg = get_config()
    if not cfg["enabled"]:
        print("[btc5m-live-maker-worker] disabled (BTC5M_LIVE_MAKER_ENABLED is false) — no live thread")
        return False
    with _start_lock:
        if _state["started"]:
            return False
        _state["started"] = True
        t = threading.Thread(target=_loop, name="btc5m-live-maker",
                             args=(cfg["poll_seconds"], cfg["startup_delay_seconds"]), daemon=True)
        _state["thread"] = t
        t.start()
        print(f"[btc5m-live-maker-worker] started (poll={cfg['poll_seconds']}s) — no-op unless a session is armed")
        return True


def is_running() -> bool:
    t = _state["thread"]
    return bool(_state["started"] and t is not None and t.is_alive())


def status() -> dict:
    cfg = get_config()
    return {"worker_running": is_running(), "worker_enabled": cfg["enabled"], "poll_seconds": cfg["poll_seconds"],
            "last_cycle_at": _state["last_cycle_at"].isoformat() if _state["last_cycle_at"] else None,
            "last_result": _state["last_result"], "last_error": _state["last_error"]}
