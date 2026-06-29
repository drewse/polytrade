"""BTC Passive-Maker FORWARD pipeline worker — research/paper ONLY.

Daemon thread (same pattern as the other research workers) that drives the forward
conversion pipeline (index → build → quote → settle) on an interval. INERT unless
BTC_PASSIVE_MAKER_FORWARD_ENABLED=true. It only calls the forward research engine,
which writes btc5m_* rows and places NO orders — no live path exists here.

Config (env):
  BTC_PASSIVE_MAKER_FORWARD_ENABLED=false    # master switch (off => no thread)
  BTC_PASSIVE_MAKER_FORWARD_POLL_SECONDS=600
  BTC_PASSIVE_MAKER_FORWARD_STARTUP_DELAY=45
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
    return {"enabled": _truthy(os.getenv("BTC_PASSIVE_MAKER_FORWARD_ENABLED", "false")),
            "poll_seconds": max(60, int(os.getenv("BTC_PASSIVE_MAKER_FORWARD_POLL_SECONDS", "600"))),
            "startup_delay_seconds": max(0, int(os.getenv("BTC_PASSIVE_MAKER_FORWARD_STARTUP_DELAY", "45")))}


def run_one_cycle(wait: bool = True) -> dict:
    if not _cycle_lock.acquire(blocking=wait):
        return {"skipped": "a forward cycle is already running"}
    try:
        from . import btc5m_passive_maker_forward as fwd
        from .db import session_scope
        db = session_scope()
        try:
            result = fwd.run_forward_cycle(db)
            _state["last_cycle_at"] = datetime.utcnow()
            _state["last_error"] = None
            _state["last_result"] = result.get("skipped") or (
                f"idx+{result.get('new_indexed')} pts+{result.get('new_points_markets')} "
                f"q+{result.get('new_quotes')} f+{result.get('new_fills')}")
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
        print(f"[btc5m-passive-maker-forward-worker] cycle error: {exc}")
        traceback.print_exc()


def _loop(poll: int, delay: int) -> None:
    time.sleep(delay)
    while True:
        _safe_cycle()
        time.sleep(poll)


def start() -> bool:
    cfg = get_config()
    if not cfg["enabled"]:
        print("[btc5m-passive-maker-forward-worker] disabled (BTC_PASSIVE_MAKER_FORWARD_ENABLED is false)")
        return False
    with _start_lock:
        if _state["started"]:
            return False
        _state["started"] = True
        t = threading.Thread(target=_loop, name="btc5m-passive-maker-forward",
                             args=(cfg["poll_seconds"], cfg["startup_delay_seconds"]), daemon=True)
        _state["thread"] = t
        t.start()
        print(f"[btc5m-passive-maker-forward-worker] started (poll={cfg['poll_seconds']}s) — PAPER only, no live path")
        return True


def is_running() -> bool:
    t = _state["thread"]
    return bool(_state["started"] and t is not None and t.is_alive())


def status() -> dict:
    cfg = get_config()
    return {"worker_running": is_running(), "worker_enabled": cfg["enabled"],
            "poll_seconds": cfg["poll_seconds"],
            "last_cycle_at": _state["last_cycle_at"].isoformat() if _state["last_cycle_at"] else None,
            "last_result": _state["last_result"], "last_error": _state["last_error"],
            "safety": "research/paper only — never trades"}
