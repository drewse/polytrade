"""Discovery 2.0 / Leaderboard Discovery.

Adds high-quality wallet SOURCES (Polymarket profit/volume leaderboards + top
holders of high-volume markets) on top of the existing recent-trades sampler,
and records WHERE each wallet was found plus a backfill priority.

SAFETY / ISOLATION:
  * Writes ONLY to the additive `discovery_sources` table. It NEVER creates or
    edits Wallet / WalletStat rows, so the production wallet universe — and thus
    eligibility, ranking, sizing, slippage, risk, pause/resume/halt, positions —
    is provably untouched. A discovered wallet becomes tradable only after the
    normal backfill + ranking + eligibility rules approve it.
  * Fetchers are injectable (default = real Polymarket APIs) and fail-closed, so
    the read endpoint and tests never depend on the network.
  * Future similar-wallet / leader-follower graph discovery can add new
    `discovery_source` values to the same table without touching this contract.
"""
from __future__ import annotations

from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import live_ranking
from .models import DiscoverySource, Market, Wallet, WalletStat

_LB = "https://lb-api.polymarket.com"
_DATA = "https://data-api.polymarket.com"
_HEADERS = {"User-Agent": "polytrade-discovery/1.0"}

# Backfill priority by (source, detail) — DETERMINISTIC. Monthly profit highest,
# down to recent-trades lowest (per spec).
_PRIORITY = {
    ("profit_leaderboard", "profit_30d"): 100,
    ("profit_leaderboard", "profit_all"): 95,
    ("profit_leaderboard", "profit_7d"): 90,
    ("profit_leaderboard", "profit_1d"): 80,
    ("top_holders", "*"): 70,
    ("volume_leaderboard", "*"): 60,
    ("recent_trades", "*"): 30,
}

# Leaderboard windows fetched, mapped to a source_detail. (today/week/month/all.)
_PROFIT_WINDOWS = [("30d", "profit_30d"), ("all", "profit_all"),
                   ("7d", "profit_7d"), ("1d", "profit_1d")]


def _priority(source: str, detail: str) -> int:
    return _PRIORITY.get((source, detail)) or _PRIORITY.get((source, "*")) or 10


def _score(priority: int, rank: int | None) -> float:
    """Deterministic 0..100 discovery score: source priority, minus a small
    rank penalty (rank 1 = full priority)."""
    r = max(1, rank or 1)
    return round(max(0.0, min(100.0, priority - (r - 1) * 0.4)), 1)


# ---- fetchers (real Polymarket APIs; injectable; fail-closed) --------------
def _get(url: str, params: dict):
    try:
        r = httpx.get(url, params=params, timeout=15, headers=_HEADERS)
        if r.status_code == 200:
            return r.json()
    except Exception:  # noqa: BLE001  (network/parse failure -> skip this source)
        pass
    return None


def fetch_profit_leaderboard(window: str = "30d", limit: int = 25):
    """[(proxyWallet, rank, pnl_usd)] from the profit leaderboard."""
    data = _get(f"{_LB}/profit", {"window": window, "limit": limit})
    return [(d["proxyWallet"], i + 1, float(d.get("amount") or 0))
            for i, d in enumerate(data or []) if d.get("proxyWallet")]


def fetch_volume_leaderboard(window: str = "30d", limit: int = 25):
    """[(proxyWallet, rank, volume_usd)] from the volume leaderboard."""
    data = _get(f"{_LB}/volume", {"window": window, "limit": limit})
    return [(d["proxyWallet"], i + 1, float(d.get("amount") or 0))
            for i, d in enumerate(data or []) if d.get("proxyWallet")]


def fetch_top_holders(condition_id: str, limit: int = 10):
    """[(proxyWallet, rank, amount, outcomeIndex)] — largest YES/NO holders."""
    data = _get(f"{_DATA}/holders", {"market": condition_id, "limit": limit})
    out, rank = [], 0
    for tok in (data or []):
        for h in tok.get("holders", []):
            rank += 1
            if h.get("proxyWallet"):
                out.append((h["proxyWallet"], rank, float(h.get("amount") or 0), h.get("outcomeIndex")))
    return out


def _wallets_with_stats(db: Session) -> set[str]:
    """Lowercased addresses of wallets that already have stats (NOT needing backfill)."""
    rows = db.execute(select(Wallet.address).join(WalletStat, WalletStat.wallet_id == Wallet.id)).all()
    return {a.lower() for (a,) in rows}


