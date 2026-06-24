"""
Application configuration.

These are *infrastructure* defaults (where the DB lives, polling cadence,
default strategy parameters). The user-tunable runtime knobs (bankroll, min
score, slippage, etc.) live in the `settings` DB table and are editable from
the dashboard Settings page. See `models.Setting` and `services.get_settings`.
"""
from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PCL_", env_file=".env", extra="ignore")

    # Database
    database_url: str = f"sqlite:///{BASE_DIR / 'polymarket_copy_lab.db'}"

    # CORS / frontend
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # Polymarket public endpoints (no auth / no paid keys).
    # NOTE: These are best-effort public hosts. If the live shape changes,
    # adjust `polymarket_client.py`. The app ships in mock mode so these are
    # only used when data_mode == "live".
    gamma_api_base: str = "https://gamma-api.polymarket.com"   # markets metadata
    data_api_base: str = "https://data-api.polymarket.com"     # trades / activity
    clob_api_base: str = "https://clob.polymarket.com"         # prices / books

    # HTTP
    http_timeout_seconds: float = 15.0

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


config = AppConfig()


# ----------------------------------------------------------------------------
# Default *runtime* strategy settings (seeded into the DB on first run).
# Editable later via PATCH /api/settings.
# ----------------------------------------------------------------------------
DEFAULT_SETTINGS: dict[str, object] = {
    "bankroll": 10_000.0,            # starting paper bankroll in USD
    "min_wallet_score": 65.0,        # only copy wallets scoring >= this
    "min_trade_count": 20,           # wallet must have at least this many trades
    "min_trade_size": 50.0,          # ignore signals smaller than this (USD)
    "max_position_pct": 1.0,         # max single position as % of bankroll
    "max_market_exposure_pct": 5.0,  # max total exposure per market as % of bankroll
    "slippage_cents": 1.5,           # simulated slippage in cents (price 0..1)
    "min_market_liquidity": 1000.0,  # market must have at least this much liquidity
    "max_price_staleness_min": 120,  # ignore signals on prices older than this
    "min_confidence": 50.0,          # only open positions for signals above this
    "min_volume": 0.0,               # market must have at least this much volume
    "min_edge": 0.0,                 # min estimated edge (P(win) - price) to copy
    "polling_interval_seconds": 30,  # worker loop cadence
    "data_mode": "mock",             # "mock" or "live"
    # --- risk controls ---
    "max_daily_loss": 500.0,             # stop opening once today's realized loss exceeds this
    "max_open_positions": 50,            # hard cap on concurrent open positions
    "max_correlated_exposure_pct": 15.0, # max open exposure per category (% of bankroll)
    "cooldown_losses": 3,                # consecutive losing closes that trigger a cooldown
    "cooldown_minutes": 30,              # how long to pause new entries after the streak
    # --- wallet discovery ---
    "auto_discovery_enabled": 0,             # 1 = worker discovers wallets automatically
    "discovery_interval_minutes": 15,        # min minutes between auto-discovery runs
    "max_wallets_to_backfill_per_cycle": 5,  # cap live backfills per discovery run
    "min_candidate_trade_count": 15,         # ignore wallets with fewer trades
    "min_candidate_notional": 25.0,          # ignore wallets whose avg trade is below this
}

# Settings that must stay integers when written back from the API.
INT_SETTING_KEYS = {
    "min_trade_count", "max_price_staleness_min", "polling_interval_seconds",
    "max_open_positions", "cooldown_losses", "cooldown_minutes",
    "auto_discovery_enabled", "discovery_interval_minutes",
    "max_wallets_to_backfill_per_cycle", "min_candidate_trade_count",
}
STR_SETTING_KEYS = {"data_mode"}
