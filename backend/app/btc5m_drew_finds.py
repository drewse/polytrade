"""DREW FINDS — reverse-engineer specific Polymarket wallets + find similar BTC-5m
traders. Research/read-only.

Profiles two target wallets from public Polymarket APIs (data-api trades + lb-api
P&L), infers each one's strategy, then discovers OTHER wallets trading the same
BTC 5-minute up/down markets with similar behaviour and results by aggregating the
co-traders in the seed wallet's markets. Cross-references our own indexed btc5m
wallet analytics where they overlap.

100% read-only: it only GETs public Polymarket endpoints + reads our btc5m_* tables.
It NEVER places orders or touches live execution / bankroll / copy trading. All
network fetchers are injectable so tests run offline.
"""
from __future__ import annotations

import statistics
from datetime import datetime

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import btc5m_models as bm
from . import btc5m_drew_finds_models as dm

DATA_API = "https://data-api.polymarket.com"
LB_API = "https://lb-api.polymarket.com"
_TIMEOUT = 12.0

# The two wallets the user asked about.
TARGETS = [
    {"address": "0xf3a6ef82d0904db48c0ad8016ca62c556fee8c6c", "handle": "@std0", "label": "std0"},
    {"address": "0x2c335066fe58fe9237c3d3dc7b275c2a034a0563",
     "handle": "@0x2c33…-1759935795465", "label": "Substantial-Service"},
]


# ---------------------------------------------------------------------------
# public-API fetchers (injectable + fail-soft)
# ---------------------------------------------------------------------------
def fetch_trades(*, user: str | None = None, market: str | None = None, limit: int = 1000) -> list:
    params = {"limit": limit}
    if user:
        params["user"] = user
    if market:
        params["market"] = market
    try:
        with httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": "polytrade-research"}) as c:
            r = c.get(f"{DATA_API}/trades", params=params)
            r.raise_for_status()
            d = r.json()
            return d if isinstance(d, list) else []
    except Exception:  # noqa: BLE001  (read-only research; never raise)
        return []


def fetch_pnl(address: str) -> float | None:
    try:
        with httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": "polytrade-research"}) as c:
            r = c.get(f"{LB_API}/profit", params={"address": address, "window": "all"})
            r.raise_for_status()
            d = r.json()
            if isinstance(d, list) and d:
                return round(float(d[0].get("amount", 0.0)), 2)
    except Exception:  # noqa: BLE001
        return None
    return None


# ---------------------------------------------------------------------------
# slug categorisation + stats
# ---------------------------------------------------------------------------
def categorize(slug: str | None) -> str:
    s = (slug or "").lower()
    if s.startswith("btc-updown") or (("bitcoin" in s or "btc" in s) and "updown" in s) or "btc-updown-5m" in s:
        return "btc_5m_updown"
    if "-updown-5m" in s or "-up-or-down" in s:
        return "crypto_updown_other"     # eth/xrp/sol 5m up-down
    if "bitcoin" in s or "btc" in s:
        return "btc_other"
    if any(k in s for k in ("fifwc", "soccer", "nba", "nfl", "mlb", "nhl", "-vs-", "ucl", "epl", "ufc")):
        return "sports"
    return "other"


def _mean(xs):
    return round(sum(xs) / len(xs), 4) if xs else 0.0


def _wallet_stats(trades: list) -> dict:
    if not trades:
        return {"n_trades": 0}
    cats: dict = {}
    for t in trades:
        cats[categorize(t.get("slug"))] = cats.get(categorize(t.get("slug")), 0) + 1
    n = len(trades)
    buys = sum(1 for t in trades if t.get("side") == "BUY")
    prices = [float(t.get("price", 0)) for t in trades]
    sizes = [float(t.get("size", 0)) for t in trades]
    ts = [int(t.get("timestamp", 0)) for t in trades if t.get("timestamp")]
    span_days = max((max(ts) - min(ts)) / 86400, 0.01) if len(ts) >= 2 else 1.0
    btc5m = cats.get("btc_5m_updown", 0)
    return {
        "n_trades": n, "category_mix": dict(sorted(cats.items(), key=lambda kv: -kv[1])),
        "btc_5m_pct": round(100 * btc5m / n, 1), "buy_pct": round(100 * buys / n, 1),
        "avg_price": _mean(prices), "avg_size": round(_mean(sizes), 1),
        "trades_per_day": round(n / span_days, 1), "span_days": round(span_days, 1),
        "name": next((t.get("pseudonym") or t.get("name") for t in trades if t.get("pseudonym") or t.get("name")), "") or "",
    }