def refresh_discovery(db: Session, *, profit_fn=fetch_profit_leaderboard,
                      volume_fn=fetch_volume_leaderboard, holders_fn=fetch_top_holders,
                      per_source: int = 25, top_markets: int = 8, holders_per_market: int = 10) -> dict:
    """Fetch leaderboard + top-holder wallets and UPSERT discovery_sources.
    Writes nothing else (no Wallet/WalletStat). Returns a summary."""
    now = datetime.utcnow()
    with_stats = _wallets_with_stats(db)
    known_wallets = {a.lower() for (a,) in db.execute(select(Wallet.address)).all()}

    # collect (wallet, source, detail, rank)
    found: list[tuple[str, str, str, int | None]] = []
    for win, detail in _PROFIT_WINDOWS:
        for addr, rank, _pnl in (profit_fn(window=win, limit=per_source) or []):
            found.append((addr.lower(), "profit_leaderboard", detail, rank))
    for addr, rank, _vol in (volume_fn(window="30d", limit=per_source) or []):
        found.append((addr.lower(), "volume_leaderboard", "volume_30d", rank))
    top_mkts = db.scalars(select(Market).where(Market.resolved == False)  # noqa: E712
                          .order_by(Market.volume.desc().nullslast()).limit(top_markets)).all()
    for m in top_mkts:
        for addr, rank, _amt, _oidx in (holders_fn(m.id, limit=holders_per_market) or []):
            found.append((addr.lower(), "top_holders", f"holders:{m.id[:10]}", rank))

    existing = {(d.wallet_address, d.discovery_source, d.source_detail): d
                for d in db.scalars(select(DiscoverySource)).all()}
    seen, by_source = set(), {}
    new_rows = 0
    new_wallets = set()
    for addr, source, detail, rank in found:
        key = (addr, source, detail)
        if key in seen:          # dedup within this run
            continue
        seen.add(key)
        by_source[source] = by_source.get(source, 0) + 1
        if addr not in known_wallets and addr not in with_stats:
            new_wallets.add(addr)
        nb = addr not in with_stats
        pr = _priority(source, detail)
        sc = _score(pr, rank)
        row = existing.get(key)
        if row:                  # UPDATE discovery metadata only (never eligibility)
            row.last_seen = now
            row.source_rank = rank
            row.discovery_score = sc
            row.backfill_priority = pr
            row.needs_backfill = nb
        else:
            db.add(DiscoverySource(wallet_address=addr, discovery_source=source,
                                   source_detail=detail, source_rank=rank, discovery_score=sc,
                                   first_seen=now, last_seen=now, needs_backfill=nb,
                                   backfill_priority=pr,
                                   backfill_status=("skipped" if not nb else "pending")))
            new_rows += 1
    db.commit()
    return {
        "discovered": len(seen),
        "new_discovery_rows": new_rows,
        "new_wallets_queued": len(new_wallets),
        "needs_backfill": sum(1 for (a, _s, _d) in seen if a not in with_stats),
        "by_source": by_source,
        "note": "Discovery only — no wallet was made tradable; production eligibility unchanged.",
    }


def discovery_candidates(db: Session, *, limit: int = 300) -> dict:
    """READ-ONLY aggregation of discovery_sources per wallet, joined to current
    stats + production-eligibility status. Changes nothing."""
    eligible = {a.lower() for a in live_ranking.eligible_addresses(db)}
    ranked = {r["address"].lower(): r for r in live_ranking.rank_wallets(db, include_failed=True)}
    wallets = {w.address.lower(): w for w in db.scalars(select(Wallet)).all()}
    stats = {s.wallet_id: s for s in db.scalars(select(WalletStat)).all()}

    by_wallet: dict[str, list] = {}
    for d in db.scalars(select(DiscoverySource)).all():
        by_wallet.setdefault(d.wallet_address, []).append(d)

    out = []
    for addr, ds in by_wallet.items():
        w = wallets.get(addr)
        st = stats.get(w.id) if w else None
        rrow = ranked.get(addr) or {}
        is_elig = addr in eligible
        if is_elig:
            reason = "(production eligible)"
        elif st is None:
            reason = "needs backfill (no stats yet)"
        elif rrow and not rrow.get("eligible", False):
            reason = rrow.get("filter_reason") or "below production thresholds"
        else:
            reason = "outside production top-N"
        out.append({
            "wallet": addr,
            "discovery_score": round(max(d.discovery_score for d in ds), 1),
            "discovery_sources": sorted({d.discovery_source for d in ds}),
            "source_details": sorted({d.source_detail for d in ds}),
            "source_rank": min([d.source_rank for d in ds if d.source_rank is not None], default=None),
            "backfill_priority": max(d.backfill_priority for d in ds),
            "needs_backfill": st is None,                     # live: no stats -> still needs backfill
            # backfill-worker tracking (read-only display)
            "backfill_status": ds[0].backfill_status or "pending",
            "trades_imported": max((d.trades_imported or 0) for d in ds),
            "stats_updated": any(d.stats_updated for d in ds),
            "backfill_error": next((d.backfill_error for d in ds if d.backfill_error), None),
            "last_backfill_attempt_at": (lambda v: v.isoformat() if v else None)(
                max((d.last_backfill_attempt_at for d in ds if d.last_backfill_attempt_at), default=None)),
            "first_seen": min(d.first_seen for d in ds).isoformat(),
            "last_seen": max(d.last_seen for d in ds).isoformat(),
            # current wallet stats if available
            "roi": rrow.get("roi"), "profit_factor": rrow.get("profit_factor"),
            "win_rate": rrow.get("win_rate"), "settled_trades": rrow.get("num_settled"),
            # production eligibility (read-only)
            "production_eligible": is_elig,
            "reason_not_eligible": reason,
        })
    out.sort(key=lambda c: (c["backfill_priority"], c["discovery_score"]), reverse=True)
    out = out[:limit]
    return {
        "candidates": out,
        "summary": {
            "total": len(out),
            "needs_backfill": sum(1 for c in out if c["needs_backfill"]),
            "already_backfilled": sum(1 for c in out if not c["needs_backfill"]),
            "production_eligible": sum(1 for c in out if c["production_eligible"]),
            "by_source": {s: sum(1 for c in out if s in c["discovery_sources"])
                          for s in ("profit_leaderboard", "volume_leaderboard", "top_holders", "recent_trades")},
        },
        "read_only": True,
        "note": "Discovery analytics only — no wallet is tradable until normal backfill + ranking + eligibility approve it.",
    }
