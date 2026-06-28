"""Public Polymarket profile fetcher — AUDIT ONLY.

Fetches public lifetime/rolling stats from Polymarket's public APIs (no auth,
read-only):
  * lb-api.polymarket.com/profit?window={all,1d,7d,30d}&address=<a>  -> P/L + name
  * lb-api.polymarket.com/volume?window=all&address=<a>             -> volume
  * data-api.polymarket.com/value?user=<a>                          -> position value
  * data-api.polymarket.com/positions?user=<a>                      -> open positions

Cached in PublicWalletProfile (separate from internal ranking stats), TTL-gated so
the dashboard never hammers the public APIs. Fail-soft: any error is captured in
fetch_status/fetch_error and NEVER raised to the caller. These stats are NEVER
used to alter ranking/eligibility — visibility only.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import wallet_audit_models as wm

_LB = "https://lb-api.polymarket.com"
_DATA = "https://data-api.polymarket.com"
_HEADERS = {"User-Agent": "polytrade-audit/1.0"}
_TIMEOUT = 12.0
CACHE_TTL_MIN = 180             # re-fetch public stats at most this often
_MAX_REFRESH_PER_CALL = 25      # cap network work per audit refresh
_RATE_LIMIT_S = 0.25           # polite delay between wallet fetches


def _get(url: str, params: dict):
    r = httpx.get(url, params=params, timeout=_TIMEOUT, headers=_HEADERS)
    r.raise_for_status()
    return r.json()


def _profit(address: str, window: str):
    """Public P/L for a window. Returns (amount, name, pseudonym, bio, image) or
    (None, ...) when unavailable."""
    data = _get(f"{_LB}/profit", {"window": window, "address": address})
    if isinstance(data, list) and data:
        row = data[0]
        return (row.get("amount"), row.get("name"), row.get("pseudonym"),
                row.get("bio"), row.get("profileImage"))
    return (None, None, None, None, None)


def fetch_one(address: str) -> dict:
    """Fetch all public stats for one wallet. Never raises — returns a dict with a
    fetch_status of ok | partial | error."""
    out: dict = {"address": address, "fetch_status": "ok", "fetch_error": None,
                 "fetched_at": datetime.utcnow()}
    errors = []
    try:
        amt, name, pseud, bio, img = _profit(address, "all")
        out["pnl_all"] = amt
        out["display_name"] = name
        out["pseudonym"] = pseud
        out["bio"] = bio
        out["profile_image"] = img
    except Exception as exc:  # noqa: BLE001
        errors.append(f"profit_all: {exc}")
    for win, key in (("1d", "pnl_1d"), ("7d", "pnl_7d"), ("30d", "pnl_30d")):
        try:
            out[key] = _profit(address, win)[0]
        except Exception as exc:  # noqa: BLE001
            errors.append(f"profit_{win}: {exc}")
    try:
        vol = _get(f"{_LB}/volume", {"window": "all", "address": address})
        out["volume_all"] = vol[0].get("amount") if isinstance(vol, list) and vol else None
    except Exception as exc:  # noqa: BLE001
        errors.append(f"volume: {exc}")
    try:
        val = _get(f"{_DATA}/value", {"user": address})
        out["position_value"] = val[0].get("value") if isinstance(val, list) and val else None
    except Exception as exc:  # noqa: BLE001
        errors.append(f"value: {exc}")
    try:
        pos = _get(f"{_DATA}/positions", {"user": address, "limit": 200})
        if isinstance(pos, list):
            out["predictions"] = len(pos)
            pnls = [(_num(p.get("cashPnl")), p.get("title"), _num(p.get("size"))) for p in pos]
            pnls = [(a, t, s) for a, t, s in pnls if a is not None]
            if pnls:
                out["biggest_win"] = round(max(a for a, _, _ in pnls), 2)
                out["biggest_loss"] = round(min(a for a, _, _ in pnls), 2)
            sizes = [s for _, _, s in pnls if s is not None]
            out["largest_position_size"] = round(max(sizes), 2) if sizes else None
            top = sorted(pos, key=lambda p: -(_num(p.get("size")) or 0))[:8]
            out["top_positions"] = [{"title": p.get("title"), "size": round(_num(p.get("size")) or 0, 2),
                                     "cashPnl": round(_num(p.get("cashPnl")) or 0, 2),
                                     "curPrice": _num(p.get("curPrice"))} for p in top]
    except Exception as exc:  # noqa: BLE001
        errors.append(f"positions: {exc}")
    if errors:
        # error only when NOTHING came back; otherwise partial
        got_any = any(out.get(k) is not None for k in ("pnl_all", "volume_all", "position_value", "predictions"))
        out["fetch_status"] = "partial" if got_any else "error"
        out["fetch_error"] = "; ".join(errors)[:500]
    return out


def _num(v):
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _store(db: Session, data: dict) -> wm.PublicWalletProfile:
    row = db.get(wm.PublicWalletProfile, data["address"]) or wm.PublicWalletProfile(address=data["address"])
    for k in ("display_name", "pseudonym", "bio", "profile_image", "pnl_all", "pnl_1d", "pnl_7d",
              "pnl_30d", "volume_all", "position_value", "predictions", "biggest_win", "biggest_loss",
              "largest_position_size", "top_positions", "fetch_status", "fetch_error", "fetched_at"):
        if k in data:
            setattr(row, k, data[k])
    db.add(row)
    return row


def get_cached(db: Session, address: str) -> wm.PublicWalletProfile | None:
    return db.get(wm.PublicWalletProfile, address)


def refresh_profiles(db: Session, addresses: list[str], *, force: bool = False,
                     fetch_fn=None) -> dict:
    """Refresh public profiles for the given wallets, skipping ones fetched within
    the TTL (unless force). Bounded + rate-limited + fail-soft. `fetch_fn` is
    injectable for tests. Returns counts."""
    fetch_fn = fetch_fn or fetch_one
    cutoff = datetime.utcnow() - timedelta(minutes=CACHE_TTL_MIN)
    fetched = skipped = errors = 0
    for address in addresses:
        if fetched >= _MAX_REFRESH_PER_CALL:
            break
        existing = db.get(wm.PublicWalletProfile, address)
        if not force and existing and existing.fetched_at and existing.fetched_at >= cutoff \
                and existing.fetch_status != "error":
            skipped += 1
            continue
        try:
            data = fetch_fn(address)
        except Exception as exc:  # noqa: BLE001  (never let a fetch break the audit)
            data = {"address": address, "fetch_status": "error", "fetch_error": str(exc)[:300],
                    "fetched_at": datetime.utcnow()}
        row = _store(db, data)
        fetched += 1
        if row.fetch_status == "error":
            errors += 1
        if fetch_fn is fetch_one and _RATE_LIMIT_S:
            time.sleep(_RATE_LIMIT_S)
    db.commit()
    return {"fetched": fetched, "skipped_fresh": skipped, "errors": errors,
            "ttl_minutes": CACHE_TTL_MIN}


def as_dict(row: wm.PublicWalletProfile | None) -> dict | None:
    if row is None:
        return None
    return {
        "display_name": row.display_name, "pseudonym": row.pseudonym, "bio": row.bio,
        "profile_image": row.profile_image,
        "pnl_all": row.pnl_all, "pnl_1d": row.pnl_1d, "pnl_7d": row.pnl_7d, "pnl_30d": row.pnl_30d,
        "volume_all": row.volume_all, "position_value": row.position_value,
        "predictions": row.predictions, "biggest_win": row.biggest_win, "biggest_loss": row.biggest_loss,
        "largest_position_size": row.largest_position_size, "top_positions": row.top_positions,
        "fetch_status": row.fetch_status, "fetch_error": row.fetch_error,
        "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None,
    }