def _strategy(stats: dict, pnl: float | None) -> str:
    """Rule-based reverse-engineering of the wallet's strategy from its stats."""
    if stats.get("n_trades", 0) == 0:
        return "no public trades available"
    btc = stats["btc_5m_pct"]
    if btc >= 50:
        cheap = "buys the CHEAP side (avg %.2f — fades the move/underdog)" % stats["avg_price"] if stats["avg_price"] < 0.47 \
            else ("buys the FAVOURITE (avg %.2f — momentum)" % stats["avg_price"] if stats["avg_price"] > 0.55
                  else "quotes near the midpoint (avg %.2f)" % stats["avg_price"])
        freq = "high-frequency (%g trades/day)" % stats["trades_per_day"]
        sells = "holds to 5-min resolution" if stats["buy_pct"] > 80 else "actively sells/scalps early (%g%% buys)" % stats["buy_pct"]
        pl = ("net profitable +$%.0f all-time" % pnl) if (pnl and pnl > 0) else (("net negative $%.0f" % pnl) if pnl is not None else "P&L unknown")
        return (f"BTC 5-minute up/down specialist ({btc:.0f}% of flow) — {freq} scalper that {cheap}, {sells}. {pl}. "
                "Profile: short-horizon mean-reversion / value market-making on the 5-minute candle.")
    top = next(iter(stats["category_mix"]), "mixed")
    pl = ("realized +$%.0f" % pnl) if (pnl and pnl > 0) else (("$%.0f" % pnl) if pnl is not None else "P&L unknown")
    return (f"NOT a BTC-5m trader — concentrates in '{top}' ({stats['category_mix'].get(top,0)}/{stats['n_trades']} trades), "
            f"avg size {stats['avg_size']:g} shares ({pl}). Included for reference; excluded from the BTC-5m similarity search.")


def profile_wallet(target: dict, *, trades_fn=fetch_trades, pnl_fn=fetch_pnl) -> dict:
    trades = trades_fn(user=target["address"], limit=1000)
    stats = _wallet_stats(trades)
    pnl = pnl_fn(target["address"])
    return {**target, **stats, "all_time_pnl": pnl, "strategy": _strategy(stats, pnl)}


# ---------------------------------------------------------------------------
# find similar BTC-5m wallets (co-traders of the seed in the same markets)
# ---------------------------------------------------------------------------
def _seed_btc5m_markets(trades: list, limit: int = 10) -> list[str]:
    cids, seen = [], set()
    for t in trades:
        cid = t.get("conditionId")
        if categorize(t.get("slug")) == "btc_5m_updown" and cid and cid not in seen:
            seen.add(cid)
            cids.append(cid)
        if len(cids) >= limit:
            break
    return cids


def _similarity(cand: dict, seed: dict) -> float:
    """0..1 — how close a candidate is to the seed's behaviour (buy-cheap, HF, BTC-5m)."""
    px_close = max(0.0, 1 - abs(cand["avg_price"] - seed["avg_price"]) / 0.3)
    buy_close = max(0.0, 1 - abs(cand["buy_pct"] - seed["buy_pct"]) / 60.0)
    activity = min(1.0, cand["trades"] / 30.0)
    return round(0.45 * px_close + 0.25 * buy_close + 0.30 * activity, 3)


def find_similar(seed_trades: list, seed_stats: dict, *, trades_fn=fetch_trades, pnl_fn=fetch_pnl,
                 max_markets: int = 10, top: int = 12, min_trades: int = 3) -> list[dict]:
    cids = _seed_btc5m_markets(seed_trades, limit=max_markets)
    agg: dict = {}
    seed_addr = (seed_stats.get("address") or "").lower()
    for cid in cids:
        for t in trades_fn(market=cid, limit=500):
            w = (t.get("proxyWallet") or "").lower()
            if not w or w == seed_addr:
                continue
            a = agg.setdefault(w, {"wallet": w, "markets": set(), "trades": 0, "buys": 0,
                                   "px": [], "vol": 0.0, "name": ""})
            a["markets"].add(cid)
            a["trades"] += 1
            a["buys"] += 1 if t.get("side") == "BUY" else 0
            a["px"].append(float(t.get("price", 0)))
            a["vol"] += float(t.get("size", 0)) * float(t.get("price", 0))
            a["name"] = a["name"] or (t.get("pseudonym") or t.get("name") or "")
    cands = []
    for w, a in agg.items():
        if a["trades"] < min_trades:
            continue
        cands.append({"wallet": w, "name": a["name"], "markets_shared": len(a["markets"]),
                      "trades": a["trades"], "buy_pct": round(100 * a["buys"] / a["trades"], 1),
                      "avg_price": _mean(a["px"]), "volume_usd": round(a["vol"], 1)})
    # score similarity to the seed, fetch all-time P&L for the most-similar ones
    for c in cands:
        c["similarity"] = _similarity(c, seed_stats)
    cands.sort(key=lambda c: -c["similarity"])
    for c in cands[:top]:
        c["all_time_pnl"] = pnl_fn(c["wallet"])
    return cands[:top]


