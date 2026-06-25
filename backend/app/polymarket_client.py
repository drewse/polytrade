"""
Polymarket data client — LIVE, READ-ONLY.

Verified against Polymarket's public, no-auth endpoints (June 2026):

  * Gamma  https://gamma-api.polymarket.com/markets         -> market metadata
  * Data   https://data-api.polymarket.com/trades           -> recent trades / wallet activity
  * CLOB   https://clob.polymarket.com/midpoint|price|book   -> live bid/ask/mid prices

This client ONLY reads public data. It never authenticates, never signs, never
submits orders, and uses no private keys. See `README.md` → Safety.

Parsing notes confirmed from live responses:
  * Gamma `outcomes`, `outcomePrices`, `clobTokenIds` are JSON-ENCODED STRINGS
    (e.g. '["Yes", "No"]'), not arrays — `_maybe_json_list` handles both.
  * Trade `size` is in SHARES; USD notional = size * price.
  * Trade `side` is "BUY"/"SELL" (upper); `timestamp` is unix seconds.
  * A market is resolved when `closed` is true; the winner is the outcome whose
    `outcomePrices` entry is ~1.0 (>= 0.99). If none is, resolution is unknown.
  * `category` lives on the nested `events[0].category` (often null).

Pure `parse_*` functions are exposed for fixture-based testing. They raise
`LiveDataError` on a malformed *response* (wrong top-level type) and
`LiveParseError` on a record missing *required* fields, while tolerating missing
*optional* fields.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

import httpx

from .settings import config

USER_AGENT = "polymarket-copy-lab/1.0 (research; read-only; no-trading)"


class LiveDataError(RuntimeError):
    """Raised when a response is the wrong shape entirely (e.g. not a list)."""


class LiveParseError(ValueError):
    """Raised when a single record is missing required fields."""


# ---------------------------------------------------------------------------
# Normalized DTOs (provider-agnostic — the rest of the app never sees wire JSON)
# ---------------------------------------------------------------------------
@dataclass
class MarketDTO:
    id: str
    question: str
    slug: str | None = None
    category: str | None = None
    outcomes: list[str] = field(default_factory=lambda: ["Yes", "No"])
    prices: list[float] = field(default_factory=lambda: [0.5, 0.5])
    token_ids: list[str] = field(default_factory=list)
    best_bid: float | None = None
    best_ask: float | None = None
    liquidity: float = 0.0
    volume: float = 0.0
    resolved: bool = False
    resolved_outcome: str | None = None
    resolved_at: datetime | None = None

    def price_for(self, outcome: str) -> float | None:
        try:
            return float(self.prices[self.outcomes.index(outcome)])
        except (ValueError, IndexError, TypeError):
            return None


@dataclass
class TradeDTO:
    external_id: str
    wallet_address: str
    market_id: str
    outcome: str
    side: str           # "buy" | "sell"
    price: float        # 0..1
    size: float         # USD notional (shares * price)
    timestamp: datetime
    category: str | None = None
    shares: float | None = None


class DataProvider(Protocol):
    def get_markets(self, limit: int = 100) -> list[MarketDTO]: ...
    def get_recent_trades(self, limit: int = 50) -> list[TradeDTO]: ...
    def get_prices(self, market_ids: list[str]) -> dict[str, list[float]]: ...


# ---------------------------------------------------------------------------
# small parse helpers
# ---------------------------------------------------------------------------
def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _maybe_json_list(value):
    """Accept a real list OR a JSON-encoded string list (Gamma style)."""
    if value is None:
        return None
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else None
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def _to_dt(value) -> datetime:
    try:
        if value is None:
            return datetime.now(timezone.utc)
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError, OSError):
        return datetime.now(timezone.utc)


def _category_from_events(row: dict) -> str | None:
    events = row.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        cat = events[0].get("category")
        if cat:
            return str(cat)
    tags = row.get("tags")
    if isinstance(tags, list) and tags:
        first = tags[0]
        return first.get("label") if isinstance(first, dict) else str(first)
    return None


def resolved_outcome_from_prices(outcomes: list[str], prices: list[float],
                                 threshold: float = 0.99) -> str | None:
    """Winner = the outcome priced ~1.0. None if no clear winner (e.g. pending)."""
    for name, price in zip(outcomes, prices):
        if price >= threshold:
            return name
    return None


# ---------------------------------------------------------------------------
# pure parsers (fixture-testable)
# ---------------------------------------------------------------------------
def parse_market(row: dict) -> MarketDTO:
    if not isinstance(row, dict):
        raise LiveParseError(f"market record is not an object: {type(row).__name__}")
    market_id = row.get("conditionId") or row.get("id") or row.get("slug")
    if not market_id:
        raise LiveParseError("market missing required id (conditionId/id/slug)")
    question = row.get("question") or row.get("title")
    if not question:
        raise LiveParseError(f"market {market_id} missing required 'question'")

    outcomes = _maybe_json_list(row.get("outcomes")) or ["Yes", "No"]
    outcomes = [str(o) for o in outcomes]
    raw_prices = _maybe_json_list(row.get("outcomePrices"))
    prices = [_safe_float(p) for p in raw_prices] if raw_prices else [round(1 / len(outcomes), 4)] * len(outcomes)
    # pad/truncate prices to align with outcomes
    if len(prices) < len(outcomes):
        prices += [0.0] * (len(outcomes) - len(prices))
    prices = prices[: len(outcomes)]

    token_ids = [str(t) for t in (_maybe_json_list(row.get("clobTokenIds")) or [])]
    resolved = bool(row.get("closed") or row.get("resolved"))
    resolved_outcome = resolved_outcome_from_prices(outcomes, prices) if resolved else None
    resolved_at = _to_dt(row.get("updatedAt") or row.get("endDate")) if resolved else None

    return MarketDTO(
        id=str(market_id),
        question=str(question),
        slug=row.get("slug"),
        category=_category_from_events(row),
        outcomes=outcomes,
        prices=prices,
        token_ids=token_ids,
        best_bid=_safe_float(row.get("bestBid"), None) if row.get("bestBid") is not None else None,
        best_ask=_safe_float(row.get("bestAsk"), None) if row.get("bestAsk") is not None else None,
        liquidity=_safe_float(row.get("liquidityNum") or row.get("liquidity")),
        volume=_safe_float(row.get("volumeNum") or row.get("volume")),
        resolved=resolved,
        resolved_outcome=resolved_outcome,
        resolved_at=resolved_at,
    )


def parse_markets(payload) -> list[MarketDTO]:
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise LiveDataError(
            f"markets response should be a list (or {{data: [...]}}); got {type(payload).__name__}"
        )
    out: list[MarketDTO] = []
    for row in rows:
        out.append(parse_market(row))
    return out


def parse_trade(row: dict) -> TradeDTO:
    if not isinstance(row, dict):
        raise LiveParseError(f"trade record is not an object: {type(row).__name__}")
    wallet = row.get("proxyWallet") or row.get("maker") or row.get("user")
    market_id = row.get("conditionId") or row.get("market")
    if not wallet:
        raise LiveParseError("trade missing required wallet (proxyWallet)")
    if not market_id:
        raise LiveParseError("trade missing required conditionId")
    price = _safe_float(row.get("price"))
    shares = _safe_float(row.get("size"))
    tx = row.get("transactionHash") or row.get("id") or row.get("hash") or ""
    asset = row.get("asset") or row.get("outcomeIndex") or ""
    external_id = f"{tx}-{wallet}-{asset}" if tx else f"{wallet}-{asset}-{row.get('timestamp')}"
    return TradeDTO(
        external_id=str(external_id),
        wallet_address=str(wallet),
        market_id=str(market_id),
        outcome=str(row.get("outcome") or "Yes"),
        side=str(row.get("side") or "buy").lower(),
        price=price,
        size=round(shares * price, 4),  # USD notional
        shares=shares,
        timestamp=_to_dt(row.get("timestamp")),
        category=row.get("category"),
    )


def parse_trades(payload) -> list[TradeDTO]:
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise LiveDataError(
            f"trades response should be a list; got {type(payload).__name__}"
        )
    out: list[TradeDTO] = []
    for row in rows:
        out.append(parse_trade(row))
    return out


def parse_midpoint(payload) -> float:
    if not isinstance(payload, dict) or "mid" not in payload:
        raise LiveDataError(f"midpoint response missing 'mid': {payload!r}")
    return _safe_float(payload["mid"])


def parse_orderbook(payload) -> dict:
    """Return {bid, ask, mid} from a CLOB book response (best levels)."""
    if not isinstance(payload, dict) or "bids" not in payload or "asks" not in payload:
        raise LiveDataError(f"orderbook response missing bids/asks: {type(payload).__name__}")
    bids = payload.get("bids") or []
    asks = payload.get("asks") or []
    # CLOB returns bids ascending; best bid is the highest, best ask the lowest.
    best_bid = max((_safe_float(b.get("price")) for b in bids), default=None)
    best_ask = min((_safe_float(a.get("price")) for a in asks), default=None)
    mid = None
    if best_bid is not None and best_ask is not None:
        mid = round((best_bid + best_ask) / 2, 4)
    return {"bid": best_bid, "ask": best_ask, "mid": mid}


# ---------------------------------------------------------------------------
# live HTTP client
# ---------------------------------------------------------------------------
class LivePolymarketClient:
    """Read-only client over Polymarket public APIs. No auth, no keys, no orders."""

    def __init__(self) -> None:
        self._client = httpx.Client(
            timeout=config.http_timeout_seconds,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )

    def _get_json(self, url: str, params: dict | None = None):
        resp = self._client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    # -- markets -------------------------------------------------------------
    def get_markets(self, limit: int = 100) -> list[MarketDTO]:
        url = f"{config.gamma_api_base}/markets"
        params = {"limit": limit, "closed": "false", "order": "volume", "ascending": "false"}
        payload = self._get_json(url, params)
        return parse_markets(payload)

    def get_market(self, condition_id: str) -> MarketDTO | None:
        payload = self._get_json(f"{config.gamma_api_base}/markets", {"condition_ids": condition_id})
        markets = parse_markets(payload)
        return markets[0] if markets else None

    def get_closed_markets(self, limit: int = 100, offset: int = 0) -> list[MarketDTO]:
        """Resolved/closed historical markets (paginated) for the replay backfill."""
        payload = self._get_json(f"{config.gamma_api_base}/markets",
                                 {"closed": "true", "limit": limit, "offset": offset,
                                  "order": "volume", "ascending": "false"})
        return parse_markets(payload)

    def get_markets_by_conditions(self, condition_ids, chunk: int = 40) -> list[MarketDTO]:
        """Fetch real metadata for specific markets by condition id, in batches.

        Gamma's /markets defaults to `closed=false`, so a plain query hides
        *resolved* markets — exactly the ones we need to settle wallet P&L (a
        wallet's edge only shows on markets that have actually resolved). We
        therefore query each batch for both closed states and merge, so resolved
        markets come back with their winning outcome."""
        ids = [c for c in dict.fromkeys(condition_ids) if c]  # dedupe, keep order
        merged: dict[str, MarketDTO] = {}
        for i in range(0, len(ids), chunk):
            batch = ids[i : i + chunk]
            for closed in ("true", "false"):
                try:
                    payload = self._get_json(
                        f"{config.gamma_api_base}/markets",
                        {"condition_ids": batch, "closed": closed, "limit": len(batch)},
                    )
                except httpx.HTTPError as exc:
                    print(f"[live] market batch failed (closed={closed}, {len(batch)} ids): {exc}")
                    continue
                rows = payload.get("data") if isinstance(payload, dict) else payload
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    try:  # one malformed market shouldn't drop the rest of the batch
                        m = parse_market(row)
                        merged[m.id] = m
                    except LiveParseError as exc:
                        print(f"[live] skipped unparseable market: {exc}")
        return list(merged.values())

    # -- trades --------------------------------------------------------------
    def get_recent_trades(self, limit: int = 50) -> list[TradeDTO]:
        url = f"{config.data_api_base}/trades"
        payload = self._get_json(url, {"limit": limit, "takerOnly": "false"})
        return parse_trades(payload)

    def get_wallet_trades(self, address: str, limit: int = 200) -> list[TradeDTO]:
        """Recent trade activity for one wallet. NOTE: this is a recent window,
        not guaranteed full lifetime history (see partial-history handling)."""
        url = f"{config.data_api_base}/trades"
        payload = self._get_json(url, {"user": address, "limit": limit})
        return parse_trades(payload)

    # -- prices --------------------------------------------------------------
    def get_token_midpoint(self, token_id: str) -> float:
        return parse_midpoint(self._get_json(f"{config.clob_api_base}/midpoint",
                                             {"token_id": token_id}))

    def get_token_book(self, token_id: str) -> dict:
        return parse_orderbook(self._get_json(f"{config.clob_api_base}/book",
                                              {"token_id": token_id}))

    def get_prices(self, market_ids: list[str]) -> dict[str, list[float]]:
        """Refresh prices by re-reading market metadata (Gamma outcomePrices).

        This avoids a per-token CLOB call per market. For tighter live mids use
        `get_token_midpoint()` with a market's token_ids.
        """
        wanted = set(market_ids)
        result: dict[str, list[float]] = {}
        for m in self.get_markets(limit=max(len(wanted), 100)):
            if m.id in wanted:
                result[m.id] = m.prices
        return result

    def close(self) -> None:
        self._client.close()


def get_provider(data_mode: str) -> DataProvider:
    """Factory used by the rest of the app. Defaults to mock."""
    if data_mode == "live":
        return LivePolymarketClient()
    from .mock_provider import MockProvider

    return MockProvider()
