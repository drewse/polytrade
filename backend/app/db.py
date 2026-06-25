"""SQLAlchemy engine / session setup for SQLite."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .settings import config


class Base(DeclarativeBase):
    pass


# `check_same_thread=False` is required because the FastAPI app and the
# background worker may touch the DB from different threads.
engine = create_engine(
    config.database_url,
    connect_args={"check_same_thread": False},
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


# Columns added after v1 shipped. SQLite has no full migration tool here, so we
# additively ALTER TABLE for any missing column (data is preserved; reseed not
# required). New *tables* are handled by create_all.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "markets": {
        "resolved_at": "DATETIME",
        "token_ids": "JSON",
        "best_bid": "FLOAT",
        "best_ask": "FLOAT",
    },
    "wallet_stats": {
        "partial_history": "BOOLEAN DEFAULT 0",
        "num_settled": "INTEGER DEFAULT 0",
    },
    "top20_strategies": {
        "exit_policy": "VARCHAR DEFAULT 'hold'",
        "philosophy": "VARCHAR DEFAULT 'mixed'",
        "metrics": "JSON",
        "version": "INTEGER DEFAULT 1",
        "parent_key": "VARCHAR",
        "status": "VARCHAR DEFAULT 'production'",
        "notes": "TEXT DEFAULT ''",
        "param_hash": "VARCHAR DEFAULT ''",
    },
    "top20_trades": {
        "entry_confidence": "FLOAT DEFAULT 0",
        "entry_edge": "FLOAT DEFAULT 0",
        "wallet_rank": "INTEGER",
        "holding_minutes": "FLOAT",
        "exit_reason": "VARCHAR",
        "explanation": "JSON",
    },
    "ingest_status": {"last_discovery_at": "DATETIME"},
    "paper_signals": {
        "edge_estimate": "FLOAT DEFAULT 0.0",
        "move_5m": "FLOAT", "move_30m": "FLOAT", "move_2h": "FLOAT",
        "move_close": "FLOAT", "mfe": "FLOAT", "mae": "FLOAT",
        "quality_updated_at": "DATETIME",
    },
}


def _auto_migrate() -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, cols in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue
            have = {c["name"] for c in inspector.get_columns(table)}
            for col, ddl in cols.items():
                if col not in have:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))


def init_db() -> None:
    """Create all tables, then additively migrate columns. Safe to call repeatedly."""
    from . import models  # noqa: F401  (ensure models are registered)

    Base.metadata.create_all(bind=engine)
    _auto_migrate()


def get_db() -> Iterator[Session]:
    """FastAPI dependency that yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def session_scope() -> Session:
    """Return a raw session for use in scripts/worker (caller manages lifecycle)."""
    return SessionLocal()
