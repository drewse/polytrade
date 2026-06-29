"""BTC Passive-Maker FORWARD research pipeline — research/paper ONLY.

Fixes the real bottleneck: the paper harness works, but the upstream conversion
stages (index_dataset → build_dataset → run_once) were manual, so paper quotes/fills
stopped growing. This worker chains them INCREMENTALLY and IDEMPOTENTLY so newly
ingested BTC markets become paper quotes/fills automatically — plus a full-funnel
diagnostic, fail-soft forward L2 capture, and an optional broad-universe pass.

HARD ISOLATION (asserted by tests): no order is ever placed; no import of live.py /
services.py / live_ranking.py / bankroll / approvals / copy trading. It only reads
the ingested Market/Trade tables + writes btc5m_* research rows. INERT unless
BTC_PASSIVE_MAKER_FORWARD_ENABLED=true.
"""
from __future__ import annotations

import os
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import btc5m
from . import btc5m_execution_lab as ex
from . import btc5m_models as bm
from . import btc5m_passive_maker as harness
from . import btc5m_passive_maker_models as pm
from . import btc5m_strategy_lab as lab
from . import btc5m_strategy_models as lm
from .models import Market, Trade

_mean = ex._mean
_std = ex._std
_clip = ex._clip


def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def get_config() -> dict:
    return {
        "enabled": _truthy(os.getenv("BTC_PASSIVE_MAKER_FORWARD_ENABLED", "false")),
        "index_window": int(os.getenv("BTC_PASSIVE_MAKER_INDEX_WINDOW", "200")),   # recent markets to (re)index
        "build_batch": int(os.getenv("BTC_PASSIVE_MAKER_BUILD_BATCH", "60")),      # new markets to build/cycle
        "broad_universe": _truthy(os.getenv("PASSIVE_MAKER_BROAD_UNIVERSE", "false")),
        "broad_batch": int(os.getenv("PASSIVE_MAKER_BROAD_BATCH", "40")),
    }


def _fwd_state(db: Session) -> pm.Btc5mForwardState:
    st = db.get(pm.Btc5mForwardState, 1)
    if st is None:
        st = pm.Btc5mForwardState(id=1)
        db.add(st)
        db.commit()
    return st


# ---------------------------------------------------------------------------
# incremental stages
# ---------------------------------------------------------------------------
def _index_new(db: Session, window: int) -> dict:
    """Stage A: index the recent window of BTC markets from the Market table into
    btc5m_* (idempotent upsert). Bounded — does NOT reprocess all history."""
    before = db.scalar(select(func.count()).select_from(bm.Btc5mMarket)) or 0
    try:
        res = btc5m.index_dataset(db, limit_markets=window)
        err = None
    except Exception as exc:  # noqa: BLE001
        res, err = {}, f"{type(exc).__name__}: {exc}"
    after = db.scalar(select(func.count()).select_from(bm.Btc5mMarket)) or 0
    return {"indexed_total": after, "new_indexed": after - before, "error": err, "raw": res}


def _build_new(db: Session, batch: int) -> dict:
    """Stage B: build lab points for resolved btc5m markets that have NO points yet —
    incremental, so we don't refetch Kraken for the whole dataset every cycle."""
    have_points = {m for (m,) in db.execute(select(lm.Btc5mLabPoint.market_id).distinct()).all()}
    resolved = [m for (m,) in db.execute(select(bm.Btc5mMarket.market_id)
                .where(bm.Btc5mMarket.resolved.is_(True))).all()]
    new_ids = [m for m in resolved if m not in have_points][:batch]
    if not new_ids:
        return {"new_built": 0, "built_market_ids": [], "error": None}
    try:
        res = lab.build_dataset(db, only_market_ids=set(new_ids))
        err = None
    except Exception as exc:  # noqa: BLE001
        res, err = {}, f"{type(exc).__name__}: {exc}"
    return {"new_built": res.get("markets_built", 0), "built_market_ids": new_ids, "error": err}


def settle_pending(db: Session) -> int:
    """Settle paper quotes whose market has since resolved (forward markets quoted while
    open). Most of our markets are already resolved at quote time, so this is usually a
    no-op — kept for correctness of true forward collection."""
    pend = db.scalars(select(pm.Btc5mPaperQuote).where(pm.Btc5mPaperQuote.settled.is_(False))).all()
    settled = 0
    for q in pend:
        mk = db.get(bm.Btc5mMarket, q.market_id)
        if mk and mk.resolved and mk.final_outcome is not None:
            up = btc5m._yes_no(mk.final_outcome) == "YES"
            q.market_resolved = True
            q.resolved_up = up
            if q.filled:
                won = up if q.side == "YES" else (not up)
                q.won = won
                q.realized_pnl = round((1.0 - q.fill_price) if won else -q.fill_price, 4)
            else:
                q.realized_pnl = 0.0
            q.settled = True
            settled += 1
    if settled:
        db.commit()
    return settled


