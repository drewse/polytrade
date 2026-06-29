"""BTC 5M Micro-Test V3 Phase 1 — on-chain OrderFilled detector (PAPER-ONLY).

Detects watched micro-test wallets' fills directly from Polygon `OrderFilled`
logs (the only public real-time source carrying wallet identity), maps them to
BTC up/down markets, and measures detection latency + price drift to decide
whether a sub-5s wallet-copy edge is achievable.

SAFETY — this module NEVER:
  * places an order or calls any executor,
  * writes to LiveExecution / LiveState / micro-test trade tables,
  * touches production copy trading, ranking, sizing, bankroll, or risk controls.
It only reads chain logs + Gamma metadata and writes btc5m_onchain_* rows.
Default DISABLED + paper-only.

Transport: an `eth_getLogs` poller over JSON-RPC. The poll-from-last-block loop
IS the reconnect/gap-fill mechanism (persisted cursor + tx/log dedup), so a true
`eth_subscribe` WS push is a drop-in latency optimisation over the identical
decode/measure core. All network calls are injectable for tests.
"""
from __future__ import annotations

import os
import re
import threading
import time
import traceback
from datetime import datetime
from urllib.parse import urlparse

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import btc5m, btc5m_micro_test as umt
from . import btc5m_onchain_models as om
from .settings import config

# OrderFilled(bytes32 orderHash, address maker, address taker, uint256 makerAssetId,
#  uint256 takerAssetId, uint256 makerAmountFilled, uint256 takerAmountFilled, uint256 fee)
ORDERFILLED_TOPIC0 = "0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06bd0c789af57f2d65bfec0f6"
COLLATERAL_ASSET_ID = "0"          # USDC collateral is assetId 0 in CTF exchange accounting
DECIMALS = 1_000_000               # USDC + CTF outcome tokens both use 6 decimals

# Polygon CTF exchange contracts (lowercased). Confirm on PolygonScan before live use.
DEFAULT_EXCHANGES = [
    "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",   # CTF Exchange v1
    "0xe111180000d2663c0091e4f400237545b87b996b",   # CTF Exchange v2
    "0xc5d563a36ae78145c45a50134d48a1215220f80a",   # NegRisk CTF Exchange (confirm)
]


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


class OnchainRpcError(RuntimeError):
    """A JSON-RPC failure that names the failing method + endpoint scheme + the
    response body + a remediation hint (so the dashboard shows something useful
    instead of a bare '400 Bad Request')."""

    def __init__(self, *, method: str, scheme: str | None = None, host: str | None = None,
                 status: int | None = None, body: str | None = None, hint: str | None = None):
        self.method, self.scheme, self.host = method, scheme, host
        self.status, self.body, self.hint = status, body, hint
        msg = f"{method} failed"
        if status is not None:
            msg += f": HTTP {status}"
        if scheme or host:
            msg += f" via {scheme or '?'}://{host or '?'}"
        if hint:
            msg += f" — {hint}"
        if body:
            msg += f" | response: {str(body)[:160]}"
        super().__init__(msg)


def _ws_to_http(url: str) -> str:
    """Best-effort wss/ws -> https/http for JSON-RPC polling. Handles the common
    providers whose HTTP path differs from their WS path (e.g. Infura's '/ws')."""
    p = urlparse(url)
    scheme = "https" if p.scheme == "wss" else "http"
    netloc, path = p.netloc, p.path
    if "infura.io" in netloc and path.startswith("/ws/"):
        path = path[3:]                                   # '/ws/v3/KEY' -> '/v3/KEY'
    rebuilt = f"{scheme}://{netloc}{path}"
    return rebuilt + (f"?{p.query}" if p.query else "")


def _resolve_http_rpc(https_url: str, ws_url: str) -> dict:
    """Pick the HTTP(S) JSON-RPC endpoint used for eth_getLogs polling.
    Prefers POLYGON_RPC_URL; falls back to converting POLYGON_WS_RPC_URL."""
    https_url, ws_url = (https_url or "").strip(), (ws_url or "").strip()
    if https_url:
        if https_url.startswith(("http://", "https://")):
            return {"url": https_url, "scheme": urlparse(https_url).scheme,
                    "source": "POLYGON_RPC_URL", "converted": False, "error": None, "note": None}
        return {"url": None, "scheme": urlparse(https_url).scheme or "?", "source": "POLYGON_RPC_URL",
                "converted": False, "note": None,
                "error": "POLYGON_RPC_URL must be an HTTPS JSON-RPC endpoint (eth_getLogs polling needs "
                         "https://…, not wss://). Set POLYGON_RPC_URL=https://polygon-mainnet.g.alchemy.com/v2/<key>."}
    if ws_url:
        if ws_url.startswith(("http://", "https://")):
            return {"url": ws_url, "scheme": urlparse(ws_url).scheme, "source": "POLYGON_WS_RPC_URL",
                    "converted": False, "error": None, "note": None}
        if ws_url.startswith(("wss://", "ws://")):
            return {"url": _ws_to_http(ws_url), "scheme": "https" if ws_url.startswith("wss://") else "http",
                    "source": "POLYGON_WS_RPC_URL", "converted": True, "error": None,
                    "note": "converted wss://→https:// for eth_getLogs polling — set POLYGON_RPC_URL "
                            "to the provider's HTTPS endpoint to be explicit"}
        return {"url": None, "scheme": "?", "source": "POLYGON_WS_RPC_URL", "converted": False, "note": None,
                "error": "POLYGON_WS_RPC_URL has an unrecognized scheme — set POLYGON_RPC_URL=https://… for polling."}
    return {"url": None, "scheme": None, "source": None, "converted": False, "note": None,
            "error": "no Polygon RPC configured — set POLYGON_RPC_URL=https://… (HTTPS JSON-RPC) for eth_getLogs polling."}


