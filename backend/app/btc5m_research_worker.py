"""Nightly BTC 5M Alpha Research worker.

A separate daemon thread (same pattern as the micro-test worker) that runs the
quant research pipeline once per interval: ingest/rebuild the dataset, regenerate
features, retrain the fair-value + ensemble models, run the evolutionary search,
detect model decay, and write a nightly research report.

It is INERT unless BTC5M_RESEARCH_ENABLED=true. It is research/paper ONLY by
construction — the research module estimates probabilities and writes only
btc5m_lab_* / btc5m_research_* rows. It NEVER places orders or touches live
execution / sizing / bankroll / copy ranking, and there is no live-trading switch
anywhere in this path.

Config (env):
  BTC5M_RESEARCH_ENABLED=false           # master switch (off => no thread)
  BTC5M_RESEARCH_INTERVAL_HOURS=24       # cadence (nightly)
  BTC5M_RESEARCH_POLL_SECONDS=900        # how often the loop checks if it's due
  BTC5M_RESEARCH_STARTUP_DELAY=60
  BTC5M_RESEARCH_LIMIT_MARKETS=80        # markets to rebuild each run
  BTC5M_RESEARCH_RUN_ON_START=false      # run once shortly after boot
"""
from __future__ import annotations

import os
import threading
import time
import traceback
from datetime import datetime, timedelta

_cycle_lock = threading.Lock()
_start_lock = threading.Lock()
_state = {
    "started": False, "thread": None, "last_run_at": None,
    "last_error": None, "last_verdict": None, "runs": 0,
}


def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def get_config() -> dict:
    return {
        "enabled": _truthy(os.getenv("BTC5M_RESEARCH_ENABLED", "false")),
        "interval_hours": max(1, int(os.getenv("BTC5M_RESEARCH_INTERVAL_HOURS", "24"))),
        "poll_seconds": max(30, int(os.getenv("BTC5M_RESEARCH_POLL_SECONDS", "900"))),
        "startup_delay_seconds": max(0, int(os.getenv("BTC5M_RESEARCH_STARTUP_DELAY", "60"))),
        "limit_markets": max(10, int(os.getenv("BTC5M_RESEARCH_LIMIT_MARKETS", "80"))),
        "run_on_start": _truthy(os.getenv("BTC5M_RESEARCH_RUN_ON_START", "false")),
    }


def run_pipeline_once(*, build: bool = True, wait: bool = True) -> dict:
    """Run the research pipeline once (rebuild + models + report). Paper/research
    only — never trades. Returns the report (or a skip dict)."""
    if not _cycle_lock.acquire(blocking=wait):
        return {"skipped": "a research run is already in progress"}
    try:
        from . import btc5m_alpha_discovery as discovery
        from .db import session_scope
        cfg = get_config()
        db = session_scope()
        try:
            # Full nightly: Phase-1 fair-value/ensemble + Phase-2 alpha discovery
            # (feature mining, registry, meta-learning, cross-asset). Paper only.
            result = discovery.run_nightly(db, build=build, limit_markets=cfg["limit_markets"])
            _state["last_run_at"] = datetime.utcnow()
            _state["last_error"] = None
            disc = result.get("alpha_discovery") or {}
            _state["last_verdict"] = f"gen {result.get('generation')}: {disc.get('verdict')}"
            _state["runs"] += 1
            return result
        finally:
            db.close()
    finally:
        _cycle_lock.release()


def _due() -> bool:
    cfg = get_config()
    last = _state["last_run_at"]
    if last is None:
        return cfg["run_on_start"]
    return datetime.utcnow() - last >= timedelta(hours=cfg["interval_hours"])


def _safe_run() -> None:
    try:
        run_pipeline_once(build=True, wait=True)
    except Exception as exc:  # noqa: BLE001  (never let the loop die)
        _state["last_error"] = f"{type(exc).__name__}: {exc}"
        print(f"[btc5m-research-worker] run error: {exc}")
        traceback.print_exc()


def _loop(poll: int, delay: int) -> None:
    time.sleep(delay)
    while True:
        if _due():
            _safe_run()
        time.sleep(poll)


def start() -> bool:
    cfg = get_config()
    if not cfg["enabled"]:
        print("[btc5m-research-worker] disabled (BTC5M_RESEARCH_ENABLED is false)")
        return False
    with _start_lock:
        if _state["started"]:
            return False
        _state["started"] = True
        t = threading.Thread(target=_loop, name="btc5m-research",
                             args=(cfg["poll_seconds"], cfg["startup_delay_seconds"]), daemon=True)
        _state["thread"] = t
        t.start()
        print(f"[btc5m-research-worker] started (interval={cfg['interval_hours']}h, "
              f"limit_markets={cfg['limit_markets']}, run_on_start={cfg['run_on_start']})")
        return True


def is_running() -> bool:
    t = _state["thread"]
    return bool(_state["started"] and t is not None and t.is_alive())


def status() -> dict:
    cfg = get_config()
    return {
        "worker_running": is_running(),
        "worker_enabled": cfg["enabled"],
        "interval_hours": cfg["interval_hours"],
        "limit_markets": cfg["limit_markets"],
        "runs": _state["runs"],
        "last_run_at": _state["last_run_at"].isoformat() if _state["last_run_at"] else None,
        "last_verdict": _state["last_verdict"],
        "last_error": _state["last_error"],
        "safety": "research/paper only — never trades",
    }