def run_forward_cycle(db: Session, *, force: bool = False, full: bool = False) -> dict:
    """One forward cycle: index new → build new points → quote new → settle → recompute
    + diagnostics. INERT unless enabled (or `force`). `full=True` reprocesses a larger
    window (explicit opt-in). Places NO orders."""
    cfg = get_config()
    if not cfg["enabled"] and not force:
        return {"ran": False, "skipped": "disabled (BTC_PASSIVE_MAKER_FORWARD_ENABLED is false)"}
    fst = _fwd_state(db)
    window = max(cfg["index_window"], 800) if full else cfg["index_window"]
    batch = max(cfg["build_batch"], 400) if full else cfg["build_batch"]

    a = _index_new(db, window)
    b = _build_new(db, batch)
    # quote only the newly-built markets (incremental); run_once is force=True because the
    # forward worker is the gate for enablement (the harness flag may be separate).
    c = harness.run_once(db, force=True, only_market_ids=b["built_market_ids"] or None)
    settled = settle_pending(db)
    if cfg["broad_universe"]:
        broad = broad_universe_cycle(db, batch=cfg["broad_batch"])
    else:
        broad = {"ran": False}

    fst.runs += 1
    fst.last_run_at = datetime.utcnow()
    fst.last_error = a["error"] or b["error"]
    summary = {"new_indexed": a["new_indexed"], "new_points_markets": b["new_built"],
               "new_quotes": c.get("created", 0), "new_fills": c.get("filled", 0),
               "settled": settled, "broad": broad.get("created", 0) if broad.get("ran") else 0,
               "status": c.get("status")}
    fst.last_summary = summary
    fst.funnel = _funnel(db, prev=fst.funnel or {})
    db.commit()
    return {"ran": True, **summary, "funnel": fst.funnel}


# ---------------------------------------------------------------------------
# diagnostics funnel
# ---------------------------------------------------------------------------
def _latest(db, col, where=None):
    q = select(func.max(col))
    if where is not None:
        q = q.where(where)
    return db.scalar(q)


def _funnel(db: Session, *, prev: dict | None = None) -> dict:
    prev = prev or {}
    btc_in_market = len(btc5m.find_btc5m_markets(db, limit=4000))
    idx_total = db.scalar(select(func.count()).select_from(bm.Btc5mMarket)) or 0
    idx_resolved = db.scalar(select(func.count()).select_from(bm.Btc5mMarket)
                             .where(bm.Btc5mMarket.resolved.is_(True))) or 0
    lab_markets = db.scalar(select(func.count(func.distinct(lm.Btc5mLabPoint.market_id)))) or 0
    lab_points = db.scalar(select(func.count()).select_from(lm.Btc5mLabPoint)) or 0
    q_indep = db.scalar(select(func.count()).select_from(pm.Btc5mPaperQuote)
                        .where(pm.Btc5mPaperQuote.market_family == "btc",
                               pm.Btc5mPaperQuote.quote_kind == "independent")) or 0
    fills = db.scalar(select(func.count()).select_from(pm.Btc5mPaperQuote)
                      .where(pm.Btc5mPaperQuote.market_family == "btc",
                             pm.Btc5mPaperQuote.quote_kind == "independent",
                             pm.Btc5mPaperQuote.filled.is_(True))) or 0
    settled = db.scalar(select(func.count()).select_from(pm.Btc5mPaperQuote)
                        .where(pm.Btc5mPaperQuote.settled.is_(True))) or 0

    def stage(name, total, upstream=None, latest_ts=None):
        p = (prev.get(name) or {})
        blocked = upstream is not None and total < upstream     # input grew past output
        return {"total": total, "new_since_last": total - (p.get("total", total)),
                "latest_ts": latest_ts.isoformat() if latest_ts else None,
                "blocked": bool(blocked), "upstream": upstream}

    return {
        "1_btc_markets_in_main": stage("1_btc_markets_in_main", btc_in_market),
        "2_btc5m_indexed": stage("2_btc5m_indexed", idx_total, upstream=btc_in_market,
                                 latest_ts=_latest(db, bm.Btc5mMarket.indexed_at)),
        "2b_btc5m_resolved": stage("2b_btc5m_resolved", idx_resolved),
        "3_lab_markets": stage("3_lab_markets", lab_markets, upstream=idx_resolved),
        "3b_lab_points": stage("3b_lab_points", lab_points),
        "4_paper_quotes": stage("4_paper_quotes", q_indep, upstream=lab_markets,
                                latest_ts=_latest(db, pm.Btc5mPaperQuote.created_at)),
        "5_paper_fills": stage("5_paper_fills", fills),
        "6_settled_fills": stage("6_settled_fills", settled),
    }


