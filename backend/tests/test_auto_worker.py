"""Tests for the in-process auto-ingest worker."""
from __future__ import annotations

import time

import pytest

from app import auto_worker


@pytest.fixture(autouse=True)
def _reset_worker_state():
    """Each test starts from a clean worker state (module-level singleton)."""
    auto_worker._state.update(started=False, thread=None, last_cycle_at=None, last_error=None)
    yield
    auto_worker._state.update(started=False, thread=None, last_cycle_at=None, last_error=None)


def test_does_not_start_when_disabled(monkeypatch):
    monkeypatch.setenv("AUTO_INGEST_ENABLED", "false")
    assert auto_worker.start() is False
    assert auto_worker.is_running() is False


def test_starts_when_enabled(monkeypatch):
    monkeypatch.setenv("AUTO_INGEST_ENABLED", "true")
    # huge startup delay so the loop sleeps and never runs a real cycle in-test
    monkeypatch.setenv("AUTO_INGEST_STARTUP_DELAY_SECONDS", "999")
    assert auto_worker.start() is True
    assert auto_worker.is_running() is True


def test_duplicate_start_prevented(monkeypatch):
    monkeypatch.setenv("AUTO_INGEST_ENABLED", "true")
    monkeypatch.setenv("AUTO_INGEST_STARTUP_DELAY_SECONDS", "999")
    assert auto_worker.start() is True       # first start
    t1 = auto_worker._state["thread"]
    assert auto_worker.start() is False      # no duplicate loop
    assert auto_worker._state["thread"] is t1  # no new thread was spawned


def test_failed_cycle_is_logged_and_loop_continues(monkeypatch):
    def boom(wait=True):
        raise RuntimeError("ingest blew up")
    monkeypatch.setattr(auto_worker, "run_one_cycle", boom)
    # _safe_cycle must swallow the error (the loop body) and record it
    auto_worker._safe_cycle()
    assert auto_worker._state["last_error"] is not None
    assert "ingest blew up" in auto_worker._state["last_error"]
    # calling again still does not raise -> loop would continue
    auto_worker._safe_cycle()


def test_run_one_cycle_delegates_to_paper_ingest(monkeypatch):
    """The worker adds NO trading logic — it only drives the existing paper
    ingest cycle. (paper-only guarantee.)"""
    calls = {"n": 0}

    class _FakeSession:
        def close(self):
            pass

    def fake_cycle(db):
        calls["n"] += 1
        return {"ok": True, "data_mode": "live"}

    monkeypatch.setattr("app.services.run_ingest_cycle", fake_cycle)
    monkeypatch.setattr("app.db.session_scope", lambda: _FakeSession())
    result = auto_worker.run_one_cycle(wait=True)
    assert calls["n"] == 1 and result["ok"] is True
    assert auto_worker._state["last_cycle_at"] is not None
    assert auto_worker._state["last_error"] is None


def test_only_one_cycle_at_a_time(monkeypatch):
    # if the lock is held, a non-blocking run is skipped (no overlap)
    auto_worker._cycle_lock.acquire()
    try:
        out = auto_worker.run_one_cycle(wait=False)
        assert "skipped" in out
    finally:
        auto_worker._cycle_lock.release()


def test_status_exposes_worker_state(monkeypatch):
    monkeypatch.setenv("AUTO_INGEST_ENABLED", "true")
    monkeypatch.setenv("AUTO_INGEST_INTERVAL_SECONDS", "30")
    s = auto_worker.status()
    for key in ("auto_ingest_enabled", "auto_ingest_interval_seconds", "worker_running",
                "last_worker_error", "last_worker_cycle_at"):
        assert key in s
    assert s["auto_ingest_enabled"] is True
    assert s["auto_ingest_interval_seconds"] == 30


def test_config_defaults_are_production_safe(monkeypatch):
    for v in ("AUTO_INGEST_ENABLED", "AUTO_INGEST_INTERVAL_SECONDS", "AUTO_INGEST_STARTUP_DELAY_SECONDS"):
        monkeypatch.delenv(v, raising=False)
    cfg = auto_worker.get_config()
    assert cfg["enabled"] is True                 # on by default
    assert cfg["interval_seconds"] == 30
    assert cfg["startup_delay_seconds"] == 5