def _cfg() -> dict:
    mt = umt._cfg()
    watched = {mt["primary_wallet"], *mt["backup_wallets"]}
    watched = {w.lower() for w in watched if w}
    ex = os.getenv("BTC5M_ONCHAIN_EXCHANGES", "")
    exchanges = [a.strip().lower() for a in ex.replace(";", ",").split(",") if a.strip()] or DEFAULT_EXCHANGES
    rpc = _resolve_http_rpc(os.getenv("POLYGON_RPC_URL", ""), os.getenv("POLYGON_WS_RPC_URL", ""))
    return {
        "enabled": _truthy(os.getenv("BTC5M_ONCHAIN_ENABLED", "false")),
        "paper_only": _truthy(os.getenv("BTC5M_ONCHAIN_PAPER_ONLY", "true")),
        # raw configured URL (either var) — used only for 'is it configured' checks
        "rpc_url": (os.getenv("POLYGON_RPC_URL", "") or os.getenv("POLYGON_WS_RPC_URL", "") or "").strip(),
        "http_rpc_url": rpc["url"],                        # the actual endpoint we POST to
        "rpc_scheme": rpc["scheme"], "rpc_source": rpc["source"], "rpc_converted": rpc["converted"],
        "rpc_config_error": rpc["error"], "rpc_note": rpc["note"],
        "ws_rpc_url": (os.getenv("POLYGON_WS_RPC_URL", "") or "").strip(),   # future ws subscribe
        "exchanges": exchanges,
        "confirmations": int(os.getenv("BTC5M_ONCHAIN_CONFIRMATIONS", "0")),
        "poll_gamma_seconds": int(os.getenv("BTC5M_ONCHAIN_POLL_GAMMA_SECONDS", "30")),
        "max_signals": int(os.getenv("BTC5M_ONCHAIN_MAX_SIGNALS", "50")),
        "target_latency_s": float(os.getenv("BTC5M_ONCHAIN_TARGET_LATENCY_SECONDS", "5")),
        "poll_seconds": max(1, int(os.getenv("BTC5M_ONCHAIN_POLL_SECONDS", "2"))),
        "diag_blocks": max(1, int(os.getenv("BTC5M_ONCHAIN_DIAG_BLOCKS", "5"))),
        # Alchemy's FREE tier caps eth_getLogs at a 10-block range; chunk every
        # query to <= this many blocks (also the per-cycle catch-up window).
        "max_block_span": max(1, int(os.getenv("BTC5M_ONCHAIN_MAX_BLOCK_SPAN", "10"))),
        "watched": watched,
        "max_entry_price": mt["max_entry_price"],
        "min_seconds_remaining": mt["min_seconds_remaining"],
    }


# ---------------------------------------------------------------------------
# pure decode + classify (no network — fully unit-testable)
# ---------------------------------------------------------------------------
def _hexint(v) -> int:
    if isinstance(v, int):
        return v
    s = str(v)
    return int(s, 16) if s.startswith("0x") else int(s)


def _addr_from_topic(topic: str) -> str:
    return "0x" + str(topic)[-40:].lower()


def decode_order_filled(log: dict) -> dict:
    """Decode a raw eth log into the OrderFilled fields (dependency-light: topics
    carry the indexed addresses; data is 5 packed uint256 words)."""
    topics = log["topics"]
    data = str(log["data"])[2:] if str(log["data"]).startswith("0x") else str(log["data"])
    words = [data[i:i + 64] for i in range(0, len(data), 64)]
    return {
        "tx_hash": str(log["transactionHash"]).lower(),
        "log_index": _hexint(log.get("logIndex", 0)),
        "block_number": _hexint(log.get("blockNumber", 0)),
        "address": str(log["address"]).lower(),
        "order_hash": str(topics[1]),
        "maker": _addr_from_topic(topics[2]),
        "taker": _addr_from_topic(topics[3]),
        "maker_asset_id": str(int(words[0], 16)),
        "taker_asset_id": str(int(words[1], 16)),
        "maker_amount": int(words[2], 16),
        "taker_amount": int(words[3], 16),
        "fee": int(words[4], 16),
    }


def classify_fill(dec: dict, watched: set[str], exchanges: set[str]) -> dict | None:
    """From the watched wallet's perspective, derive side/token/price/shares/usd.
    Returns None if the watched wallet is not involved or the counterparty is the
    exchange contract itself (mint/burn sub-event)."""
    maker, taker = dec["maker"], dec["taker"]
    role = "maker" if maker in watched else "taker" if taker in watched else None
    if role is None:
        return None
    if maker == taker:
        return None
    other = taker if role == "maker" else maker
    if other in exchanges:                              # exchange-as-counterparty sub-event
        return None
    if role == "maker":
        if dec["maker_asset_id"] == COLLATERAL_ASSET_ID:   # gave USDC -> BOUGHT tokens
            side, token, usd_raw, sh_raw = "buy", dec["taker_asset_id"], dec["maker_amount"], dec["taker_amount"]
        else:                                              # gave tokens -> SOLD
            side, token, sh_raw, usd_raw = "sell", dec["maker_asset_id"], dec["maker_amount"], dec["taker_amount"]
        wallet = maker
    else:
        if dec["taker_asset_id"] == COLLATERAL_ASSET_ID:   # gave USDC -> BOUGHT tokens
            side, token, usd_raw, sh_raw = "buy", dec["maker_asset_id"], dec["taker_amount"], dec["maker_amount"]
        else:
            side, token, sh_raw, usd_raw = "sell", dec["taker_asset_id"], dec["taker_amount"], dec["maker_amount"]
        wallet = taker
    price = round(usd_raw / sh_raw, 4) if sh_raw else None   # 1e6 scaling cancels -> $/share
    return {"watched": wallet, "role": role, "side": side, "token_id": token,
            "usd": round(usd_raw / DECIMALS, 4), "shares": round(sh_raw / DECIMALS, 4), "price": price}


