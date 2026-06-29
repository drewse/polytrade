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


# With a continuous background ingest worker writing while API requests read,
# use WAL + a busy timeout so readers don't hit "database is locked". SQLite-only.
if config.database_url.startswith("sqlite"):
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover (driver-level)
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

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
        "profit_factor": "FLOAT DEFAULT 0",
        "expectancy": "FLOAT DEFAULT 0",
        "sharpe": "FLOAT DEFAULT 0",
        "max_drawdown": "FLOAT DEFAULT 0",
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
        "realistic_metrics": "JSON",
    },
    "top20_trades": {
        "entry_confidence": "FLOAT DEFAULT 0",
        "entry_edge": "FLOAT DEFAULT 0",
        "wallet_rank": "INTEGER",
        "holding_minutes": "FLOAT",
        "exit_reason": "VARCHAR",
        "explanation": "JSON",
        "source": "VARCHAR DEFAULT 'live'",
    },
    "top20_feature_vectors": {
        "source": "VARCHAR DEFAULT 'live'",
    },
    "live_executions": {
        "limit_price": "FLOAT",
        "order_id": "VARCHAR",
        "fill_outcome": "VARCHAR",          # filled|partially_filled_cancelled|unfilled_cancelled|submit_error|cancel_error|simulated
        "venue_error": "TEXT",              # FULL untruncated venue/PolyApiException text
        "requested_size_usd": "FLOAT",      # intended stake (size_usd holds the FILLED amount)
        "tick_size": "FLOAT",               # venue book tick_size used for the decision
        "min_order_size": "FLOAT",          # venue book min_order_size (shares) used
        "sizing_detail": "JSON",            # dynamic risk-aware sizing breakdown
        "fill_source": "VARCHAR",           # exact|venue|pending|simulated|estimate
        "fill_pending_reconciliation": "BOOLEAN DEFAULT 0",
        "reconciled_at": "DATETIME",
    },
    "discovery_sources": {
        "backfill_status": "VARCHAR DEFAULT 'pending'",
        "last_backfill_attempt_at": "DATETIME",
        "backfill_completed_at": "DATETIME",
        "backfill_error": "TEXT",
        "trades_imported": "INTEGER DEFAULT 0",
        "stats_updated": "BOOLEAN DEFAULT 0",
    },
    "ingest_status": {"last_discovery_at": "DATETIME"},
    # BTC 5M on-chain detector read-only diagnostics added after the table shipped.
    "btc5m_onchain_state": {
        "blocks_scanned": "BIGINT DEFAULT 0",
        "logs_scanned": "BIGINT DEFAULT 0",
        "events_decoded": "BIGINT DEFAULT 0",
        "events_watched": "BIGINT DEFAULT 0",
        "btc_matches": "BIGINT DEFAULT 0",
        "ignored_by_reason": "JSON",
        "error_count": "INTEGER DEFAULT 0",
        "last_orderfilled_at": "DATETIME",
        "last_orderfilled_desc": "TEXT",
        "last_watched_event_at": "DATETIME",
        "last_watched_desc": "TEXT",
        "last_btc_event_at": "DATETIME",
        "last_btc_desc": "TEXT",
        "token_map_refreshed_at": "DATETIME",
        "token_map_error": "TEXT",
        "token_map_pages": "INTEGER DEFAULT 0",
        "token_map_open": "INTEGER DEFAULT 0",
        "token_map_closed": "INTEGER DEFAULT 0",
        "unmapped_tokens": "JSON",
        "decoded_by_signature": "JSON",
        "unknown_topic0_count": "BIGINT DEFAULT 0",
        "last_decoded_signature": "TEXT",
        "last_decode_error": "TEXT",
    },
    # BTC 5M Micro-Test V2 — latency instrumentation + price-drift columns added
    # after the V1 table shipped (ALTER preserves the existing empty table).
    "btc5m_micro_test_trades": {
        "signal_source": "VARCHAR",
        "wallet_trade_at": "DATETIME",
        "detected_at": "DATETIME",
        "submitted_at": "DATETIME",
        "venue_ack_at": "DATETIME",
        "filled_at": "DATETIME",
        "detection_latency_s": "FLOAT",
        "execution_latency_s": "FLOAT",
        "fill_latency_s": "FLOAT",
        "total_latency_s": "FLOAT",
        "wallet_entry_price": "FLOAT",
        "detected_price": "FLOAT",
        "missed_edge": "FLOAT",
        "latency_cost": "FLOAT",
    },
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