# ---------------------------------------------------------------------------
# our own indexed btc5m wallets (cross-reference: profitable specialists)
# ---------------------------------------------------------------------------
def our_btc5m_specialists(db: Session, *, top: int = 8) -> list[dict]:
    rows = db.scalars(select(bm.Btc5mWalletProfile)
                      .where(bm.Btc5mWalletProfile.profitable.is_(True))
                      .order_by(bm.Btc5mWalletProfile.realized_pnl.desc()).limit(top)).all()
    return [{"wallet": r.wallet_address, "realized_pnl": round(r.realized_pnl, 1), "roi": r.roi,
             "win_rate": r.win_rate, "trade_count": r.trade_count, "profit_factor": r.profit_factor,
             "avg_trade_size": round(r.avg_trade_size, 1), "cluster": r.cluster} for r in rows]


# ---------------------------------------------------------------------------
# orchestration + persistence
# ---------------------------------------------------------------------------
def _state(db: Session) -> dm.Btc5mDrewFindsState:
    st = db.get(dm.Btc5mDrewFindsState, 1)
    if st is None:
        st = dm.Btc5mDrewFindsState(id=1)
        db.add(st)
        db.commit()
    return st


def run(db: Session, *, trades_fn=fetch_trades, pnl_fn=fetch_pnl) -> dict:
    """Reverse-engineer the two targets + find similar BTC-5m wallets. Live read-only
    fetch from public Polymarket APIs (injectable for tests). Caches the report."""
    targets, seed_trades, seed_stats = [], None, None
    for tg in TARGETS:
        prof = profile_wallet(tg, trades_fn=trades_fn, pnl_fn=pnl_fn)
        targets.append(prof)
        if seed_trades is None and prof.get("btc_5m_pct", 0) >= 50:
            seed_trades = trades_fn(user=tg["address"], limit=1000)
            seed_stats = prof
    similar = []
    if seed_trades:
        similar = find_similar(seed_trades, seed_stats, trades_fn=trades_fn, pnl_fn=pnl_fn)
    report = {
        "generated_at": datetime.utcnow().isoformat(),
        "targets": targets,
        "seed_wallet": (seed_stats or {}).get("handle"),
        "similar_btc5m_wallets": similar,
        "our_indexed_specialists": our_btc5m_specialists(db),
        "summary": _summary(targets, similar),
        "safety": "research / read-only — profiles public wallets from public APIs; never trades",
    }
    st = _state(db)
    st.report = report
    st.built_at = datetime.utcnow()
    db.commit()
    return report


def _summary(targets: list, similar: list) -> str:
    btc = next((t for t in targets if t.get("btc_5m_pct", 0) >= 50), None)
    other = [t for t in targets if t is not btc]
    parts = []
    if btc:
        parts.append(f"{btc['handle']} is a BTC-5m scalper ({btc['btc_5m_pct']:.0f}% of flow, "
                     f"{btc.get('trades_per_day')}/day, avg entry {btc.get('avg_price')}, "
                     f"P&L {btc.get('all_time_pnl')}).")
    for o in other:
        parts.append(f"{o['handle']} — {o.get('strategy','')[:80]}")
    profitable_similar = [s for s in similar if (s.get('all_time_pnl') or 0) > 0]
    parts.append(f"Found {len(similar)} co-traders in the same BTC-5m markets; "
                 f"{len(profitable_similar)} are net-profitable all-time.")
    return " ".join(parts)


def status(db: Session) -> dict:
    st = _state(db)
    return {"report": st.report, "built_at": st.built_at.isoformat() if st.built_at else None,
            "targets_configured": [t["handle"] for t in TARGETS],
            "safety": "DREW FINDS — research / read-only; never trades"}