# ---------------------------------------------------------------------------
# Gamma token map (token_id -> BTC up/down market metadata)
# ---------------------------------------------------------------------------
_DUR_RE = re.compile(r"(\d+)\s*m", re.I)


def _parse_duration(slug: str | None, question: str | None) -> int | None:
    for s in (slug or "", question or ""):
        m = _DUR_RE.search(s)
        if m:
            return int(m.group(1))
    return None


def _is_btc_updown(question: str | None, slug: str | None) -> bool:
    return btc5m.is_btc5m_market(question, slug, None) or "btc-updown" in (slug or "").lower()


def _parse_market_row(row: dict) -> dict | None:
    """Normalize a Gamma market row to the fields we need. Tolerates JSON-encoded
    string arrays (clobTokenIds / outcomes)."""
    import json
    q = row.get("question") or row.get("title")
    slug = row.get("slug")
    if not _is_btc_updown(q, slug):
        return None
    toks = row.get("clobTokenIds") or row.get("clob_token_ids") or []
    outs = row.get("outcomes") or []
    if isinstance(toks, str):
        try: toks = json.loads(toks)
        except Exception: toks = []
    if isinstance(outs, str):
        try: outs = json.loads(outs)
        except Exception: outs = []
    if not toks:
        return None
    return {
        "condition_id": row.get("conditionId") or row.get("condition_id"),
        "market_id": row.get("conditionId") or row.get("condition_id") or row.get("id"),
        "clob_token_ids": [str(t) for t in toks],
        "outcomes": list(outs),
        "question": q, "slug": slug,
        "end_date": row.get("endDate") or row.get("end_date"),
        "closed": bool(row.get("closed", False)),
        "duration_minutes": _parse_duration(slug, q),
    }


def _default_gamma_fetch() -> list[dict]:
    url = f"{config.gamma_api_base}/markets"
    out: list[dict] = []
    with httpx.Client(timeout=config.http_timeout_seconds) as c:
        # active (open) markets; Gamma paginates — a couple of pages of recent
        # high-volume markets covers the rolling BTC up/down set.
        for off in (0, 500):
            try:
                r = c.get(url, params={"closed": "false", "limit": 500, "offset": off,
                                       "order": "startDate", "ascending": "false"})
                r.raise_for_status()
                rows = r.json()
                out.extend(rows if isinstance(rows, list) else rows.get("data", []))
            except Exception:  # noqa: BLE001
                break
    return out


def build_token_map(rows: list[dict]) -> dict[str, dict]:
    tmap: dict[str, dict] = {}
    for row in rows or []:
        meta = _parse_market_row(row)
        if not meta:
            continue
        for i, tok in enumerate(meta["clob_token_ids"]):
            outcome = meta["outcomes"][i] if i < len(meta["outcomes"]) else None
            tmap[str(tok)] = {**meta, "outcome": outcome, "outcome_index": i}
    return tmap


def fetch_token_map(fetch_fn=None) -> dict[str, dict]:
    rows = (fetch_fn or _default_gamma_fetch)()
    return build_token_map(rows)


# ---------------------------------------------------------------------------
# JSON-RPC transport (eth_getLogs poller; injectable for tests)
# ---------------------------------------------------------------------------
def _rpc(cfg: dict, method: str, params: list):
    url = cfg.get("http_rpc_url")
    if not url:
        raise OnchainRpcError(method=method, scheme=cfg.get("rpc_scheme"),
                              hint=cfg.get("rpc_config_error") or "POLYGON_RPC_URL not set")
    p = urlparse(url)
    scheme, host = p.scheme, p.netloc
    try:
        with httpx.Client(timeout=config.http_timeout_seconds) as c:
            r = c.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    except httpx.HTTPError as exc:
        raise OnchainRpcError(method=method, scheme=scheme, host=host,
                              hint="network error contacting the RPC endpoint", body=str(exc))
    if r.status_code != 200:
        if scheme != "https":
            hint = ("eth_getLogs polling needs an HTTPS JSON-RPC endpoint — set "
                    "POLYGON_RPC_URL=https://… (wss:// is only for future websocket subscriptions)")
        elif r.status_code in (400, 401, 403):
            hint = ("RPC rejected the request — verify the API key / HTTPS endpoint. If you only set "
                    "POLYGON_WS_RPC_URL (wss://), set POLYGON_RPC_URL to the provider's matching https:// URL")
        else:
            hint = "RPC endpoint returned a non-200 status"
        raise OnchainRpcError(method=method, scheme=scheme, host=host, status=r.status_code,
                              body=(r.text or "")[:200], hint=hint)
    d = r.json()
    if d.get("error"):
        raise OnchainRpcError(method=method, scheme=scheme, host=host, status=200,
                              body=str(d["error"])[:200], hint="RPC returned a JSON-RPC error")
    return d.get("result")


