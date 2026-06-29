"""BTC 5M Passive-Maker PAPER worker — research/paper ONLY.

A separate daemon thread (same pattern as the other research workers) that drives the
forward paper-collection loop. INERT unless BTC_PASSIVE_MAKER_PAPER_ENABLED=true.
It only calls the paper harness, which simulates quotes/fills from the historical
trade stream and writes btc5m_paper_* rows. It NEVER places orders or touches live
execution / bankroll / copy trading — there is no live path anywhere in this module.

Config (env):
  BTC_PASSIVE_MAKER_PAPER_ENABLED=false      # master switch (off => no thread)
  BTC_PASSIVE_MAKER_POLL_SECONDS=900
  BTC_PASSIVE_MAKER_STARTUP_DELAY=30
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
    return {"enabled": _truthy(os.getenv("BTC_PASSIVE_MAKER_PAPER_ENABLED", "false")),
            "poll_seconds": max(30, int(os.getenv("BTC_PASSIVE_MAKER_POLL_SECONDS", "900"))),
            "startup_delay_seconds": max(0, int(os.getenv("BTC_PASSIVE_MAKER_STARTUP_DELAY", "30")))}


def run_one_cycle(wait: bool = True) -> dict:
    if not _cycle_lock.acquire(blocking=wait):
        return {"skipped": "a paper-maker cycle is already running"}
    try:
        from . import btc5m_passive_maker as harness
        from .db import session_scope
        db = session_scope()
        try:
            result = harness.run_once(db)
            _state["last_cycle_at"] = datetime.utcnow()
            _state["last_error"] = None
            _state["last_result"] = result.get("skipped") or f"created={result.get('created')} filled={result.get('filled')}"
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
        print(f"[btc5m-passive-maker-worker] cycle error: {exc}")
        traceback.print_exc()


def _loop(poll: int, delay: int) -> None:
    time.sleep(delay)
    while True:
        _safe_cycle()
        time.sleep(poll)


def start() -> bool:
    cfg = get_config()
    if not cfg["enabled"]:
        print("[btc5m-passive-maker-worker] disabled (BTC_PASSIVE_MAKER_PAPER_ENABLED is false)")
        return False
    with _start_lock:
        if _state["started"]:
            return False
        _state["started"] = True
        t = threading.Thread(target=_loop, name="btc5m-passive-maker",
                             args=(cfg["poll_seconds"], cfg["startup_delay_seconds"]), daemon=True)
        _state["thread"] = t
        t.start()
        print(f"[btc5m-passive-maker-worker] started (poll={cfg['poll_seconds']}s) — PAPER only, no live path")
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
