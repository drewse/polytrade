"""CLOB access for the BTC 5M live-maker trial.

Two concerns, kept separate:
  * READ-ONLY market data (open BTC-5m markets + the live order book) via public
    HTTP — needs no credentials, used by shadow AND live.
  * ORDER placement — three pluggable clients with an identical interface:
      - MockClobClient  : offline, injected latencies/fills (tests).
      - ShadowClient    : real book reads, but post/cancel are NO-OPs (logs intent).
      - LiveClobClient  : the ONLY one that sends real orders; lazy-imports
                          py-clob-client and is instantiated ONLY when the executor
                          is enabled + armed in 'live' mode.

`py-clob-client` is imported lazily so this module (and the whole app) loads fine
without it — it is added to requirements only when we actually go live.
"""
from __future__ import annotations

import time
from datetime import datetime

import httpx

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
_TIMEOUT = 8.0


def _now():
    return datetime.utcnow(), time.monotonic_ns()


# ---------------------------------------------------------------------------
# read-only market data (public; no credentials)
# ---------------------------------------------------------------------------
def open_btc5m_markets(limit: int = 30) -> list[dict]:
    """The CURRENT live BTC 5-minute up/down market(s) + their CLOB token ids. Read-only.

    BTC-5m market slugs sit on exact 5-minute unix boundaries (`btc-updown-5m-<ts>`,
    ts % 300 == 0). Gamma's market LIST won't surface the live one (it returns stale /
    far-future markets first), so we COMPUTE the current boundary and fetch the
    next/current/previous windows by exact slug — freshest (newest, fullest-life,
    two-sided book) first."""
    import json as _json
    import time as _time
    now = int(_time.time())
    base = (now // 300) * 300
    out = []
    try:
        with httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": "polytrade-research"}) as c:
            for ts in (base + 300, base, base - 300):     # next (fresh), current, prev
                try:
                    r = c.get(f"{GAMMA}/markets", params={"slug": f"btc-updown-5m-{ts}"})
                    r.raise_for_status()
                    d = r.json()
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(d, list) or not d:
                    continue
                m = d[0]
                if m.get("closed"):
                    continue
                toks = m.get("clobTokenIds") or m.get("clob_token_ids") or []
                if isinstance(toks, str):
                    try:
                        toks = _json.loads(toks)
                    except Exception:  # noqa: BLE001
                        toks = []
                if not toks:
                    continue
                out.append({"market_id": m.get("conditionId") or m.get("id"),
                            "slug": (m.get("slug") or "").lower(), "question": m.get("question"),
                            "token_ids": toks, "end_date": m.get("endDate"), "window_ts": ts})
                if len(out) >= limit:
                    break
    except Exception:  # noqa: BLE001
        return out
    return out


def get_book(token_id: str) -> dict:
    """Best bid/ask/mid for a token, timestamped. Read-only public CLOB book. Fail-soft."""
    wall, mono = _now()
    try:
        with httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": "polytrade-research"}) as c:
            r = c.get(f"{CLOB}/book", params={"token_id": token_id})
            r.raise_for_status()
            b = r.json()
        bids = [(float(x["price"]), float(x["size"])) for x in (b.get("bids") or [])]
        asks = [(float(x["price"]), float(x["size"])) for x in (b.get("asks") or [])]
        best_bid = max((p for p, _ in bids), default=None)
        best_ask = min((p for p, _ in asks), default=None)
        mid = (best_bid + best_ask) / 2 if (best_bid is not None and best_ask is not None) else None
        bid_size = next((s for p, s in bids if p == best_bid), 0.0) if best_bid is not None else 0.0
        return {"ok": True, "best_bid": best_bid, "best_ask": best_ask, "mid": mid,
                "bid_size": bid_size, "ts": wall, "mono_ns": mono, "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "best_bid": None, "best_ask": None, "mid": None,
                "ts": wall, "mono_ns": mono, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# order clients — identical interface; only LiveClobClient sends real orders
# ---------------------------------------------------------------------------
class MockClobClient:
    """Offline simulator for tests. Configurable ack/fill/cancel behaviour + latency."""
    name = "mock"

    def __init__(self, *, ack_ms=20.0, cancel_ms=15.0, fill_after_polls: int | None = None,
                 fill_price: float | None = None):
        self.ack_ms, self.cancel_ms = ack_ms, cancel_ms
        self.fill_after_polls, self.fill_price = fill_after_polls, fill_price
        self._orders: dict = {}
        self._n = 0

    def post_limit(self, *, token_id, side, price, size):
        self._n += 1
        oid = f"mock-{self._n}"
        self._orders[oid] = {"polls": 0, "price": price, "size": size, "filled": 0.0}
        return {"ok": True, "order_id": oid, "status": "acked", "latency_ms": self.ack_ms, "error": None}

    def get_order(self, order_id):
        o = self._orders.get(order_id)
        if not o:
            return {"ok": False, "status": "unknown", "filled_size": 0.0}
        o["polls"] += 1
        if self.fill_after_polls is not None and o["polls"] >= self.fill_after_polls and o["filled"] < o["size"]:
            o["filled"] = o["size"]
            return {"ok": True, "status": "filled", "filled_size": o["size"],
                    "fill_price": self.fill_price if self.fill_price is not None else o["price"]}
        return {"ok": True, "status": "resting", "filled_size": o["filled"]}

    def cancel(self, order_id):
        o = self._orders.get(order_id)
        filled = o and o["filled"] >= o["size"]
        return {"ok": True, "cancelled": not filled, "latency_ms": self.cancel_ms, "error": None}

    def open_orders(self):
        return [oid for oid, o in self._orders.items() if o["filled"] < o["size"]]


class ShadowClient:
    """Reads the real book but NEVER sends an order — post/cancel are logged no-ops.
    Used for live-data dry-runs before any real money."""
    name = "shadow"

    def post_limit(self, *, token_id, side, price, size):
        return {"ok": True, "order_id": None, "status": "shadow", "latency_ms": 0.0,
                "error": None, "would_place": {"token_id": token_id, "side": side, "price": price, "size": size}}

    def get_order(self, order_id):
        return {"ok": True, "status": "shadow", "filled_size": 0.0}

    def cancel(self, order_id):
        return {"ok": True, "cancelled": True, "latency_ms": 0.0, "error": None}

    def open_orders(self):
        return []


class LiveClobClient:
    """The ONLY client that places real orders. Lazy-imports py-clob-client; raises a
    clear error if the library/credentials are absent. Instantiated by the executor
    ONLY when enabled + armed in 'live' mode."""
    name = "live"

    def __init__(self, *, private_key: str, host: str = CLOB, chain_id: int = 137):
        try:
            from py_clob_client.client import ClobClient  # noqa
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"py-clob-client not installed — cannot go live: {exc}")
        self._ClobClient = ClobClient
        self._client = ClobClient(host, key=private_key, chain_id=chain_id)
        self._client.set_api_creds(self._client.create_or_derive_api_creds())

    def post_limit(self, *, token_id, side, price, size):
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL
        t0 = time.monotonic()
        args = OrderArgs(price=float(price), size=float(size),
                         side=BUY if side == "BUY" else SELL, token_id=str(token_id))
        signed = self._client.create_order(args)
        resp = self._client.post_order(signed, OrderType.GTC)
        lat = (time.monotonic() - t0) * 1000
        oid = (resp or {}).get("orderID") or (resp or {}).get("order_id")
        return {"ok": bool(oid), "order_id": oid, "status": "acked" if oid else "rejected",
                "latency_ms": lat, "raw": resp, "error": None if oid else str(resp)}

    def get_order(self, order_id):
        o = self._client.get_order(order_id)
        size = float((o or {}).get("size_matched", 0) or 0)
        status = (o or {}).get("status", "unknown")
        return {"ok": True, "status": status, "filled_size": size, "raw": o}

    def cancel(self, order_id):
        t0 = time.monotonic()
        resp = self._client.cancel(order_id)
        return {"ok": True, "cancelled": True, "latency_ms": (time.monotonic() - t0) * 1000, "raw": resp}

    def open_orders(self):
        try:
            return [o.get("id") or o.get("order_id") for o in (self._client.get_orders() or [])]
        except Exception:  # noqa: BLE001
            return []