def _topic_addr(addr: str) -> str:
    return "0x" + addr[2:].rjust(64, "0").lower()


def _block_chunks(from_block: int, to_block: int, span: int):
    """Yield (a, b) windows of at most `span` blocks (free-tier eth_getLogs cap)."""
    a = from_block
    while a <= to_block:
        b = min(to_block, a + span - 1)
        yield a, b
        a = b + 1


def rpc_block_number(cfg: dict) -> int:
    return _hexint(_rpc(cfg, "eth_blockNumber", []))


def rpc_get_logs(cfg: dict, from_block: int, to_block: int) -> list[dict]:
    """OrderFilled logs in [from_block, to_block] where a WATCHED wallet is maker
    OR taker (two topic-filtered queries, merged + de-duplicated). Chunked to the
    free-tier block-range cap so wide ranges don't 400."""
    watched_topics = [_topic_addr(a) for a in sorted(cfg["watched"])]
    if not watched_topics:
        return []
    span = cfg.get("max_block_span", 10)
    seen, out = set(), []
    for ca, cb in _block_chunks(from_block, to_block, span):
        base = {"fromBlock": hex(ca), "toBlock": hex(cb), "address": cfg["exchanges"]}
        for slot in (2, 3):                             # topic[2]=maker, topic[3]=taker
            topics = [ORDERFILLED_TOPIC0, None, None, None]
            topics[slot] = watched_topics
            for lg in (_rpc(cfg, "eth_getLogs", [{**base, "topics": topics[:slot + 1]}]) or []):
                key = (str(lg["transactionHash"]).lower(), _hexint(lg.get("logIndex", 0)))
                if key in seen:
                    continue
                seen.add(key)
                out.append(lg)
    return out


def rpc_get_logs_all(cfg: dict, from_block: int, to_block: int) -> list[dict]:
    """ALL OrderFilled logs on the configured exchanges in [from_block, to_block]
    (no wallet filter). Read-only diagnostic: lets us see whether the chain is
    producing OrderFilled at all (RPC + contract addresses + topic0 healthy) even
    when no WATCHED wallet has traded. Chunked to the free-tier block-range cap."""
    span = cfg.get("max_block_span", 10)
    out: list[dict] = []
    for ca, cb in _block_chunks(from_block, to_block, span):
        out.extend(_rpc(cfg, "eth_getLogs", [{"fromBlock": hex(ca), "toBlock": hex(cb),
                                              "address": cfg["exchanges"], "topics": [ORDERFILLED_TOPIC0]}]) or [])
    return out


_BLOCK_TS_CACHE: dict[int, datetime] = {}


def rpc_block_timestamp(cfg: dict, block_number: int) -> datetime | None:
    if block_number in _BLOCK_TS_CACHE:
        return _BLOCK_TS_CACHE[block_number]
    try:
        b = _rpc(cfg, "eth_getBlockByNumber", [hex(block_number), False])
        ts = datetime.utcfromtimestamp(_hexint(b["timestamp"]))
        _BLOCK_TS_CACHE[block_number] = ts
        return ts
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------
def get_state(db: Session) -> om.Btc5mOnchainState:
    st = db.get(om.Btc5mOnchainState, 1)
    if st is None:
        st = om.Btc5mOnchainState(id=1)
        db.add(st)
        db.commit()
    return st


def _exists(db: Session, tx_hash: str, log_index: int) -> bool:
    return db.scalar(select(func.count()).select_from(om.Btc5mOnchainSignal)
                     .where(om.Btc5mOnchainSignal.tx_hash == tx_hash,
                            om.Btc5mOnchainSignal.log_index == log_index)) > 0


def _seconds_until_expiry(end_date, now: datetime) -> float | None:
    if not end_date:
        return None
    try:
        from dateutil import parser as _p
        end = _p.parse(str(end_date))
        if end.tzinfo is not None:
            end = end.replace(tzinfo=None)
        return (end - now).total_seconds()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# process logs -> measured paper-only signals
