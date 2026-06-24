"""
Background polling worker.

Runs a loop that, every `polling_interval_seconds`:
  1. fetches recent trades (mock or live, per Settings)
  2. updates wallet stats for wallets that traded
  3. creates copy-trade signals
  4. opens paper positions when strategy rules pass
  5. marks open positions to market / auto-closes resolved markets
  6. records an equity snapshot

On first run it seeds the mock world if the DB is empty so you get an
immediately-populated dashboard.

Run with:   python -m app.worker
"""
from __future__ import annotations

import time

from sqlalchemy import func, select

from . import services
from .db import init_db, session_scope
from .models import Wallet


def _seed_if_empty() -> None:
    db = session_scope()
    try:
        services.ensure_settings(db)
        count = db.scalar(select(func.count()).select_from(Wallet))
        if not count:
            print("[worker] empty DB -> seeding mock world ...")
            result = services.seed_mock_data(db)
            print(f"[worker] seeded: {result}")
    finally:
        db.close()


def run_once() -> dict:
    db = session_scope()
    try:
        return services.run_ingest_cycle(db)
    finally:
        db.close()


def main() -> None:
    init_db()
    _seed_if_empty()
    print("[worker] starting polling loop. Ctrl-C to stop.")
    while True:
        db = session_scope()
        try:
            interval = int(services.get_settings(db)["polling_interval_seconds"])
        finally:
            db.close()
        try:
            result = run_once()
            print(f"[worker] cycle: {result}")
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            print(f"[worker] cycle error: {exc}")
        time.sleep(max(5, interval))


if __name__ == "__main__":
    main()