def diagnostics(db: Session) -> dict:
    fst = _fwd_state(db)
    cfg = get_config()
    funnel = _funnel(db, prev=fst.funnel or {})
    blocked = [k for k, v in funnel.items() if v.get("blocked")]
    main = None
    try:
        from . import auto_worker
        main = auto_worker.status()
    except Exception:  # noqa: BLE001
        main = {"error": "unavailable"}
    return {
        "forward_enabled": cfg["enabled"], "broad_universe_enabled": cfg["broad_universe"],
        "runs": fst.runs, "last_run_at": fst.last_run_at.isoformat() if fst.last_run_at else None,
        "last_error": fst.last_error, "last_summary": fst.last_summary or {},
        "main_ingest": {"running": main.get("worker_running") if isinstance(main, dict) else None,
                        "last_cycle_at": main.get("last_worker_cycle_at") if isinstance(main, dict) else None},
        "funnel": funnel, "blocked_stages": blocked,
        "pipeline_blocked": bool(blocked),
        "safety": "research/paper only — converts ingested markets to PAPER quotes; never trades",
    }


# ---------------------------------------------------------------------------
# Part 5 — broader Polymarket binary universe (SEPARATE family + gate)
# ---------------------------------------------------------------------------
def _family_of(mk: Market) -> str:
    s = f"{(mk.slug or '')} {(mk.category or '')}".lower()
    if any(k in s for k in ("nba", "nfl", "mlb", "soccer", "ufc", "tennis", "game", "vs-")):
        return "sports"
    if any(k in s for k in ("election", "president", "senate", "congress", "trump", "biden", "poll")):
        return "politics"
    if any(k in s for k in ("eth", "solana", "crypto", "coin", "price")):
        return "crypto_other"
    return "other"


def _broad_signal(mk: Market, trades: list) -> dict | None:
    """Build the minimal sig dict simulate_queue needs from a generic binary market's
    trade stream (no BTC features). Pure spread-capture YES quote."""
    if mk.created_at is None or len(trades) < 6 or not mk.outcomes or len(mk.outcomes) != 2:
        return None
    trades = sorted(trades, key=lambda t: t.timestamp)
    t0 = mk.created_at

    def yp(tr):
        return tr.price if btc5m._yes_no(tr.outcome) == "YES" else (1.0 - tr.price)
    di = max(1, len(trades) // 3)
    dt = trades[di]
    t = int((dt.timestamp - t0).total_seconds())
    prior = [yp(x) for x in trades[: di + 1]]
    mid = prior[-1]
    half = max(_std(prior[-8:]), 0.01)
    future = [((int((x.timestamp - t0).total_seconds()) - t), yp(x), float(x.size or 0.0))
              for x in trades[di + 1:]]
    future = [f for f in future if 0 < f[0] <= 5]
    up = btc5m._yes_no(mk.resolved_outcome) == "YES"
    end = mk.resolved_at or dt.timestamp
    return {"market_id": mk.id, "t": t, "mid": _clip(mid, 0.02, 0.98), "half": _clip(half, 0.01, 0.2),
            "side": "YES", "up": up, "model_prob": mid, "regime": "broad",
            "secs_to_expiry": max(5, int((end - dt.timestamp).total_seconds())),
            "duration_minutes": 0, "btc_vol": 0.0, "volume_usd": float(mk.volume or 0.0),
            "flow_imbalance": 0.0, "btc_ret_sofar": 0.0,
            "created_ts": t0.timestamp(), "future": future}


def broad_universe_cycle(db: Session, *, batch: int = 40) -> dict:
    """Run the SAME worst-queue simulator on broader Polymarket binary markets to answer
    'does passive making work anywhere?'. Tagged market_family != 'btc' so it gets its
    OWN gate and never touches the BTC verdict. Paper only."""
    btc_ids = {m.id for m in btc5m.find_btc5m_markets(db, limit=4000)}
    already = {q for (q,) in db.execute(select(pm.Btc5mPaperQuote.market_id)
               .where(pm.Btc5mPaperQuote.market_family != "btc").distinct()).all()}
    cands = db.scalars(select(Market).where(Market.resolved.is_(True), Market.resolved_outcome.isnot(None))
                       .order_by(Market.resolved_at.desc()).limit(batch * 8)).all()
    created = filled = 0
    for mk in cands:
        if mk.id in btc_ids or mk.id in already:
            continue
        trades = db.scalars(select(Trade).where(Trade.market_id == mk.id)).all()
        sig = _broad_signal(mk, trades)
        if sig is None:
            continue
        q_usd = harness._queue_ahead_estimate([sig]) if sig["future"] else 25.0
        row = harness._quote_and_settle(db, sig, q_usd, kind="independent", family=_family_of(mk))
        created += 1
        filled += 1 if row.filled else 0
        if created >= batch:
            break
    harness.recompute_stats(db)
    return {"ran": True, "created": created, "filled": filled}


def status(db: Session) -> dict:
    return diagnostics(db)