# ---------------------------------------------------------------------------
def process_logs(db: Session, logs: list[dict], *, cfg: dict, tmap: dict, now: datetime | None = None,
                 price_fn=None, block_ts_fn=None) -> dict:
    now = now or datetime.utcnow()
    watched, exchanges = cfg["watched"], set(cfg["exchanges"])
    created = ignored = skipped = 0
    # per-cycle read-only diagnostics
    diag = {"decoded": 0, "watched": 0, "btc_matches": 0, "ignored_by_reason": {},
            "last_orderfilled": None, "last_orderfilled_at": None,
            "last_watched": None, "last_watched_at": None,
            "last_btc": None, "last_btc_at": None}
    for log in logs:
        topics = log.get("topics") or []
        if not topics or str(topics[0]).lower() != ORDERFILLED_TOPIC0:
            continue
        if str(log.get("address", "")).lower() not in exchanges:
            continue
        dec = decode_order_filled(log)
        diag["decoded"] += 1
        diag["last_orderfilled"] = f"block {dec['block_number']} {dec['maker'][:10]}…→{dec['taker'][:10]}…"
        diag["last_orderfilled_at"] = now
        if _exists(db, dec["tx_hash"], dec["log_index"]):        # dedup (gap-fill safe)
            skipped += 1
            continue
        cl = classify_fill(dec, watched, exchanges)
        if cl is None:                                           # not a watched-wallet fill
            continue
        diag["watched"] += 1
        diag["last_watched"] = f"block {dec['block_number']} {cl['watched'][:10]}… {cl['side']} {cl['token_id'][:8]}…"
        diag["last_watched_at"] = now
        meta = tmap.get(str(cl["token_id"]))
        if meta is not None:
            diag["btc_matches"] += 1
            diag["last_btc"] = f"{(meta.get('question') or '')[:32]} {cl['side']} @ {cl['price']}"
            diag["last_btc_at"] = now
        block_ts = (block_ts_fn or (lambda b: rpc_block_timestamp(cfg, b)))(dec["block_number"])
        latency_ms = round((now - block_ts).total_seconds() * 1000, 1) if block_ts else None
        secs = _seconds_until_expiry(meta.get("end_date") if meta else None, now)

        ignored_reason = None
        if meta is None:
            ignored_reason = "token not in BTC up/down map"
        elif meta.get("closed"):
            ignored_reason = "market closed"
        elif cl["side"] != "buy":
            ignored_reason = "not an opening BUY"

        # price-at-detection + drift (best-effort)
        det_price = None
        if meta is not None and price_fn is not None:
            try: det_price = float(price_fn(str(cl["token_id"])))
            except Exception: det_price = None
        elif meta is not None and price_fn is None:
            det_price = _safe_price(str(cl["token_id"]))
        drift = round(det_price - cl["price"], 4) if (det_price is not None and cl["price"] is not None) else None

        # gate simulation (paper only — no order is ever placed)
        would_pass = None
        if ignored_reason is None:
            would_pass = bool(cl["price"] is not None and cl["price"] <= cfg["max_entry_price"]
                              and (secs is None or secs >= cfg["min_seconds_remaining"]))
            if not would_pass and ignored_reason is None:
                ignored_reason = ("price > max entry" if (cl["price"] or 0) > cfg["max_entry_price"]
                                  else f"< {cfg['min_seconds_remaining']:.0f}s remaining")

        direction = btc5m._yes_no(meta.get("outcome")) if meta else None
        db.add(om.Btc5mOnchainSignal(
            tx_hash=dec["tx_hash"], log_index=dec["log_index"], block_number=dec["block_number"],
            block_timestamp=block_ts, detected_at=now, exchange_address=dec["address"],
            watched_wallet=cl["watched"], wallet_role=cl["role"],
            market_id=(meta or {}).get("market_id"), condition_id=(meta or {}).get("condition_id"),
            token_id=str(cl["token_id"]), question=(meta or {}).get("question"),
            outcome=(meta or {}).get("outcome"), direction=direction, side=cl["side"],
            price=cl["price"], shares=cl["shares"], usd_amount=cl["usd"],
            duration_minutes=(meta or {}).get("duration_minutes"), seconds_until_expiry=secs,
            detection_latency_ms=latency_ms, market_price_at_detection=det_price,
            price_drift=drift, missed_edge=drift, would_pass_gates=would_pass,
            ignored_reason=ignored_reason))
        created += 1
        if ignored_reason:
            ignored += 1
            diag["ignored_by_reason"][ignored_reason] = diag["ignored_by_reason"].get(ignored_reason, 0) + 1
    db.commit()
    return {"signals_created": created, "ignored": ignored, "deduped": skipped, "diag": diag}


def _safe_price(token_id: str) -> float | None:
    try:
        return float(umt._client().get_token_midpoint(token_id))
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# one poll cycle (reconnect/gap-fill safe)
# ---------------------------------------------------------------------------
def _accumulate_diag(st: om.Btc5mOnchainState, *, blocks: int, logs_all: int, res: dict) -> None:
    """Fold a cycle's read-only diagnostics into the persisted detector state."""
    d = res.get("diag", {}) if res else {}
    st.blocks_scanned = (st.blocks_scanned or 0) + max(0, blocks)
    st.logs_scanned = (st.logs_scanned or 0) + max(0, logs_all)
    st.events_decoded = (st.events_decoded or 0) + d.get("decoded", 0)
    st.events_watched = (st.events_watched or 0) + d.get("watched", 0)
    st.btc_matches = (st.btc_matches or 0) + d.get("btc_matches", 0)
    if d.get("ignored_by_reason"):
        merged = dict(st.ignored_by_reason or {})
        for k, v in d["ignored_by_reason"].items():
            merged[k] = merged.get(k, 0) + v
        st.ignored_by_reason = merged
    if d.get("last_orderfilled"):
        st.last_orderfilled_at = d["last_orderfilled_at"]; st.last_orderfilled_desc = d["last_orderfilled"]
    if d.get("last_watched"):
        st.last_watched_event_at = d["last_watched_at"]; st.last_watched_desc = d["last_watched"]
    if d.get("last_btc"):
        st.last_btc_event_at = d["last_btc_at"]; st.last_btc_desc = d["last_btc"]


