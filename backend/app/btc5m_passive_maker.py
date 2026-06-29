"""BTC 5M Passive-Maker PAPER harness — research/paper ONLY.

Forward-collects PAPER quotes/fills for the 5-second passive-maker edge to grow the
sample beyond the 15 historical fills, so we can decide with confidence whether the
edge is real. A "fill" here is a SIMULATED paper fill inferred from the historical
trade stream under the conservative worst-case queue model — it is NEVER a real
order.

HARD ISOLATION GUARANTEES (enforced by construction, asserted by tests):
  * No CLOB order is ever created (there is no order-placement call in this module).
  * It never imports or touches live.py / services.py / live_ranking.py / bankroll /
    accounting / copy trading / approvals / open positions.
  * It writes only btc5m_paper_* research tables.
  * It is INERT unless BTC_PASSIVE_MAKER_PAPER_ENABLED=true (default false).

A hard-coded, pre-registered validation gate decides status. The harness can at best
reach 'paper_validated' — there is no path from here to live trading.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import btc5m_execution_lab as ex
from . import btc5m_maker_validation as mv
from . import btc5m_models as bm
from . import btc5m_passive_maker_models as pm

_mean = ex._mean
_std = ex._std
_clip = ex._clip

POLICY = "join_bid"            # the pre-registered winning policy
TIMEOUT_S = 5                  # 5-second rest window
QUEUE = "worst"               # conservative worst-case queue by default
DURATIONS = (5, 15)
MARKETS_PER_DAY = {5: 288, 15: 96}
GATE_MIN_FILLS = 100
GATE_MIN_P = 0.95
GATE_MAX_REGIME_SHARE = 0.60


def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def get_config() -> dict:
    return {"enabled": _truthy(os.getenv("BTC_PASSIVE_MAKER_PAPER_ENABLED", "false")),
            "max_quotes_per_run": int(os.getenv("BTC_PASSIVE_MAKER_MAX_PER_RUN", "300")),
            "capture_book": _truthy(os.getenv("BTC_PASSIVE_MAKER_CAPTURE_BOOK", "false")),
            "multi_point": _truthy(os.getenv("BTC_PASSIVE_MAKER_MULTI_POINT_QUOTES", "false"))}


def _state(db: Session) -> pm.Btc5mPaperMakerState:
    st = db.get(pm.Btc5mPaperMakerState, 1)
    if st is None:
        st = pm.Btc5mPaperMakerState(id=1)
        db.add(st)
        db.commit()
    return st


def _iso_week(ts: float) -> str:
    if not ts:
        return "?"
    d = datetime.fromtimestamp(ts, tz=timezone.utc)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


# ---------------------------------------------------------------------------
# forward collection: one cycle (scan → quote → 5s cancel → fill → settle)
# ---------------------------------------------------------------------------
def run_once(db: Session, *, force: bool = False, multi_point: bool | None = None,
             only_market_ids=None) -> dict:
    """One paper cycle. INERT (no-op) unless enabled, or `force` for direct tests.
    Creates ONE independent paper quote per not-yet-quoted BTC 5m/15m market (the
    earliest decision point), infers worst-case paper fills from the trade stream,
    settles resolved ones, recomputes stats + the gate. With `multi_point`, also adds
    up to 4 extra CORRELATED quotes per market (tagged separately so the gate never
    mixes them). Places NO orders. `only_market_ids` restricts to specific markets
    (forward pipeline incremental mode)."""
    cfg = get_config()
    if not cfg["enabled"] and not force:
        return {"ran": False, "skipped": "disabled (BTC_PASSIVE_MAKER_PAPER_ENABLED is false)"}
    multi_point = cfg["multi_point"] if multi_point is None else multi_point
    spec = next(iter(ex._models_to_test(db)), None)
    if spec is None:
        return {"ran": False, "skipped": "dataset too small / no model"}

    # signals across all splits, grouped by market and ordered by decision offset
    sigs = []
    for s in ("train", "val", "holdout"):
        sigs += ex.build_signals(db, s, spec["model"], spec["feats"], max_future=None)
    sigs = [s for s in sigs if s["duration_minutes"] in DURATIONS]
    if only_market_ids is not None:
        ids = set(only_market_ids)
        sigs = [s for s in sigs if s["market_id"] in ids]
    by_market: dict = {}
    for s in sigs:
        by_market.setdefault(s["market_id"], []).append(s)
    for v in by_market.values():
        v.sort(key=lambda s: s["t"])
    have_indep = {q for (q,) in db.execute(select(pm.Btc5mPaperQuote.market_id)
                  .where(pm.Btc5mPaperQuote.market_family == "btc",
                         pm.Btc5mPaperQuote.quote_kind == "independent").distinct()).all()}
    have_mp = {(m, di) for (m, di) in db.execute(select(pm.Btc5mPaperQuote.market_id, pm.Btc5mPaperQuote.decision_index)
               .where(pm.Btc5mPaperQuote.quote_kind == "multi_point").distinct()).all()}
    q_usd = _queue_ahead_estimate(sigs)

    created = filled = 0
    for mid, points in by_market.items():
        if mid not in have_indep:
            row = _quote_and_settle(db, points[0], q_usd, kind="independent", decision_index=0)
            created += 1
            filled += 1 if row.filled else 0
        if multi_point:
            for di, sig in enumerate(points[1:5], start=1):
                if (mid, di) not in have_mp:
                    row = _quote_and_settle(db, sig, q_usd, kind="multi_point", decision_index=di)
                    created += 1
                    filled += 1 if row.filled else 0
        if created >= cfg["max_quotes_per_run"]:
            break

    if cfg["capture_book"]:
        _capture_books(db, [p[0] for p in by_market.values()][:5])

    stats = recompute_stats(db)
    st = _state(db)
    st.last_run_at = datetime.utcnow()
    db.commit()
    return {"ran": True, "created": created, "filled": filled, "status": stats["status"],
            "total_quotes": stats["quotes"], "total_fills": stats["fills"]}


def _queue_ahead_estimate(sigs: list[dict]) -> float:
    sizes = [e[2] for s in sigs for e in s["future"] if len(e) > 2 and e[2] > 0]
    return round(sorted(sizes)[len(sizes) // 2], 2) if sizes else 25.0


def _quote_and_settle(db: Session, sig: dict, q_usd: float, *, kind: str = "independent",
                      decision_index: int = 0, family: str = "btc") -> pm.Btc5mPaperQuote:
    """Record a paper quote and settle it against the trade stream (worst-case queue).
    NO order is placed — `simulate_queue` only reads historical trades. `kind`/`family`
    keep correlated multi-point + broad-universe quotes OUT of the BTC gate."""
    res = ex.simulate_queue(sig, POLICY, timeout=TIMEOUT_S, mode=QUEUE, queue_ahead_usd=q_usd)
    m, half = sig["mid"], sig["half"]
    mk = db.get(bm.Btc5mMarket, sig["market_id"])
    tok = None
    if mk and mk.token_ids:
        tok = mk.token_ids[0] if sig["side"] == "YES" else (mk.token_ids[1] if len(mk.token_ids) > 1 else mk.token_ids[0])
    quote_price = _clip((m - half) + (0.01 if POLICY == "improve_bid" else 0.0), 0.01, 0.99) if sig["side"] == "YES" \
        else _clip((1 - m) - half + (0.01 if POLICY == "improve_bid" else 0.0), 0.01, 0.99)
    resolved = sig.get("up") is not None
    row = pm.Btc5mPaperQuote(
        market_id=sig["market_id"], token_id=tok, outcome=sig["side"], side=sig["side"],
        duration_minutes=sig["duration_minutes"], policy=POLICY,
        quote_price=round(quote_price, 4), best_bid=round(m - half, 4), best_ask=round(m + half, 4),
        spread=round(2 * half, 4), quote_t_offset_s=sig["t"], cancel_t_offset_s=sig["t"] + TIMEOUT_S,
        quote_lifetime_s=float(TIMEOUT_S), queue_assumption=QUEUE, queue_ahead_usd=q_usd,
        regime=sig.get("regime"), week=_iso_week(sig.get("created_ts", 0)),
        market_family=family, quote_kind=kind, decision_index=decision_index,
        market_resolved=resolved, resolved_up=sig.get("up"))
    if res["filled"]:
        row.status = "filled"
        row.filled = True
        row.fill_delay_s = res["delay"]
        row.fill_t_offset_s = int(sig["t"] + (res["delay"] or 0))
        row.fill_price = res["entry"]
        row.fill_evidence = f"worst-queue: a trade printed through {row.quote_price} within {TIMEOUT_S}s"
        row.spread_captured = res["spread_captured"]
        if resolved:
            row.realized_pnl = res["pnl"]
            row.won = res["win"]
            row.settled = True
    else:
        row.status = "expired"
        row.reason_not_filled = f"no trade printed through {row.quote_price} within {TIMEOUT_S}s (worst-case queue)"
        row.realized_pnl = 0.0
        row.settled = resolved
    db.add(row)
    db.commit()
    return row


# ---------------------------------------------------------------------------
# L2 book snapshot capture (fail-soft, read-only)
# ---------------------------------------------------------------------------
def _capture_books(db: Session, sigs: list[dict]) -> None:
    for sig in sigs:
        capture_book(db, sig["market_id"])


def capture_book(db: Session, market_id: str) -> pm.Btc5mPaperBookSnapshot:
    """Attempt a read-only CLOB L2 book snapshot. Fail-soft — stores an error row when
    the book is unavailable (e.g. the market already resolved). Never places anything."""
    mk = db.get(bm.Btc5mMarket, market_id)
    tok = (mk.token_ids[0] if mk and mk.token_ids else None)
    snap = pm.Btc5mPaperBookSnapshot(market_id=market_id, token_id=tok, source="clob")
    try:
        if not tok:
            raise RuntimeError("no token id")
        with httpx.Client(timeout=5.0) as c:
            r = c.get("https://clob.polymarket.com/book", params={"token_id": tok})
            r.raise_for_status()
            book = r.json()
        bids = [[float(b["price"]), float(b["size"])] for b in (book.get("bids") or [])][:10]
        asks = [[float(a["price"]), float(a["size"])] for a in (book.get("asks") or [])][:10]
        snap.bid_levels = bids
        snap.ask_levels = asks
        snap.best_bid = bids[0][0] if bids else None
        snap.best_ask = asks[0][0] if asks else None
        snap.spread = round((snap.best_ask - snap.best_bid), 4) if (snap.best_bid and snap.best_ask) else None
        snap.depth_at_quote = bids[0][1] if bids else None
    except Exception as exc:  # noqa: BLE001  (fail-soft: book often unavailable)
        snap.error = f"{type(exc).__name__}: {exc}"[:300]
    db.add(snap)
    db.commit()
    return snap


# ---------------------------------------------------------------------------
# cumulative stats + the PRE-REGISTERED validation gate
# ---------------------------------------------------------------------------
def _settled_fills(db: Session, *, family: str = "btc", kind: str = "independent") -> list[dict]:
    """Settled paper fills for ONE market family + quote kind. The BTC gate uses only
    family='btc', kind='independent' — correlated multi-point and broad-universe fills
    are never mixed in."""
    q = select(pm.Btc5mPaperQuote).where(pm.Btc5mPaperQuote.filled.is_(True),
                                         pm.Btc5mPaperQuote.settled.is_(True))
    if family is not None:
        q = q.where(pm.Btc5mPaperQuote.market_family == family)
    if kind is not None:
        q = q.where(pm.Btc5mPaperQuote.quote_kind == kind)
    rows = db.scalars(q).all()
    return [{"pnl": r.realized_pnl or 0.0, "spread_captured": r.spread_captured or 0.0,
             "won": bool(r.won), "regime": r.regime or "?", "week": r.week or "?",
             "resolved_up": r.resolved_up, "side": r.side} for r in rows]


def evaluate_gate(fills: list[dict]) -> tuple[str, dict]:
    """Hard-coded, pre-registered gate. We cannot 'promote' unless ALL conditions pass.
    Returns (status, per-condition dict). The best status is 'paper_validated' — there
    is no live path."""
    n = len(fills)
    pnls = [f["pnl"] for f in fills]
    boot = mv.phase_d_bootstrap(fills) if n >= 4 else {"ok": False}
    p = boot.get("prob_true_ev_positive") if boot.get("ok") else 0.0
    ci = boot.get("ev_per_fill", {}).get("ci95") if boot.get("ok") else None
    weeks = {f["week"] for f in fills if f["week"] != "?"}
    # per-week positivity
    week_ev = {}
    for f in fills:
        week_ev.setdefault(f["week"], []).append(f["pnl"])
    pos_weeks = [w for w, v in week_ev.items() if w != "?" and _mean(v) > 0]
    # regime concentration of POSITIVE EV
    reg_ev = {}
    for f in fills:
        reg_ev.setdefault(f["regime"], 0.0)
        reg_ev[f["regime"]] += f["pnl"]
    total_pos = sum(v for v in reg_ev.values() if v > 0) or 1.0
    max_share = max((v / total_pos for v in reg_ev.values() if v > 0), default=0.0)
    # exclude the 5 best fills, EV still positive
    ex_top5 = sorted(pnls, reverse=True)[5:]
    cond = {
        "min_100_fills": n >= GATE_MIN_FILLS,
        "prob_ev_positive_ge_0.95": bool(p >= GATE_MIN_P),
        "ci_strictly_above_zero": bool(ci and ci[0] > 0),
        "stable_across_2_weeks": len(weeks) >= 2 and len(pos_weeks) >= 2,
        "worst_queue_positive": _mean(pnls) > 0 if pnls else False,
        "no_regime_over_60pct": max_share <= GATE_MAX_REGIME_SHARE,
        "ev_positive_excluding_top5": (_mean(ex_top5) > 0) if len(ex_top5) >= 1 else False,
    }
    if all(cond.values()):
        status = "paper_validated"
    elif n >= GATE_MIN_FILLS and (_mean(pnls) <= 0 or (p < 0.85 and (ci and ci[0] <= 0))):
        status = "failed_validation"
    else:
        status = "research_only_not_validated"
    return status, cond


def recompute_stats(db: Session) -> dict:
    """Recompute the canonical BTC gate (family='btc', kind='independent' ONLY).
    Correlated multi-point + broad-universe results are tracked separately and never
    enter this gate."""
    st = _state(db)
    base = pm.Btc5mPaperQuote.market_family == "btc"
    indep = base & (pm.Btc5mPaperQuote.quote_kind == "independent")
    n_quotes = db.scalar(select(func.count()).select_from(pm.Btc5mPaperQuote).where(indep)) or 0
    n_fills = db.scalar(select(func.count()).select_from(pm.Btc5mPaperQuote)
                        .where(indep, pm.Btc5mPaperQuote.filled.is_(True))) or 0
    n_skip = db.scalar(select(func.count()).select_from(pm.Btc5mPaperQuote)
                       .where(pm.Btc5mPaperQuote.status == "skipped")) or 0
    fills = _settled_fills(db, family="btc", kind="independent")
    pnls = [f["pnl"] for f in fills]
    caps = [f["spread_captured"] for f in fills]
    boot = mv.phase_d_bootstrap(fills) if len(fills) >= 4 else {"ok": False}
    # adverse selection at cohort level: unconditional win rate vs filled win rate
    all_signals_win = [1 if f["won"] else 0 for f in fills]
    uncond = _mean([1 if (f["resolved_up"] if f["side"] == "YES" else (not f["resolved_up"])) else 0 for f in fills]) if fills else 0.0
    adverse = round(uncond - _mean(all_signals_win), 4) if fills else 0.0
    # fills/day estimate (worst-queue fill rate × cadence)
    fr = (n_fills / n_quotes) if n_quotes else 0.0
    fpd = round(sum(MARKETS_PER_DAY.values()) * fr, 1)
    status, gate = evaluate_gate(fills)

    st.status = status
    st.quotes = n_quotes
    st.fills = n_fills
    st.skipped = n_skip
    st.fill_rate = round(fr, 4)
    st.ev_per_fill = round(_mean(pnls), 5) if pnls else 0.0
    st.ev_per_day_estimate = round(fpd * (_mean(pnls) if pnls else 0.0), 4)
    st.prob_ev_positive = boot.get("prob_true_ev_positive", 0.0) if boot.get("ok") else 0.0
    ci = boot.get("ev_per_fill", {}).get("ci95") if boot.get("ok") else None
    st.ci_low = ci[0] if ci else 0.0
    st.ci_high = ci[1] if ci else 0.0
    st.spread_captured = round(_mean(caps), 5) if caps else 0.0
    st.adverse_selection = adverse
    st.weeks_covered = len({f["week"] for f in fills if f["week"] != "?"})
    st.gate = gate
    db.commit()
    return {"status": status, "quotes": n_quotes, "fills": n_fills, "settled_fills": len(fills),
            "ev_per_fill": st.ev_per_fill, "prob_ev_positive": st.prob_ev_positive, "gate": gate}


# ---------------------------------------------------------------------------
# read APIs
# ---------------------------------------------------------------------------
def _cohort_summary(db: Session, family, kind) -> dict:
    """Independent stats + an INDEPENDENT gate for one (family, kind) cohort. Used to
    report BTC-multi-point and broad-universe SEPARATELY from the canonical BTC gate."""
    n_q = db.scalar(select(func.count()).select_from(pm.Btc5mPaperQuote)
                    .where(pm.Btc5mPaperQuote.market_family == family,
                           pm.Btc5mPaperQuote.quote_kind == kind)) or 0
    fills = _settled_fills(db, family=family, kind=kind)
    pnls = [f["pnl"] for f in fills]
    boot = mv.phase_d_bootstrap(fills) if len(fills) >= 4 else {"ok": False}
    g_status, gate = evaluate_gate(fills)
    return {"family": family, "kind": kind, "quotes": n_q, "fills": len(fills),
            "ev_per_fill": round(_mean(pnls), 5) if pnls else 0.0,
            "prob_ev_positive": boot.get("prob_true_ev_positive", 0.0) if boot.get("ok") else 0.0,
            "gate_status": g_status, "gate_passed": sum(1 for v in gate.values() if v), "gate_total": len(gate) or 7}


def family_breakdown(db: Session) -> dict:
    """Every (family, kind) cohort, each with its OWN gate. The BTC edge is judged ONLY
    by ('btc','independent'); broad universe and multi-point get independent verdicts."""
    pairs = db.execute(select(pm.Btc5mPaperQuote.market_family, pm.Btc5mPaperQuote.quote_kind).distinct()).all()
    return {f"{fam}:{kind}": _cohort_summary(db, fam, kind) for (fam, kind) in sorted(pairs)}


def status(db: Session) -> dict:
    cfg = get_config()
    st = _state(db)
    gate = st.gate or {}
    return {
        "enabled": cfg["enabled"], "status": st.status,
        "config": {"policy": POLICY, "timeout_s": TIMEOUT_S, "queue": QUEUE, "durations": list(DURATIONS),
                   "multi_point": cfg["multi_point"], "capture_book": cfg["capture_book"]},
        "gate_cohort": "btc:independent",
        "quotes": st.quotes, "fills": st.fills, "skipped": st.skipped, "fill_rate": st.fill_rate,
        "ev_per_fill": st.ev_per_fill, "ev_per_day_estimate": st.ev_per_day_estimate,
        "prob_ev_positive": st.prob_ev_positive, "ci95": [st.ci_low, st.ci_high],
        "spread_captured": st.spread_captured, "adverse_selection": st.adverse_selection,
        "weeks_covered": st.weeks_covered,
        "gate": gate, "gate_progress": {"passed": sum(1 for v in gate.values() if v), "total": len(gate) or 7},
        "fills_target": GATE_MIN_FILLS,
        "family_breakdown": family_breakdown(db),
        "l2_book": _book_status(db),
        "last_run_at": st.last_run_at.isoformat() if st.last_run_at else None,
        "safety": ("BTC 5M Passive-Maker PAPER harness — research/paper only; simulates quotes/fills from the "
                   "historical trade stream; NEVER places orders or touches live execution / bankroll / copy trading"),
    }


def _book_status(db: Session) -> dict:
    n = db.scalar(select(func.count()).select_from(pm.Btc5mPaperBookSnapshot)) or 0
    ok = db.scalar(select(func.count()).select_from(pm.Btc5mPaperBookSnapshot)
                   .where(pm.Btc5mPaperBookSnapshot.error.is_(None))) or 0
    last = db.scalar(select(pm.Btc5mPaperBookSnapshot).order_by(pm.Btc5mPaperBookSnapshot.captured_at.desc()))
    return {"snapshots": n, "with_book": ok, "errors": n - ok,
            "last_error": (last.error if last else None), "capture_enabled": get_config()["capture_book"]}


def quotes(db: Session, *, limit: int = 50) -> dict:
    rows = db.scalars(select(pm.Btc5mPaperQuote).order_by(pm.Btc5mPaperQuote.created_at.desc()).limit(limit)).all()
    def row(q):
        return {"market_id": q.market_id, "token_id": q.token_id, "side": q.side, "policy": q.policy,
                "duration_minutes": q.duration_minutes, "quote_price": q.quote_price, "best_bid": q.best_bid,
                "best_ask": q.best_ask, "spread": q.spread, "quote_t_offset_s": q.quote_t_offset_s,
                "cancel_t_offset_s": q.cancel_t_offset_s, "queue_assumption": q.queue_assumption,
                "status": q.status, "filled": q.filled, "fill_price": q.fill_price, "fill_delay_s": q.fill_delay_s,
                "reason_not_filled": q.reason_not_filled, "reason_skipped": q.reason_skipped,
                "realized_pnl": q.realized_pnl, "spread_captured": q.spread_captured, "won": q.won,
                "regime": q.regime, "week": q.week}
    return {"quotes": [row(q) for q in rows], "safety": "paper only — no orders"}


def fills(db: Session, *, limit: int = 50) -> dict:
    rows = db.scalars(select(pm.Btc5mPaperQuote).where(pm.Btc5mPaperQuote.filled.is_(True))
                      .order_by(pm.Btc5mPaperQuote.created_at.desc()).limit(limit)).all()
    def row(q):
        return {"market_id": q.market_id, "side": q.side, "fill_price": q.fill_price,
                "fill_delay_s": q.fill_delay_s, "fill_evidence": q.fill_evidence, "spread_captured": q.spread_captured,
                "realized_pnl": q.realized_pnl, "won": q.won, "regime": q.regime, "week": q.week,
                "queue_assumption": q.queue_assumption, "settled": q.settled}
    return {"fills": [row(q) for q in rows], "safety": "paper only — simulated fills from the trade stream"}