def run_once(db: Session, *, now: datetime | None = None, fetch_logs_fn=None, latest_block_fn=None,
             token_fetch_fn=None, price_fn=None, block_ts_fn=None, fetch_all_logs_fn=None) -> dict:
    now = now or datetime.utcnow()
    cfg = _cfg()
    if not cfg["enabled"]:
        return {"ran": False, "reason": "BTC5M_ONCHAIN_ENABLED is false"}
    if not cfg["watched"]:
        return {"ran": False, "reason": "no watched wallets configured"}
    st = get_state(db)
    if cfg.get("rpc_config_error"):                       # bad/misconfigured RPC URL -> clear error
        st.rpc_connected = False
        st.error_count = (st.error_count or 0) + 1
        st.last_error = cfg["rpc_config_error"]
        db.commit()
        return {"ran": False, "reason": cfg["rpc_config_error"]}
    try:
        # token-map refresh (with read-only status)
        try:
            tmap = fetch_token_map(token_fetch_fn)
            st.token_map_size = len(tmap)
            st.token_map_refreshed_at = now
            st.token_map_error = None if tmap else "token map empty (no BTC up/down markets found)"
        except Exception as exc:  # noqa: BLE001  (token map is best-effort)
            tmap = {}
            st.token_map_error = f"{type(exc).__name__}: {exc}"
        latest = (latest_block_fn or rpc_block_number)(cfg)
        confirmed = max(0, latest - cfg["confirmations"])
        span = cfg["max_block_span"]
        # cursor: resume from last_processed_block+1; first run scans the last
        # `span` blocks (measuring NEW fills, not backfilling all history).
        from_block = (st.last_processed_block + 1) if st.last_processed_block else max(0, confirmed - span + 1)
        if from_block > confirmed:
            st.last_poll_at = now; st.rpc_connected = True; db.commit()
            return {"ran": True, "from_block": from_block, "to_block": confirmed,
                    "signals_created": 0, "ignored": 0, "deduped": 0, "no_new_blocks": True}
        # Free-tier safety + stay real-time: never scan more than `span` blocks
        # behind the tip in one cycle. A stale/way-behind cursor self-heals by
        # jumping forward (old backlog is skipped — we want recent fills).
        skipped = 0
        if confirmed - from_block + 1 > span:
            skipped = (confirmed - span + 1) - from_block
            from_block = confirmed - span + 1
        logs = (fetch_logs_fn or rpc_get_logs)(cfg, from_block, confirmed)
        res = process_logs(db, logs, cfg=cfg, tmap=tmap, now=now, price_fn=price_fn, block_ts_fn=block_ts_fn)

        # read-only diagnostic: count ALL OrderFilled (any wallet) over a bounded
        # recent window, to tell "no watched-wallet trade" apart from "RPC/contract
        # issue". Fail-soft — never affects detection.
        logs_all = 0
        try:
            diag_from = max(from_block, confirmed - cfg["diag_blocks"] + 1)
            logs_all = len((fetch_all_logs_fn or rpc_get_logs_all)(cfg, diag_from, confirmed))
        except Exception:  # noqa: BLE001
            logs_all = max(logs_all, len(logs))      # at least the watched logs we did see

        _accumulate_diag(st, blocks=(confirmed - from_block + 1), logs_all=logs_all, res=res)
        st.last_processed_block = confirmed
        st.last_poll_at = now
        st.rpc_connected = True
        st.last_error = None
        st.signals_captured = db.scalar(select(func.count()).select_from(om.Btc5mOnchainSignal)) or 0
        db.commit()
        return {"ran": True, "from_block": from_block, "to_block": confirmed,
                "skipped_blocks": skipped, "logs_scanned": logs_all, **res}
    except Exception as exc:  # noqa: BLE001  (fail-soft: record, keep cursor, retry next poll)
        st.rpc_connected = False
        st.error_count = (st.error_count or 0) + 1
        st.last_error = f"{type(exc).__name__}: {exc}"
        db.commit()
        return {"ran": False, "reason": st.last_error}


# ---------------------------------------------------------------------------
# stats + go/no-go verdict
# ---------------------------------------------------------------------------
def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return round(xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2, 1)


def _pctile(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return round(xs[min(len(xs) - 1, int(len(xs) * p))], 1)


def stats(db: Session) -> dict:
    rows = list(db.scalars(select(om.Btc5mOnchainSignal)).all())
    lat = [r.detection_latency_ms / 1000.0 for r in rows if r.detection_latency_ms is not None]
    drifts = [r.price_drift for r in rows if r.price_drift is not None]
    absd = [abs(r.price_drift) for r in rows if r.price_drift is not None]
    actionable = [r for r in rows if r.would_pass_gates]
    n = len(lat)
    median = _median(lat)
    under5 = sum(1 for v in lat if v < 5)
    under10 = sum(1 for v in lat if v < 10)
    avg_price = (sum(r.price for r in rows if r.price) / max(1, sum(1 for r in rows if r.price)))
    avg_absd = round(sum(absd) / len(absd), 4) if absd else None
    roi_loss = round(avg_absd / avg_price, 4) if (avg_absd and avg_price) else None

    verdict, recommendation = _verdict(median, under10, n, avg_absd, _cfg()["target_latency_s"])
    return {
        "signals": len(rows),
        "measured": n,
        "actionable_buys": len(actionable),
        "median_latency_s": median,
        "p90_latency_s": _pctile(lat, 0.9),
        "worst_latency_s": round(max(lat), 1) if lat else None,
        "best_latency_s": round(min(lat), 1) if lat else None,
        "pct_under_5s": round(100 * under5 / n, 1) if n else None,
        "pct_under_10s": round(100 * under10 / n, 1) if n else None,
        "avg_price_drift": round(sum(drifts) / len(drifts), 4) if drifts else None,
        "avg_abs_drift": avg_absd,
        "avg_missed_edge": round(sum(drifts) / len(drifts), 4) if drifts else None,
        "est_roi_loss_to_latency": roi_loss,
        "target_latency_s": _cfg()["target_latency_s"],
        "verdict": verdict,
        "recommendation": recommendation,
    }


def _verdict(median, under10, n, avg_absd, target):
    if not n or n < 20:
        return "insufficient_data", f"collect ≥20 signals to decide (have {n})"
    pct10 = 100 * under10 / n
    fast = median is not None and median < target
    coverage = pct10 >= 70
    low_drift = avg_absd is None or avg_absd < 0.03
    if fast and coverage and low_drift:
        return "viable", ("proceed to live micro-test V4 — median detection "
                          f"{median}s < {target}s, {pct10:.0f}% under 10s, drift {avg_absd}")
    if fast and coverage:
        return "marginal", ("latency OK but price drift is high — keep PAPER only and re-check edge "
                            "after drift before any live test")
    return "not_viable", (f"abandon live BTC 5M copy on this source — median {median}s, "
                          f"{pct10:.0f}% under 10s (need <{target}s and ≥70% under 10s)")


# ---------------------------------------------------------------------------
# detector thread (paper-only; opt-in via /start; never auto-starts)
# ---------------------------------------------------------------------------
_run_flag = threading.Event()
_thread: threading.Thread | None = None
_last = {"cycle_at": None, "error": None, "result": None}


def _loop(poll: int) -> None:
    from .db import session_scope
    while _run_flag.is_set():
        try:
            db = session_scope()
            try:
                res = run_once(db)
                _last["cycle_at"] = datetime.utcnow()
                _last["result"] = res.get("reason") or f"+{res.get('signals_created', 0)} signals"
                _last["error"] = None if res.get("ran") else res.get("reason")
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            _last["error"] = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        time.sleep(poll)


def start(db: Session) -> dict:
    """Start the PAPER detector loop (opt-in). Refuses unless ENABLED + RPC set."""
    global _thread
    cfg = _cfg()
    if not cfg["enabled"]:
        return {"ok": False, "error": "BTC5M_ONCHAIN_ENABLED is false"}
    if not cfg["rpc_url"]:
        return {"ok": False, "error": "no Polygon RPC configured — set POLYGON_RPC_URL=https://… (HTTPS JSON-RPC)"}
    if cfg.get("rpc_config_error"):
        return {"ok": False, "error": cfg["rpc_config_error"]}
    if not cfg["watched"]:
        return {"ok": False, "error": "no watched wallets (set BTC5M_MICRO_TEST_PRIMARY_WALLET)"}
    st = get_state(db)
    st.running = True
    st.started_at = datetime.utcnow()
    db.commit()
    if _thread is None or not _thread.is_alive():
        _run_flag.set()
        _thread = threading.Thread(target=_loop, name="btc5m-onchain", args=(cfg["poll_seconds"],), daemon=True)
        _thread.start()
    return {"ok": True, "running": True, "paper_only": cfg["paper_only"], "poll_seconds": cfg["poll_seconds"]}


def stop(db: Session) -> dict:
    _run_flag.clear()
    st = get_state(db)
    st.running = False
    db.commit()
    return {"ok": True, "running": False}


def is_running() -> bool:
    return _run_flag.is_set() and _thread is not None and _thread.is_alive()


# ---------------------------------------------------------------------------
# read-only diagnostics + derived diagnosis (answers: why 0 signals?)
# ---------------------------------------------------------------------------
def _diagnostics(st: om.Btc5mOnchainState, cfg: dict) -> dict:
    return {
        "blocks_scanned": st.blocks_scanned or 0,
        "logs_scanned": st.logs_scanned or 0,                 # all OrderFilled on exchanges
        "orderfilled_decoded": st.events_decoded or 0,        # watched-filtered, decoded
        "events_matching_watched": st.events_watched or 0,
        "btc_token_map_matches": st.btc_matches or 0,
        "ignored_by_reason": st.ignored_by_reason or {},
        "error_count": st.error_count or 0,
        "last_block_scanned": st.last_processed_block,
        "last_orderfilled": st.last_orderfilled_desc,
        "last_orderfilled_at": st.last_orderfilled_at.isoformat() if st.last_orderfilled_at else None,
        "last_watched_event": st.last_watched_desc,
        "last_watched_event_at": st.last_watched_event_at.isoformat() if st.last_watched_event_at else None,
        "last_btc_market_event": st.last_btc_desc,
        "last_btc_market_event_at": st.last_btc_event_at.isoformat() if st.last_btc_event_at else None,
        "last_error": st.last_error,
        "token_map": {
            "size": st.token_map_size or 0,
            "refreshed_at": st.token_map_refreshed_at.isoformat() if st.token_map_refreshed_at else None,
            "error": st.token_map_error,
        },
        "rpc": {
            "scheme": cfg.get("rpc_scheme"),
            "source": cfg.get("rpc_source"),                       # POLYGON_RPC_URL | POLYGON_WS_RPC_URL
            "host": (urlparse(cfg["http_rpc_url"]).netloc if cfg.get("http_rpc_url") else None),
            "converted_from_wss": bool(cfg.get("rpc_converted")),
            "requires": "https (eth_getLogs polling)",
            "config_error": cfg.get("rpc_config_error"),
            "note": cfg.get("rpc_note"),
        },
    }


def _diagnosis(st: om.Btc5mOnchainState, cfg: dict, actionable: int) -> dict:
    """Explain what 0 signals means, per the 4 scenarios. Read-only."""
    if not cfg.get("rpc_url"):
        return {"code": "rpc_not_configured",
                "message": "no Polygon RPC configured — set POLYGON_RPC_URL=https://… (HTTPS JSON-RPC)"}
    if cfg.get("rpc_config_error"):
        return {"code": "rpc_config_issue", "message": cfg["rpc_config_error"]}
    # an RPC/log error must surface BEFORE "not_started" — a failing scan that
    # never advanced is an RPC issue, not an un-started detector.
    if (st.error_count or 0) and not st.rpc_connected:
        return {"code": "rpc_log_issue", "message": f"RPC/log error: {st.last_error}"}
    if not (st.blocks_scanned or 0):
        return {"code": "not_started", "message": "detector has not scanned any blocks yet — start it / run once"}
    if not (st.logs_scanned or 0):
        return {"code": "rpc_log_issue",
                "message": ("no OrderFilled events seen on the configured exchanges across "
                            f"{st.blocks_scanned} blocks — check exchange addresses / topic0 / RPC "
                            "(or, far less likely, the whole venue was idle)")}
    if not (st.events_watched or 0):
        return {"code": "no_watched_trade",
                "message": (f"chain is active ({st.logs_scanned} OrderFilled seen) but NONE involved a "
                            "watched wallet — the watched wallets simply haven't traded")}
    if not (st.btc_matches or 0):
        return {"code": "token_map_issue",
                "message": (f"watched wallets traded ({st.events_watched} events) but no token resolved to a "
                            f"BTC up/down market — token-map issue (size {st.token_map_size}) or non-BTC trades")}
    if not actionable:
        top = sorted((st.ignored_by_reason or {}).items(), key=lambda kv: -kv[1])
        why = ", ".join(f"{k}×{v}" for k, v in top[:4]) or "see ignored table"
        return {"code": "all_ignored",
                "message": f"watched BTC trades detected but all ignored by gates: {why}"}
    return {"code": "detecting", "message": f"detecting actionable signals ({actionable})"}


def _signal_dict(s: om.Btc5mOnchainSignal) -> dict:
    return {"id": s.id, "tx_hash": s.tx_hash, "log_index": s.log_index, "block_number": s.block_number,
            "block_timestamp": s.block_timestamp.isoformat() if s.block_timestamp else None,
            "detected_at": s.detected_at.isoformat() if s.detected_at else None,
            "watched_wallet": s.watched_wallet, "wallet_role": s.wallet_role, "exchange": s.exchange_address,
            "market_id": s.market_id, "question": s.question, "outcome": s.outcome, "direction": s.direction,
            "side": s.side, "price": s.price, "shares": s.shares, "usd_amount": s.usd_amount,
            "duration_minutes": s.duration_minutes, "seconds_until_expiry": s.seconds_until_expiry,
            "detection_latency_ms": s.detection_latency_ms,
            "detection_latency_s": round(s.detection_latency_ms / 1000, 2) if s.detection_latency_ms is not None else None,
            "market_price_at_detection": s.market_price_at_detection, "price_drift": s.price_drift,
            "missed_edge": s.missed_edge, "would_pass_gates": s.would_pass_gates,
            "ignored_reason": s.ignored_reason}


def signals(db: Session, *, limit: int = 50) -> dict:
    rows = db.scalars(select(om.Btc5mOnchainSignal)
                      .order_by(om.Btc5mOnchainSignal.created_at.desc()).limit(limit)).all()
    rows = [_signal_dict(s) for s in rows]
    return {"signals": [r for r in rows if not r["ignored_reason"]],
            "ignored": [r for r in rows if r["ignored_reason"]],
            "all": rows}


def status(db: Session) -> dict:
    cfg = _cfg()
    st = get_state(db)
    st_stats = stats(db)
    actionable = st_stats.get("actionable_buys", 0)
    return {
        "enabled": cfg["enabled"],
        "paper_only": cfg["paper_only"],
        "live_execution": False,            # V3 Phase 1 NEVER executes
        "running": is_running(),
        "rpc_connected": st.rpc_connected,
        "rpc_configured": bool(cfg["rpc_url"]),
        "watched_wallets": sorted(cfg["watched"]),
        "exchanges": cfg["exchanges"],
        "token_map_size": st.token_map_size,
        "last_processed_block": st.last_processed_block,
        "signals_captured": st.signals_captured,
        "started_at": st.started_at.isoformat() if st.started_at else None,
        "last_poll_at": st.last_poll_at.isoformat() if st.last_poll_at else None,
        "last_error": st.last_error or _last["error"],
        "config": {"poll_seconds": cfg["poll_seconds"], "confirmations": cfg["confirmations"],
                   "poll_gamma_seconds": cfg["poll_gamma_seconds"], "max_signals": cfg["max_signals"],
                   "target_latency_s": cfg["target_latency_s"], "max_entry_price": cfg["max_entry_price"],
                   "min_seconds_remaining": cfg["min_seconds_remaining"]},
        "stats": st_stats,
        "diagnostics": _diagnostics(st, cfg),
        "diagnosis": _diagnosis(st, cfg, actionable),
        "safety": ("PAPER-ONLY on-chain latency measurement — never places orders, never touches "
                   "LiveExecution/bankroll/production copy trading"),
    }
