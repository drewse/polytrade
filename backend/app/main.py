"""FastAPI application exposing the dashboard API."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import attribution, auto_worker, btc5m, btc5m_micro_test, btc5m_micro_test_models, btc5m_micro_test_worker, btc5m_models, btc5m_onchain_models, btc5m_onchain_source, btc5m_strategy_models, challenger, challenger_models, deep_backfill, discovery, live, market_intel, market_intel_models, research, research_models, services, top20, wallet_approval, wallet_approval_models, wallet_audit, wallet_audit_models  # noqa: F401  (model imports register tables for create_all)
# aliased: the btc5m research module already defines an endpoint function named
# `btc5m_strategy_lab` (for /api/btc5m/strategy-lab), which would shadow the module.
from . import btc5m_strategy_lab as strat_lab  # noqa: E402
from . import btc5m_alpha_research as research_lab  # noqa: E402
from . import btc5m_alpha_discovery as discovery_lab  # noqa: E402
from . import btc5m_execution_lab as execution_lab  # noqa: E402
from . import btc5m_maker_validation as maker_validation  # noqa: E402
from . import btc5m_passive_maker as passive_maker  # noqa: E402
from . import btc5m_passive_maker_forward as passive_maker_forward  # noqa: E402
from . import btc5m_passive_maker_models  # noqa: F401,E402  (register paper tables for create_all)
from . import btc5m_drew_finds as drew_finds  # noqa: E402
from . import btc5m_drew_finds_models  # noqa: F401,E402  (register table for create_all)
from . import btc5m_longshot_lab as longshot_lab  # noqa: E402
from . import btc5m_longshot_models  # noqa: F401,E402  (register table for create_all)
from . import btc5m_live_maker as live_maker  # noqa: E402
from . import btc5m_live_maker_models  # noqa: F401,E402  (register tables for create_all)
from .db import get_db, init_db
from .models import (
    Backtest,
    BacktestResult,
    BacktestTrade,
    EquitySnapshot,
    Market,
    PaperPosition,
    PaperSignal,
    Wallet,
    WalletStat,
)
from .schemas import (
    BacktestConfig,
    BacktestListItem,
    BacktestOut,
    BacktestTradeOut,
    BackfillRequest,
    CandidateDetailOut,
    CandidateOut,
    DiscoveryRunRequest,
    EquityPoint,
    MarketOut,
    MessageOut,
    OverviewOut,
    PositionOut,
    SettingsOut,
    SettingsUpdate,
    SignalOut,
    SignalQualityOut,
    StatusOut,
    TopWallet,
    WalletAttributionOut,
    WalletCreate,
    WalletOut,
    WalletUpdate,
)
from .settings import config

app = FastAPI(title="Polymarket Copy Lab", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    db = next(get_db())
    try:
        services.ensure_settings(db)
        top20.ensure_strategies(db)
    finally:
        db.close()
    # CLOB SDK / execution-mode banner (so the deployed build's executor stack is
    # visible in logs). Guarded — never blocks startup.
    try:
        info = live.sdk_info()
        print(f"[startup] CLOB execution SDK: {info['sdk_package']}=={info['sdk_version']} "
              f"mode={info['clob_api_mode']} collateral={info['collateral']} "
              f"v2_installed={info['v2_sdk_installed']} archived_v1_present={info['archived_v1_present']}")
        cfg = live.get_config()
        real_trading = cfg.enabled and cfg.executor == "polymarket"
        if info["archived_v1_present"]:
            print("[startup] WARNING: archived py-clob-client (v1) is installed — real "
                  "trading fails closed until it is removed.")
            if real_trading:
                # HARD startup failure: refuse to boot a real-trading config on the
                # archived/non-functional v1 client.
                raise RuntimeError(
                    "archived py-clob-client (v1) is installed while LIVE_TRADING_ENABLED + "
                    "executor=polymarket — remove it and use py-clob-client-v2 only.")
        if real_trading and not info["v2_sdk_installed"]:
            raise RuntimeError("real trading configured but CLOB v2 SDK (py-clob-client-v2) is not installed")
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] SDK banner error: {exc}")
    # Start the in-process auto-ingest worker so live data refreshes itself
    # (guarded: one loop only; paper-trading only).
    auto_worker.start()
    # Start the background fill-reconciliation worker (accounting only — corrects
    # recorded fill prices/cost basis from the venue's actual fills; never trades).
    from . import fill_reconciler
    fill_reconciler.start()
    # Start the BTC 5M micro-test worker (separate daemon; inert unless ENABLED +
    # armed; paper-only unless an explicit live-place opt-in is set). Never starts
    # real trading by itself and never touches the production workers above.
    btc5m_micro_test_worker.start()
    # Start the nightly BTC 5M alpha-research worker (separate daemon; inert unless
    # BTC5M_RESEARCH_ENABLED; research/paper ONLY — retrains fair-value models +
    # writes a research report; no live-trading path exists in it).
    from . import btc5m_research_worker
    btc5m_research_worker.start()
    # Start the BTC passive-maker PAPER worker (separate daemon; inert unless
    # BTC_PASSIVE_MAKER_PAPER_ENABLED; forward-collects paper quotes/fills from the
    # historical trade stream — never places orders or touches live execution).
    from . import btc5m_passive_maker_worker
    btc5m_passive_maker_worker.start()
    # Start the forward conversion pipeline worker (separate daemon; inert unless
    # BTC_PASSIVE_MAKER_FORWARD_ENABLED; chains index->build->quote->settle on ingested
    # markets — research/paper only, never places orders).
    from . import btc5m_passive_maker_forward_worker
    btc5m_passive_maker_forward_worker.start()
    # Start the BTC 5M live-maker worker ONLY if BTC5M_LIVE_MAKER_ENABLED=true (default
    # false => no thread). Even when started it no-ops unless a live session is armed,
    # and every order passes hard caps + maker-only. This is the only real-money path.
    from . import btc5m_live_maker_worker
    btc5m_live_maker_worker.start()


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "polymarket-copy-lab", "paper_trading_only": True}


@app.get("/api/status", response_model=StatusOut)
def status(db: Session = Depends(get_db)) -> StatusOut:
    """Data-source health for the dashboard badges (mock/live/error/stale)."""
    return StatusOut(**services.get_ingest_status(db))


# ===========================================================================
# Overview
# ===========================================================================
@app.get("/api/overview", response_model=OverviewOut)
def overview(db: Session = Depends(get_db)) -> OverviewOut:
    settings = services.get_settings(db)
    starting = settings["bankroll"]
    bankroll = services.current_bankroll(db, settings)

    open_pos = list(db.scalars(select(PaperPosition).where(PaperPosition.status == "open")).all())
    closed_pos = list(
        db.scalars(select(PaperPosition).where(PaperPosition.status == "closed")).all()
    )
    unrealized = round(sum(p.unrealized_pnl for p in open_pos), 2)
    realized = round(sum(p.realized_pnl for p in closed_pos), 2)
    equity = round(bankroll + unrealized, 2)
    total_pnl = round(realized + unrealized, 2)
    roi = round((total_pnl / starting * 100) if starting else 0.0, 2)

    wins = sum(1 for p in closed_pos if p.realized_pnl > 0)
    win_rate = round((wins / len(closed_pos) * 100) if closed_pos else 0.0, 1)

    today = datetime.utcnow().date()
    signals_today = db.scalar(
        select(func.count()).select_from(PaperSignal).where(
            PaperSignal.created_at >= datetime(today.year, today.month, today.day)
        )
    )

    tracked_wallets = db.scalar(select(func.count()).select_from(Wallet))
    tracked_markets = db.scalar(select(func.count()).select_from(Market))

    # top copied wallets (by # of copied positions, then score)
    copied_counts = dict(
        db.execute(
            select(PaperPosition.wallet_id, func.count())
            .group_by(PaperPosition.wallet_id)
        ).all()
    )
    top: list[TopWallet] = []
    stat_rows = db.scalars(select(WalletStat).order_by(WalletStat.score.desc())).all()
    for stat in stat_rows:
        wallet = db.get(Wallet, stat.wallet_id)
        if not wallet:
            continue
        top.append(
            TopWallet(
                wallet_id=wallet.id,
                address=wallet.address,
                label=wallet.label,
                score=stat.score,
                classification=stat.classification,
                realized_roi=stat.realized_roi,
                copied_positions=int(copied_counts.get(wallet.id, 0)),
            )
        )
    top.sort(key=lambda w: (w.copied_positions, w.score), reverse=True)
    top = top[:5]

    curve_rows = db.scalars(
        select(EquitySnapshot).order_by(EquitySnapshot.timestamp.desc()).limit(100)
    ).all()
    curve = [
        EquityPoint(timestamp=s.timestamp, equity=s.equity, total_pnl=s.total_pnl)
        for s in reversed(curve_rows)
    ]

    return OverviewOut(
        bankroll=bankroll,
        starting_bankroll=starting,
        equity=equity,
        total_pnl=total_pnl,
        roi=roi,
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        open_positions=len(open_pos),
        closed_positions=len(closed_pos),
        win_rate=win_rate,
        signals_today=int(signals_today or 0),
        tracked_wallets=int(tracked_wallets or 0),
        tracked_markets=int(tracked_markets or 0),
        top_wallets=top,
        equity_curve=curve,
    )


# ===========================================================================
# Wallets
# ===========================================================================
@app.get("/api/wallets", response_model=list[WalletOut])
def list_wallets(db: Session = Depends(get_db)) -> list[Wallet]:
    wallets = db.scalars(select(Wallet)).all()
    # sort by score desc (wallets without stats go last)
    return sorted(
        wallets,
        key=lambda w: (w.stats.score if w.stats else -1),
        reverse=True,
    )


@app.post("/api/wallets", response_model=WalletOut, status_code=201)
def create_wallet(payload: WalletCreate, db: Session = Depends(get_db)) -> Wallet:
    existing = db.scalar(select(Wallet).where(Wallet.address == payload.address))
    if existing:
        raise HTTPException(status_code=409, detail="Wallet already tracked")
    wallet = Wallet(
        address=payload.address, label=payload.label, copy_enabled=payload.copy_enabled
    )
    db.add(wallet)
    db.commit()
    db.refresh(wallet)
    # compute stats from whatever trades we already have for this address
    services.recompute_wallet_stats(db, wallet)
    db.commit()
    return wallet


@app.post("/api/wallets/backfill", response_model=MessageOut)
def backfill_wallet(payload: BackfillRequest, db: Session = Depends(get_db)) -> MessageOut:
    """Pull a wallet's recent LIVE trade history and (re)score it (read-only).
    Requires data_mode=live; the resulting stats are marked partial_history."""
    result = services.backfill_wallet(db, payload.address, limit=payload.limit)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "backfill failed"))
    return MessageOut(message=f"Backfilled {payload.address}", detail=result)


@app.patch("/api/wallets/{wallet_id}", response_model=WalletOut)
def update_wallet(
    wallet_id: int, payload: WalletUpdate, db: Session = Depends(get_db)
) -> Wallet:
    wallet = db.get(Wallet, wallet_id)
    if wallet is None:
        raise HTTPException(status_code=404, detail="Wallet not found")
    if payload.label is not None:
        wallet.label = payload.label
    if payload.copy_enabled is not None:
        wallet.copy_enabled = payload.copy_enabled
    db.commit()
    db.refresh(wallet)
    return wallet


# ===========================================================================
# Signals
# ===========================================================================
@app.get("/api/signals", response_model=list[SignalOut])
def list_signals(limit: int = 100, db: Session = Depends(get_db)) -> list[SignalOut]:
    rows = db.scalars(
        select(PaperSignal).order_by(PaperSignal.created_at.desc()).limit(limit)
    ).all()
    out: list[SignalOut] = []
    for s in rows:
        out.append(
            SignalOut(
                id=s.id, wallet_id=s.wallet_id, market_id=s.market_id, outcome=s.outcome,
                side=s.side, observed_price=s.observed_price, suggested_entry=s.suggested_entry,
                confidence=s.confidence, reason=s.reason, copied=s.copied,
                created_at=s.created_at,
                wallet_address=s.wallet.address if s.wallet else None,
                market_question=s.market.question if s.market else None,
            )
        )
    return out


# ===========================================================================
# Positions
# ===========================================================================
@app.get("/api/positions", response_model=list[PositionOut])
def list_positions(status: str | None = None, db: Session = Depends(get_db)) -> list[PositionOut]:
    stmt = select(PaperPosition).order_by(PaperPosition.opened_at.desc())
    if status in ("open", "closed"):
        stmt = stmt.where(PaperPosition.status == status)
    rows = db.scalars(stmt).all()
    out: list[PositionOut] = []
    for p in rows:
        out.append(
            PositionOut(
                id=p.id, signal_id=p.signal_id, wallet_id=p.wallet_id, market_id=p.market_id,
                outcome=p.outcome, side=p.side, size=p.size, shares=p.shares,
                entry_price=p.entry_price, current_price=p.current_price, exit_price=p.exit_price,
                status=p.status, realized_pnl=p.realized_pnl, unrealized_pnl=p.unrealized_pnl,
                reason=p.reason, opened_at=p.opened_at, closed_at=p.closed_at,
                wallet_address=p.wallet.address if p.wallet else None,
                market_question=p.market.question if p.market else None,
            )
        )
    return out


@app.post("/api/positions/{position_id}/close", response_model=MessageOut)
def close_position(position_id: int, db: Session = Depends(get_db)) -> MessageOut:
    settings = services.get_settings(db)
    pos = services.close_position_manual(db, position_id, settings)
    if pos is None:
        raise HTTPException(status_code=404, detail="Open position not found")
    services.record_equity_snapshot(db)
    return MessageOut(
        message=f"Closed position #{position_id}",
        detail={"realized_pnl": pos.realized_pnl, "exit_price": pos.exit_price},
    )


# ===========================================================================
# Markets
# ===========================================================================
@app.get("/api/markets", response_model=list[MarketOut])
def list_markets(
    limit: int = 200, category: str | None = None, db: Session = Depends(get_db)
) -> list[Market]:
    stmt = select(Market).order_by(Market.volume.desc())
    if category:
        stmt = stmt.where(Market.category == category)
    return list(db.scalars(stmt.limit(limit)).all())


# ===========================================================================
# Settings
# ===========================================================================
@app.get("/api/settings", response_model=SettingsOut)
def read_settings(db: Session = Depends(get_db)) -> SettingsOut:
    return SettingsOut(**services.ensure_settings(db))


@app.patch("/api/settings", response_model=SettingsOut)
def patch_settings(payload: SettingsUpdate, db: Session = Depends(get_db)) -> SettingsOut:
    updates = payload.model_dump(exclude_none=True)
    return SettingsOut(**services.update_settings(db, updates))


# ===========================================================================
# Ingestion / seeding
# ===========================================================================
@app.post("/api/ingest/run", response_model=MessageOut)
def ingest_run(db: Session = Depends(get_db)) -> MessageOut:
    # Shares the worker's lock so a manual run can never overlap an auto cycle.
    result = auto_worker.run_one_cycle(wait=False)
    return MessageOut(message="Ingest cycle complete", detail=result)


# ===========================================================================
# Live execution validation (PAPER/DRY-RUN by default; gated by LIVE_TRADING_ENABLED)
# ===========================================================================
@app.get("/api/live/status")
def live_status(db: Session = Depends(get_db)) -> dict:
    return live.status(db)


@app.get("/api/live/executions")
def live_executions(limit: int = 100, db: Session = Depends(get_db)) -> dict:
    return {"executions": live.list_executions(db, limit)}


@app.get("/api/live/wallet-ranking")
def live_wallet_ranking(limit: int = 20, db: Session = Depends(get_db)) -> dict:
    """Production wallet ranking (profitability-first) used to select live trades.
    Exposes both copyability (legacy) and production_rank_score (new)."""
    from . import live_ranking
    return live_ranking.ranking_view(db, limit)


@app.post("/api/live/reconcile", response_model=MessageOut)
def live_reconcile(balance: float, db: Session = Depends(get_db)) -> MessageOut:
    """Reconcile computed bankroll against a manually-reported venue balance."""
    return MessageOut(message="reconciliation", detail=live.reconcile(db, balance))


@app.post("/api/live/reconcile-account", response_model=MessageOut)
def live_reconcile_account(db: Session = Depends(get_db)) -> MessageOut:
    """Refresh market resolution for open positions, settle any ended markets, and
    fetch the live venue balance — returns venue cash vs local bankroll, open
    exposure, realized/unrealized P/L. Read-only against the venue (no orders)."""
    return MessageOut(message="account reconciled", detail=live.reconcile_account(db))


@app.post("/api/live/reset-test-state", response_model=MessageOut)
def live_reset_test_state(db: Session = Depends(get_db)) -> MessageOut:
    """Clear rejected/dry-run test attempts + halt latch (never real orders)."""
    return MessageOut(message="test state reset", detail=live.reset_test_state(db))


@app.post("/api/live/set-bankroll", response_model=MessageOut)
def live_set_bankroll(amount: float, db: Session = Depends(get_db)) -> MessageOut:
    """Set tracked bankroll to the ACTUAL funded balance (clean slate only)."""
    return MessageOut(message="bankroll set", detail=live.set_bankroll(db, amount))


@app.post("/api/live/resume", response_model=MessageOut)
def live_resume(db: Session = Depends(get_db)) -> MessageOut:
    """Clear a tripped halt/pause and resume new orders. Returns the new status."""
    live.resume(db)
    return MessageOut(message="resumed", detail=live.status(db))


@app.post("/api/live/halt", response_model=MessageOut)
def live_halt(reason: str = "manual", db: Session = Depends(get_db)) -> MessageOut:
    """Hard halt (operator stop). Returns the new status."""
    live.halt(db, reason)
    return MessageOut(message="halted", detail=live.status(db))


@app.post("/api/live/pause", response_model=MessageOut)
def live_pause(db: Session = Depends(get_db)) -> MessageOut:
    """Pause trading (operator-initiated; same safety latch as halt, shown as
    'paused'). Returns the new status."""
    live.pause(db)
    return MessageOut(message="paused", detail=live.status(db))


@app.post("/api/live/run-once", response_model=MessageOut)
def live_run_once(db: Session = Depends(get_db)) -> MessageOut:
    """DIAGNOSTIC: runs the exact event-driven decision pipeline read-only (places
    nothing, writes no rows) and returns a complete decision report — signals seen,
    eligible count, per-candidate gates and the precise reason each signal was or
    was not acted on. Actual execution is event-driven in the worker. There is
    never again an unexplained 'placed=0'."""
    return MessageOut(message="live decision report", detail=live.run_pipeline(db, place=False))


@app.get("/api/live/decisions", response_model=MessageOut)
def live_decisions(limit: int = 100, db: Session = Depends(get_db)) -> MessageOut:
    """The per-signal execution audit trail (newest first)."""
    return MessageOut(message="live decisions", detail={"decisions": live.signal_decisions(db, limit)})


@app.get("/api/live/promotion-candidates", response_model=MessageOut)
def live_promotion_candidates(limit: int = 200, db: Session = Depends(get_db)) -> MessageOut:
    """READ-ONLY analytics 'farm system': wallets NOT in production that look
    promising from real signal history. Changes no trading logic, eligibility,
    ranking, sizing, or risk — purely informational."""
    from . import promotion
    return MessageOut(message="promotion candidates", detail=promotion.promotion_candidates(db, limit=limit))


@app.get("/api/live/shadow-portfolio", response_model=MessageOut)
def live_shadow_portfolio(limit: int = 200, db: Session = Depends(get_db)) -> MessageOut:
    """READ-ONLY simulation: what a copy of the promotion-candidate wallets WOULD
    have done. Places no orders, writes no executions/positions, changes no trading
    logic — every value is simulated."""
    from . import shadow
    return MessageOut(message="shadow portfolio", detail=shadow.shadow_portfolio(db, limit=limit))


@app.get("/api/live/discovery-candidates", response_model=MessageOut)
def live_discovery_candidates(limit: int = 300, db: Session = Depends(get_db)) -> MessageOut:
    """READ-ONLY: wallets discovered from leaderboards / top holders / recent
    trades, with backfill priority + eligibility status. Discovering a wallet
    never makes it tradable or changes production eligibility."""
    from . import discovery2
    return MessageOut(message="discovery candidates", detail=discovery2.discovery_candidates(db, limit=limit))


@app.post("/api/live/discovery/refresh", response_model=MessageOut)
def live_discovery_refresh(db: Session = Depends(get_db)) -> MessageOut:
    """Fetch Polymarket leaderboard + top-holder wallets into the discovery queue.
    Writes ONLY discovery metadata (discovery_sources) — never Wallet/WalletStat,
    so production eligibility, ranking, sizing, and live trading are unchanged."""
    from . import discovery2
    return MessageOut(message="discovery refreshed", detail=discovery2.refresh_discovery(db))


@app.post("/api/live/discovery-backfill/run-once", response_model=MessageOut)
def live_discovery_backfill_run_once(batch: int = 5, db: Session = Depends(get_db)) -> MessageOut:
    """Backfill the top `batch` queued discovery wallets (priority order) into
    WalletStat using existing backfill logic. Creates stats only — never forces
    eligibility or triggers a live trade; eligibility may change only via the
    unchanged ranking once stats exist."""
    from . import discovery_backfill
    return MessageOut(message="backfill batch", detail=discovery_backfill.run_backfill_batch(db, batch=batch))


@app.get("/api/live/discovery-backfill/status", response_model=MessageOut)
def live_discovery_backfill_status(db: Session = Depends(get_db)) -> MessageOut:
    """READ-ONLY backfill queue status: pending/running/completed/failed counts,
    latest errors, last run time, recently completed wallets."""
    from . import discovery_backfill
    return MessageOut(message="backfill status", detail=discovery_backfill.backfill_status(db))


@app.post("/api/live/rebaseline-bankroll", response_model=MessageOut)
def live_rebaseline_bankroll(payload: dict = Body(default={}),
                             db: Session = Depends(get_db)) -> MessageOut:
    """Guarded re-baseline of the local bankroll to venue reality. Requires
    {"confirm": true}. Writes ONLY bankroll + starting_bankroll (preserves realized
    P/L, open positions, executions, fills, history). Never trades."""
    return MessageOut(message="bankroll rebaseline",
                      detail=live.rebaseline_bankroll(db, confirm=bool(payload.get("confirm"))))


@app.post("/api/live/reconcile-fills", response_model=MessageOut)
def live_reconcile_fills(limit: int = 300, db: Session = Depends(get_db)) -> MessageOut:
    """HISTORICAL REPAIR: correct executions still recorded at the limit price using
    the venue's ACTUAL fills (fill price, cost basis, exposure, realized P/L,
    bankroll). Accounting only — never places or cancels orders."""
    return MessageOut(message="fill reconciliation", detail=live.reconcile_fills(db, limit=limit))


@app.post("/api/live/reconcile-pending", response_model=MessageOut)
def live_reconcile_pending(db: Session = Depends(get_db)) -> MessageOut:
    """Background-worker pass: reconcile executions flagged pending once their venue
    fills are available."""
    return MessageOut(message="pending fill reconciliation", detail=live.reconcile_pending(db))


@app.get("/api/live/reconciler-status", response_model=MessageOut)
def live_reconciler_status(db: Session = Depends(get_db)) -> MessageOut:
    from . import fill_reconciler
    detail = fill_reconciler.status()
    detail["pending_count"] = live.pending_reconciliation_count(db)
    return MessageOut(message="reconciler status", detail=detail)


@app.post("/api/live/deep-backfill/run-once", response_model=MessageOut)
def live_deep_backfill_run_once(batch: int = 3, max_pages: int | None = None,
                                db: Session = Depends(get_db)) -> MessageOut:
    """Deep-backfill the highest-priority wallets to improve coverage (data quality
    only). Resumable + idempotent. Never auto-approves or places orders."""
    return MessageOut(message="deep backfill batch",
                      detail=deep_backfill.run_deep_backfill(db, batch=batch, max_pages=max_pages))


@app.get("/api/live/deep-backfill/status", response_model=MessageOut)
def live_deep_backfill_status(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="deep backfill status", detail=deep_backfill.backfill_status(db))


@app.get("/api/live/approved-wallets", response_model=MessageOut)
def live_approved_wallets(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="approved wallets", detail=wallet_approval.approved_wallets(db))


@app.post("/api/live/wallet-approval/{address}", response_model=MessageOut)
def live_wallet_approval(address: str, action: str, note: str | None = None,
                         by: str | None = None, db: Session = Depends(get_db)) -> MessageOut:
    """Apply a manual control: disable | enable | approve | remove_approval | reject
    | watchlist | reset | request_backfill. Manual disable is a HARD override; no
    action makes a wallet copyable by itself (gates still apply)."""
    result = wallet_approval.set_status(db, address, action, by=by, note=note)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return MessageOut(message="wallet approval updated", detail=result)


@app.get("/api/live/wallet-approval-queue", response_model=MessageOut)
def live_wallet_approval_queue(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="wallet approval queue", detail=wallet_approval.approval_queue(db))


@app.get("/api/live/top-wallets-audit", response_model=MessageOut)
def live_top_wallets_audit(refresh_public: bool = False, force_refresh: bool = False,
                           db: Session = Depends(get_db)) -> MessageOut:
    """READ-ONLY audit of the production Top-N copied wallets: internal stats,
    rolling windows, cached PUBLIC Polymarket stats, score breakdown, and warnings.
    Never changes ranking/eligibility/execution. `refresh_public=true` re-fetches
    stale public profiles (bounded + rate-limited)."""
    return MessageOut(message="top wallets audit",
                      detail=wallet_audit.top_wallets_audit(db, refresh_public=refresh_public,
                                                            force_refresh=force_refresh))


@app.get("/api/live/top-wallets-audit/{address}", response_model=MessageOut)
def live_wallet_audit_detail(address: str, db: Session = Depends(get_db)) -> MessageOut:
    detail = wallet_audit.wallet_audit_detail(db, address)
    if detail is None:
        raise HTTPException(status_code=404, detail="wallet not found in ranking")
    return MessageOut(message="wallet audit detail", detail=detail)


@app.get("/api/live/sizing-simulation", response_model=MessageOut)
def live_sizing_simulation(limit: int = 1000, db: Session = Depends(get_db)) -> MessageOut:
    """READ-ONLY: compare legacy flat-$ vs new dynamic risk-aware sizing over recent
    historical signals (avg stake/shares before vs after, distribution by price).
    Places nothing; live execution logic untouched."""
    return MessageOut(message="sizing simulation", detail=live.sizing_simulation(db, limit=limit))


@app.get("/api/live/auth-check", response_model=MessageOut)
def live_auth_check() -> MessageOut:
    """READ-ONLY: validate the live API credentials with one authenticated GET
    (no order placed, no secrets exposed). Returns ok=true if L2 auth succeeds."""
    return MessageOut(message="live auth check", detail=live.auth_check())


@app.post("/api/admin/rescore-wallets", response_model=MessageOut)
def admin_rescore_wallets(db: Session = Depends(get_db)) -> MessageOut:
    """Recompute all wallet stats (incl. PF/expectancy/Sharpe/drawdown) and
    re-evaluate copyability with the revised formula. Paper-only research action."""
    n = services.recompute_all_wallet_stats(db, reconstruct=True)
    disc = services.run_discovery(db)
    return MessageOut(message=f"Rescored {n} wallets", detail=disc)


@app.post("/api/mock/seed", response_model=MessageOut)
def mock_seed(db: Session = Depends(get_db)) -> MessageOut:
    services.ensure_settings(db)
    result = services.seed_mock_data(db)
    return MessageOut(message="Seeded mock data", detail=result)


# ===========================================================================
# BTC 5M Reversal Lab — isolated READ-ONLY research module. Never submits orders,
# changes rankings/eligibility/discovery, or affects live trading.
# ===========================================================================
@app.get("/api/btc5m/dashboard", response_model=MessageOut)
def btc5m_dashboard(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m dashboard", detail=btc5m.dashboard(db))


@app.post("/api/btc5m/refresh", response_model=MessageOut)
def btc5m_refresh(limit_markets: int = 50, train: bool = True,
                  db: Session = Depends(get_db)) -> MessageOut:
    """Run one research cycle (index -> fingerprint -> train+promote -> shadow).
    Idempotent; read-only w.r.t. production. This is the 'small research batch'."""
    return MessageOut(message="btc5m research cycle", detail=btc5m.refresh(
        db, limit_markets=limit_markets, train=train))


@app.get("/api/btc5m/dataset", response_model=MessageOut)
def btc5m_dataset(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m dataset", detail=btc5m.dataset_summary(db))


@app.get("/api/btc5m/wallet-iq", response_model=MessageOut)
def btc5m_wallet_iq(limit: int = 50, db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m wallet IQ", detail={"cards": btc5m.wallet_iq_cards(db, limit=limit)})


@app.get("/api/btc5m/wallet-profiles", response_model=MessageOut)
def btc5m_wallet_profiles(limit: int = 200, db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m wallet profiles", detail={"profiles": btc5m.wallet_profiles(db, limit=limit)})


@app.get("/api/btc5m/clusters", response_model=MessageOut)
def btc5m_clusters(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m clusters", detail=btc5m.clusters(db))


@app.get("/api/btc5m/strategy-lab", response_model=MessageOut)
def btc5m_strategy_lab(scope: str = "global", db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m strategy lab", detail={
        "scope": scope, "leaderboard": btc5m.leaderboard(db, scope=scope),
        "feature_importance": btc5m.feature_importance(db, scope=scope)})


@app.get("/api/btc5m/consensus", response_model=MessageOut)
def btc5m_consensus(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m consensus", detail=btc5m.consensus(db))


@app.get("/api/btc5m/feature-importance", response_model=MessageOut)
def btc5m_feature_importance(scope: str = "global", db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m feature importance",
                      detail={"scope": scope, "feature_importance": btc5m.feature_importance(db, scope=scope)})


@app.get("/api/btc5m/shadow", response_model=MessageOut)
def btc5m_shadow(limit: int = 50, db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m shadow strategy", detail={
        "performance": btc5m.shadow_performance(db), "signals": btc5m.shadow_signals(db, limit=limit)})


@app.get("/api/btc5m/models", response_model=MessageOut)
def btc5m_models_leaderboard(scope: str = "global", db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m model leaderboard", detail={"leaderboard": btc5m.leaderboard(db, scope=scope)})


@app.get("/api/btc5m/research-notes", response_model=MessageOut)
def btc5m_research_notes(limit: int = 40, db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m research notes", detail={"notes": btc5m.research_notes(db, limit=limit)})


# ===========================================================================
# BTC 5M Micro-Test Mode — opt-in, minimum-size single-wallet live test, fully
# isolated from general live copy trading (separate table + accounting; reuses
# only the safe execution primitive). Default DISABLED + DISARMED.
# ===========================================================================
@app.get("/api/btc5m/micro-test/status", response_model=MessageOut)
def btc5m_micro_test_status(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m micro-test status", detail=btc5m_micro_test.status(db))


@app.post("/api/btc5m/micro-test/run-once", response_model=MessageOut)
def btc5m_micro_test_run_once(place: bool = False, db: Session = Depends(get_db)) -> MessageOut:
    """Run one micro-test cycle. place=false (default) => PAPER simulation;
    place=true => real execution via the shared safe path (per LIVE_EXECUTOR)."""
    btc5m_micro_test.settle(db)                      # settle resolved test positions first
    return MessageOut(message="btc5m micro-test run", detail=btc5m_micro_test.run_once(db, place=place))


@app.post("/api/btc5m/micro-test/arm", response_model=MessageOut)
def btc5m_micro_test_arm(by: str | None = None, db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m micro-test arm", detail=btc5m_micro_test.arm(db, by=by))


@app.post("/api/btc5m/micro-test/disarm", response_model=MessageOut)
def btc5m_micro_test_disarm(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m micro-test disarm", detail=btc5m_micro_test.disarm(db))


@app.post("/api/btc5m/micro-test/settle", response_model=MessageOut)
def btc5m_micro_test_settle(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m micro-test settle", detail=btc5m_micro_test.settle(db))


@app.get("/api/btc5m/micro-test/latency", response_model=MessageOut)
def btc5m_micro_test_latency(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m micro-test latency", detail=btc5m_micro_test.latency_stats(db))


# --- V3 Phase 1: on-chain OrderFilled detector (PAPER-ONLY measurement) ------
@app.get("/api/btc5m/onchain/status", response_model=MessageOut)
def btc5m_onchain_status(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m onchain status", detail=btc5m_onchain_source.status(db))


@app.post("/api/btc5m/onchain/start", response_model=MessageOut)
def btc5m_onchain_start(db: Session = Depends(get_db)) -> MessageOut:
    """Start the PAPER-ONLY on-chain detector loop. Never places orders."""
    return MessageOut(message="btc5m onchain start", detail=btc5m_onchain_source.start(db))


@app.post("/api/btc5m/onchain/stop", response_model=MessageOut)
def btc5m_onchain_stop(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m onchain stop", detail=btc5m_onchain_source.stop(db))


@app.post("/api/btc5m/onchain/run-once", response_model=MessageOut)
def btc5m_onchain_run_once(db: Session = Depends(get_db)) -> MessageOut:
    """Run one detector poll cycle (paper-only). Useful for manual measurement."""
    return MessageOut(message="btc5m onchain run-once", detail=btc5m_onchain_source.run_once(db))


@app.get("/api/btc5m/onchain/signals", response_model=MessageOut)
def btc5m_onchain_signals(limit: int = 50, db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m onchain signals", detail=btc5m_onchain_source.signals(db, limit=limit))


# --- BTC 5M Independent Strategy Lab (research/paper only) -------------------
def _lab_safe(fn, db):
    """Run a lab call; on error return a JSON error detail instead of a 500
    (research tool — never crash the API)."""
    try:
        return fn(db)
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        return {"error": f"{type(exc).__name__}: {exc}"}


@app.get("/api/btc5m/lab/status", response_model=MessageOut)
def btc5m_lab_status(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m strategy lab status", detail=_lab_safe(strat_lab.status, db))


@app.post("/api/btc5m/lab/build-dataset", response_model=MessageOut)
def btc5m_lab_build(limit_markets: int = 80, db: Session = Depends(get_db)) -> MessageOut:
    """Build the synchronized BTC-spot + Polymarket + order-flow dataset. Paper-only."""
    return MessageOut(message="btc5m lab build",
                      detail=_lab_safe(lambda d: strat_lab.build_dataset(d, limit_markets=limit_markets), db))


@app.post("/api/btc5m/lab/search", response_model=MessageOut)
def btc5m_lab_search(db: Session = Depends(get_db)) -> MessageOut:
    """Generate + backtest strategies (train/val/holdout), reject overfit, rank robust."""
    def _run(d):
        res = strat_lab.run_search(d)
        strat_lab.build_report(d)
        return res
    return MessageOut(message="btc5m lab search", detail=_lab_safe(_run, db))


@app.get("/api/btc5m/lab/leaderboard", response_model=MessageOut)
def btc5m_lab_leaderboard(limit: int = 40, db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m lab leaderboard", detail=strat_lab.leaderboard(db, limit=limit))


@app.get("/api/btc5m/lab/analyses", response_model=MessageOut)
def btc5m_lab_analyses(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m lab analyses", detail={
        "lag": strat_lab.lag_analysis(db),
        "large_trade": strat_lab.large_trade_analysis(db),
        "flow_imbalance": strat_lab.flow_imbalance_analysis(db),
        "edge_decay": strat_lab.edge_decay(db)})


@app.get("/api/btc5m/lab/report", response_model=MessageOut)
def btc5m_lab_report(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m lab report", detail=strat_lab.build_report(db))


# --- BTC 5M Alpha Research Platform (fair-value / ensemble / nightly) --------
# Quant research layer on top of the Strategy Lab dataset: estimates the true
# P(YES), measures EV-after-cost significance, and promotes only validated
# signals. 100% research/paper — never trades or touches live execution.
@app.get("/api/btc5m/research/status", response_model=MessageOut)
def btc5m_research_status(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m alpha research status",
                      detail=_lab_safe(research_lab.research_status, db))


@app.post("/api/btc5m/research/run", response_model=MessageOut)
def btc5m_research_run(build: bool = False, limit_markets: int = 60,
                       db: Session = Depends(get_db)) -> MessageOut:
    """Run the research pipeline: fair-value + ensemble + feature discovery +
    microstructure + cross-market + evolution + decay, then report. `build=true`
    rebuilds the dataset first (slower). Paper/research only."""
    return MessageOut(message="btc5m alpha research run",
                      detail=_lab_safe(lambda d: research_lab.run_pipeline(d, build=build, limit_markets=limit_markets), db))


@app.get("/api/btc5m/research/models", response_model=MessageOut)
def btc5m_research_models(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m research model leaderboard",
                      detail=_lab_safe(research_lab.model_leaderboard, db))


@app.get("/api/btc5m/research/report", response_model=MessageOut)
def btc5m_research_report(db: Session = Depends(get_db)) -> MessageOut:
    st = _lab_safe(research_lab.research_status, db)
    return MessageOut(message="btc5m research report", detail=(st or {}).get("research") if isinstance(st, dict) else st)


@app.get("/api/btc5m/research/worker", response_model=MessageOut)
def btc5m_research_worker_status() -> MessageOut:
    from . import btc5m_research_worker
    return MessageOut(message="btc5m research worker", detail=btc5m_research_worker.status())


# --- BTC 5M Alpha Discovery Engine (Phase 2: feature mining / meta-learning) -
# Mines new candidate features, scores them (IC / MI / SHAP / stability / decay),
# tracks them across generations, and runs a model lifecycle. Research/paper only.
@app.get("/api/btc5m/discovery/status", response_model=MessageOut)
def btc5m_discovery_status(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m alpha discovery status",
                      detail=_lab_safe(discovery_lab.discovery_status, db))


@app.post("/api/btc5m/discovery/run", response_model=MessageOut)
def btc5m_discovery_run(cross_assets: bool = True, db: Session = Depends(get_db)) -> MessageOut:
    """Run one discovery generation: mine features → registry → meta-learn → cross-asset.
    `cross_assets` fetches ETH/SOL (slower). Paper/research only."""
    return MessageOut(message="btc5m alpha discovery run",
                      detail=_lab_safe(lambda d: discovery_lab.run_discovery(d, cross_assets=cross_assets), db))


@app.post("/api/btc5m/discovery/nightly", response_model=MessageOut)
def btc5m_discovery_nightly(build: bool = False, cross_assets: bool = True,
                            db: Session = Depends(get_db)) -> MessageOut:
    """Full nightly run: Phase-1 fair-value/ensemble + Phase-2 discovery. Paper only."""
    return MessageOut(message="btc5m alpha nightly",
                      detail=_lab_safe(lambda d: discovery_lab.run_nightly(d, build=build, cross_assets=cross_assets), db))


@app.get("/api/btc5m/discovery/features", response_model=MessageOut)
def btc5m_discovery_features(limit: int = 60, db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m alpha feature registry",
                      detail=_lab_safe(lambda d: discovery_lab.feature_registry(d, limit=limit), db))


@app.get("/api/btc5m/discovery/models", response_model=MessageOut)
def btc5m_discovery_models(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m alpha model generations",
                      detail=_lab_safe(discovery_lab.model_generations, db))


# --- BTC 5M Execution Research Lab (Phase 3: passive-vs-market simulation) ----
# Simulates execution styles on historical signals/trades to test whether passive
# liquidity provision converts predictive-but-untradeable models into significant
# +EV. Research/paper only — no live execution path.
@app.get("/api/btc5m/execution/status", response_model=MessageOut)
def btc5m_execution_status(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m execution lab status",
                      detail=_lab_safe(execution_lab.execution_status, db))


@app.post("/api/btc5m/execution/run", response_model=MessageOut)
def btc5m_execution_run(db: Session = Depends(get_db)) -> MessageOut:
    """Simulate market vs passive execution, build the frontier, run the promotion
    experiment, and answer the research questions. Paper/research only."""
    return MessageOut(message="btc5m execution lab run",
                      detail=_lab_safe(execution_lab.run_execution_lab, db))


@app.post("/api/btc5m/execution/sweep", response_model=MessageOut)
def btc5m_execution_sweep(db: Session = Depends(get_db)) -> MessageOut:
    """Sweep passive resting windows × policies × BTC universes, measuring how per-fill
    EV and adverse-selection cost evolve as resting time grows. Paper/research only."""
    return MessageOut(message="btc5m execution rest-window sweep",
                      detail=_lab_safe(execution_lab.run_rest_window_sweep, db))


@app.post("/api/btc5m/execution/queue-study", response_model=MessageOut)
def btc5m_execution_queue_study(db: Session = Depends(get_db)) -> MessageOut:
    """Queue-position realism study for the 5s passive-maker edge: best/mid/worst queue
    assumptions, timeout sweep, regime breakdown, policy comparison, verdict. Paper/
    research only — promotes nothing, places no orders."""
    return MessageOut(message="btc5m execution queue study",
                      detail=_lab_safe(execution_lab.run_queue_study, db))


@app.get("/api/btc5m/execution/validation", response_model=MessageOut)
def btc5m_maker_validation_status(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m maker validation",
                      detail=_lab_safe(maker_validation.validation_status, db))


@app.post("/api/btc5m/execution/validate", response_model=MessageOut)
def btc5m_maker_validate(db: Session = Depends(get_db)) -> MessageOut:
    """Rigorously validate the fixed 5s passive-maker edge: stability, walk-forward,
    bootstrap P(EV>0), failure analysis, sensitivity. Paper/research only — no orders,
    no promotion, no live path."""
    return MessageOut(message="btc5m maker validation run",
                      detail=_lab_safe(maker_validation.run_validation, db))


# --- BTC 5M Passive-Maker PAPER harness (forward collection; research only) ---
# Simulates 5s passive quotes/fills from the historical trade stream to forward-grow
# the sample. INERT unless BTC_PASSIVE_MAKER_PAPER_ENABLED=true. NO endpoint places
# an order; there is no live execution path anywhere in this feature.
@app.get("/api/btc5m/passive-maker-paper/status", response_model=MessageOut)
def btc5m_passive_maker_status(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m passive-maker paper status", detail=_lab_safe(passive_maker.status, db))


@app.post("/api/btc5m/passive-maker-paper/run-once", response_model=MessageOut)
def btc5m_passive_maker_run_once(db: Session = Depends(get_db)) -> MessageOut:
    """Run one paper cycle. No-op unless BTC_PASSIVE_MAKER_PAPER_ENABLED=true. Places
    NO orders — paper fills are inferred from the historical trade stream."""
    return MessageOut(message="btc5m passive-maker paper run-once", detail=_lab_safe(passive_maker.run_once, db))


@app.get("/api/btc5m/passive-maker-paper/quotes", response_model=MessageOut)
def btc5m_passive_maker_quotes(limit: int = 50, db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m passive-maker paper quotes",
                      detail=_lab_safe(lambda d: passive_maker.quotes(d, limit=limit), db))


@app.get("/api/btc5m/passive-maker-paper/fills", response_model=MessageOut)
def btc5m_passive_maker_fills(limit: int = 50, db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m passive-maker paper fills",
                      detail=_lab_safe(lambda d: passive_maker.fills(d, limit=limit), db))


# --- Forward conversion pipeline (research-only; converts ingested -> paper) --
@app.get("/api/btc5m/passive-maker-forward/diagnostics", response_model=MessageOut)
def btc5m_passive_maker_forward_diag(db: Session = Depends(get_db)) -> MessageOut:
    """Full data-funnel diagnostics — shows exactly where the pipeline stalls."""
    return MessageOut(message="btc5m passive-maker forward diagnostics",
                      detail=_lab_safe(passive_maker_forward.diagnostics, db))


@app.post("/api/btc5m/passive-maker-forward/run-once", response_model=MessageOut)
def btc5m_passive_maker_forward_run_once(db: Session = Depends(get_db)) -> MessageOut:
    """Run one forward cycle (index new → build new points → quote → settle). No-op
    unless BTC_PASSIVE_MAKER_FORWARD_ENABLED=true. Places NO orders."""
    return MessageOut(message="btc5m passive-maker forward run-once",
                      detail=_lab_safe(passive_maker_forward.run_forward_cycle, db))


# --- DREW FINDS — reverse-engineer wallets + find similar BTC-5m traders ------
@app.get("/api/btc5m/drew-finds/status", response_model=MessageOut)
def btc5m_drew_finds_status(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m drew finds", detail=_lab_safe(drew_finds.status, db))


@app.post("/api/btc5m/drew-finds/run", response_model=MessageOut)
def btc5m_drew_finds_run(db: Session = Depends(get_db)) -> MessageOut:
    """Reverse-engineer the target wallets + find similar BTC-5m traders from public
    Polymarket APIs. Read-only research — never places orders."""
    return MessageOut(message="btc5m drew finds run", detail=_lab_safe(drew_finds.run, db))


# --- BTC 5M Longshot/Value Lab (cheap-side mispricing test; research only) -----
@app.get("/api/btc5m/longshot/status", response_model=MessageOut)
def btc5m_longshot_status(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m longshot lab", detail=_lab_safe(longshot_lab.status, db))


@app.post("/api/btc5m/longshot/run", response_model=MessageOut)
def btc5m_longshot_run(db: Session = Depends(get_db)) -> MessageOut:
    """Test whether buying the CHEAP side (favorite-longshot / value making) is +EV in
    our own data — calibration + mid/maker/taker × entry-threshold grid. Research only."""
    return MessageOut(message="btc5m longshot run", detail=_lab_safe(longshot_lab.run, db))


# --- BTC 5M LIVE MAKER trial (capped, maker-only, default-off) ----------------
# The ONLY real-money path. No order is sent unless BTC5M_LIVE_MAKER_ENABLED=true AND
# a session is armed in 'live' mode AND every cap passes. Shadow mode reads the real
# book but sends nothing.
@app.get("/api/btc5m/live-maker/status", response_model=MessageOut)
def btc5m_live_maker_status(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m live maker status", detail=_lab_safe(live_maker.status, db))


@app.post("/api/btc5m/live-maker/arm", response_model=MessageOut)
def btc5m_live_maker_arm(mode: str = "shadow", ttl_min: float = 20.0, max_orders: int = 0,
                         queue_lifetime_s: float | None = None,
                         db: Session = Depends(get_db)) -> MessageOut:
    """Arm a session. mode='shadow' reads the real book but sends NO orders; mode='live'
    is refused unless BTC5M_LIVE_MAKER_ENABLED=true and a key is configured. max_orders>0
    caps the session to that many orders (smoke test = 1) and auto-disarms once they
    reach a terminal state. queue_lifetime_s overrides how long each quote rests before
    cancel (the fill-probability lever) for this session only; null uses the env default."""
    return MessageOut(message="btc5m live maker arm",
                      detail=_lab_safe(lambda d: live_maker.arm(
                          d, mode=mode, ttl_min=ttl_min, max_orders=max_orders,
                          queue_lifetime_s=queue_lifetime_s), db))


@app.post("/api/btc5m/live-maker/disarm", response_model=MessageOut)
def btc5m_live_maker_disarm(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m live maker disarm",
                      detail=_lab_safe(lambda d: live_maker.disarm(d, reason="manual"), db))


@app.post("/api/btc5m/live-maker/kill", response_model=MessageOut)
def btc5m_live_maker_kill(db: Session = Depends(get_db)) -> MessageOut:
    """EMERGENCY: cancel all open orders + disarm + latch the kill flag."""
    return MessageOut(message="btc5m live maker KILL", detail=_lab_safe(live_maker.kill, db))


@app.post("/api/btc5m/live-maker/reset-kill", response_model=MessageOut)
def btc5m_live_maker_reset_kill(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m live maker reset-kill", detail=_lab_safe(live_maker.reset_kill, db))


@app.post("/api/btc5m/live-maker/reset-lock", response_model=MessageOut)
def btc5m_live_maker_reset_lock(db: Session = Depends(get_db)) -> MessageOut:
    """Manually clear the PERMANENT cumulative-loss lock (operator action)."""
    return MessageOut(message="btc5m live maker reset-lock", detail=_lab_safe(live_maker.reset_lock, db))


@app.post("/api/btc5m/live-maker/reconcile", response_model=MessageOut)
def btc5m_live_maker_reconcile(db: Session = Depends(get_db)) -> MessageOut:
    """Detect + cancel any orphan open orders from a previous run (read/cancel only)."""
    return MessageOut(message="btc5m live maker reconcile", detail=_lab_safe(live_maker.reconcile_open_orders, db))


@app.post("/api/btc5m/live-maker/check-connection", response_model=MessageOut)
def btc5m_live_maker_check_connection(db: Session = Depends(get_db)) -> MessageOut:
    """READ-ONLY: authenticate the wallet against the CLOB + read open orders. Places no
    order, cancels nothing. For pre-flight validation before enabling live."""
    return MessageOut(message="btc5m live maker check-connection", detail=_lab_safe(live_maker.check_connection, db))


@app.post("/api/btc5m/live-maker/run-cycle", response_model=MessageOut)
def btc5m_live_maker_run_cycle(db: Session = Depends(get_db)) -> MessageOut:
    """Drive one cycle manually (used for shadow dry-runs). No-op unless armed; live
    orders only if ENABLED + armed(live) + caps pass."""
    return MessageOut(message="btc5m live maker cycle", detail=_lab_safe(live_maker.run_cycle, db))


@app.get("/api/btc5m/live-maker/events", response_model=MessageOut)
def btc5m_live_maker_events(limit: int = 100, db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="btc5m live maker events",
                      detail=_lab_safe(lambda d: live_maker.events(d, limit=limit), db))


@app.get("/api/btc5m/live-maker/orders", response_model=MessageOut)
def btc5m_live_maker_orders(limit: int = 60, db: Session = Depends(get_db)) -> MessageOut:
    """Decision-level analytics for every order (the research dataset)."""
    return MessageOut(message="btc5m live maker orders",
                      detail=_lab_safe(lambda d: live_maker.orders(d, limit=limit), db))


@app.get("/api/btc5m/live-maker/summary", response_model=MessageOut)
def btc5m_live_maker_summary(session_id: int = 0, db: Session = Depends(get_db)) -> MessageOut:
    """Auto research summary for a session (latest if session_id omitted)."""
    return MessageOut(message="btc5m live maker summary",
                      detail=_lab_safe(lambda d: live_maker.session_summary(d, session_id=session_id or None), db))


# ===========================================================================
# Research Platform V1 — isolated PAPER-ONLY research on top of the BTC 5M Lab.
# Never places orders, changes production rankings/eligibility/discovery, or
# touches live trading/bankroll/execution.
# ===========================================================================
@app.get("/api/research/dashboard", response_model=MessageOut)
def research_dashboard(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="research dashboard", detail=research.dashboard(db))


@app.post("/api/research/cycle", response_model=MessageOut)
def research_cycle_run(limit_markets: int = 120, train: bool = True, mutate: bool = True,
                       db: Session = Depends(get_db)) -> MessageOut:
    """Run one full continuous-learning cycle (refresh -> seed -> mutate -> ensembles
    -> replay/paper-trade -> tournament -> hypotheses -> nightly review). Paper-only;
    reproducible; never touches live trading."""
    return MessageOut(message="research cycle", detail=research.research_cycle(
        db, limit_markets=limit_markets, train=train, mutate=mutate))


@app.post("/api/research/replay", response_model=MessageOut)
def research_replay(db: Session = Depends(get_db)) -> MessageOut:
    """Re-run the deterministic historical replay / paper trading for all strategies."""
    return MessageOut(message="replay", detail=research.replay_all(db))


@app.get("/api/research/strategies", response_model=MessageOut)
def research_strategies(status: str | None = None, limit: int = 300,
                        db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="strategy library",
                      detail={"strategies": research.strategy_library(db, status=status, limit=limit)})


@app.get("/api/research/strategies/{strategy_id}", response_model=MessageOut)
def research_strategy_detail(strategy_id: int, db: Session = Depends(get_db)) -> MessageOut:
    detail = research.strategy_detail(db, strategy_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    return MessageOut(message="strategy detail", detail=detail)


@app.get("/api/research/tournament", response_model=MessageOut)
def research_tournament(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="tournament", detail=research.tournament(db))


@app.get("/api/research/champion", response_model=MessageOut)
def research_champion(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="champion board", detail=research.champion_board(db))


@app.get("/api/research/hypotheses", response_model=MessageOut)
def research_hypotheses(limit: int = 60, db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="hypotheses", detail={"hypotheses": research.hypotheses(db, limit=limit)})


@app.get("/api/research/nightly-reviews", response_model=MessageOut)
def research_nightly_reviews(limit: int = 30, db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="nightly reviews", detail={"reviews": research.nightly_reviews(db, limit=limit)})


@app.get("/api/research/experiments", response_model=MessageOut)
def research_experiments(limit: int = 80, db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="experiments", detail={"experiments": research.experiments(db, limit=limit)})


# ===========================================================================
# Market Intelligence & Regime Engine V1 — isolated READ-ONLY analytics. Never
# trades, never changes execution/eligibility/ranking/discovery/bankroll.
# ===========================================================================
@app.get("/api/market-intel/dashboard", response_model=MessageOut)
def mi_dashboard(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="market intel dashboard", detail=market_intel.dashboard(db))


@app.post("/api/market-intel/run", response_model=MessageOut)
def mi_run(refresh_lab: bool = True, limit_markets: int = 150,
           db: Session = Depends(get_db)) -> MessageOut:
    """Run one Market-Intelligence batch (profiles -> regimes -> wallet/strategy
    specialization -> decay -> originality -> counterfactual -> recommendations ->
    nightly review). Read-only research; never trades."""
    return MessageOut(message="market intel batch", detail=market_intel.run_intel_batch(
        db, refresh_lab=refresh_lab, limit_markets=limit_markets))


@app.get("/api/market-intel/markets", response_model=MessageOut)
def mi_markets(regime: str | None = None, limit: int = 200, db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="market profiles", detail={"markets": market_intel.markets(db, regime=regime, limit=limit)})


@app.get("/api/market-intel/markets/{market_id}", response_model=MessageOut)
def mi_market_detail(market_id: str, db: Session = Depends(get_db)) -> MessageOut:
    detail = market_intel.market_detail(db, market_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="market profile not found")
    return MessageOut(message="market detail", detail=detail)


@app.get("/api/market-intel/regimes", response_model=MessageOut)
def mi_regimes(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="regime distribution", detail=market_intel.regime_distribution(db))


@app.get("/api/market-intel/wallet-specialization", response_model=MessageOut)
def mi_wallet_spec(limit: int = 100, db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="wallet specialization", detail={"wallets": market_intel.wallet_specialization(db, limit=limit)})


@app.get("/api/market-intel/strategy-specialization", response_model=MessageOut)
def mi_strategy_spec(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="strategy specialization", detail={"strategies": market_intel.strategy_specialization(db)})


@app.get("/api/market-intel/leaderboards", response_model=MessageOut)
def mi_leaderboards(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="regime leaderboards", detail=market_intel.regime_leaderboards(db))


@app.get("/api/market-intel/decay", response_model=MessageOut)
def mi_decay(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="decay analysis", detail=market_intel.decay_analysis(db))


@app.get("/api/market-intel/originality", response_model=MessageOut)
def mi_originality(limit: int = 100, db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="originality graph", detail=market_intel.originality(db, limit=limit))


@app.get("/api/market-intel/counterfactual", response_model=MessageOut)
def mi_counterfactual(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="counterfactual results", detail=market_intel.counterfactual_results(db))


@app.get("/api/market-intel/recommendations", response_model=MessageOut)
def mi_recommendations(limit: int = 50, db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="market recommendations", detail={"recommendations": market_intel.market_recommendations(db, limit=limit)})


@app.get("/api/market-intel/nightly-reviews", response_model=MessageOut)
def mi_nightly_reviews(limit: int = 30, db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="market intel nightly reviews", detail={"reviews": market_intel.nightly_reviews(db, limit=limit)})


# ===========================================================================
# Paper Challenger Framework V1 — isolated PAPER-ONLY A/B research. Never trades
# or changes execution/eligibility/rankings/bankroll/copy-trading/risk controls.
# ===========================================================================
@app.get("/api/challenger/dashboard", response_model=MessageOut)
def pc_dashboard(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="challenger dashboard", detail=challenger.dashboard(db))


@app.post("/api/challenger/run", response_model=MessageOut)
def pc_run(refresh_lab: bool = False, limit_markets: int = 150, db: Session = Depends(get_db)) -> MessageOut:
    """Run one paper-challenger cycle (build immutable experiments -> rebuild
    portfolios -> significance -> champion -> recommendations -> nightly review).
    Paper-only; never trades."""
    return MessageOut(message="challenger cycle", detail=challenger.run_challengers(
        db, refresh_lab=refresh_lab, limit_markets=limit_markets))


@app.get("/api/challenger/challengers", response_model=MessageOut)
def pc_challengers(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="challengers", detail={"challengers": challenger.challengers(db)})


@app.get("/api/challenger/challengers/{key}", response_model=MessageOut)
def pc_challenger_detail(key: str, db: Session = Depends(get_db)) -> MessageOut:
    detail = challenger.challenger_detail(db, key)
    if detail is None:
        raise HTTPException(status_code=404, detail="challenger not found")
    return MessageOut(message="challenger detail", detail=detail)


@app.get("/api/challenger/experiments", response_model=MessageOut)
def pc_experiments(limit: int = 60, db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="experiments", detail={"experiments": challenger.experiments(db, limit=limit)})


@app.get("/api/challenger/experiments/{experiment_id}", response_model=MessageOut)
def pc_experiment_detail(experiment_id: int, db: Session = Depends(get_db)) -> MessageOut:
    detail = challenger.experiment_detail(db, experiment_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return MessageOut(message="experiment detail", detail=detail)


@app.get("/api/challenger/comparison", response_model=MessageOut)
def pc_comparison(kind: str = "timing", db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="comparison", detail={"kind": kind, "rows": challenger.comparison(db, kind)})


@app.get("/api/challenger/regime-performance", response_model=MessageOut)
def pc_regime_perf(db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="regime performance", detail=challenger.regime_performance(db))


@app.get("/api/challenger/recommendations", response_model=MessageOut)
def pc_recommendations(limit: int = 50, db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="challenger recommendations", detail={"recommendations": challenger.recommendations(db, limit=limit)})


@app.get("/api/challenger/nightly-reviews", response_model=MessageOut)
def pc_nightly_reviews(limit: int = 30, db: Session = Depends(get_db)) -> MessageOut:
    return MessageOut(message="challenger nightly reviews", detail={"reviews": challenger.nightly_reviews(db, limit=limit)})


# ===========================================================================
# Backtests
# ===========================================================================
@app.post("/api/backtests/run", response_model=BacktestOut)
def backtests_run(config: BacktestConfig, db: Session = Depends(get_db)) -> Backtest:
    backtest = services.run_backtest(db, config.model_dump())
    db.refresh(backtest)
    return backtest


@app.get("/api/backtests", response_model=list[BacktestListItem])
def backtests_list(db: Session = Depends(get_db)) -> list[Backtest]:
    return list(db.scalars(select(Backtest).order_by(Backtest.created_at.desc())).all())


@app.get("/api/backtests/{backtest_id}", response_model=BacktestOut)
def backtests_get(backtest_id: int, db: Session = Depends(get_db)) -> Backtest:
    backtest = db.get(Backtest, backtest_id)
    if backtest is None:
        raise HTTPException(status_code=404, detail="Backtest not found")
    return backtest


@app.get("/api/backtests/{backtest_id}/trades", response_model=list[BacktestTradeOut])
def backtests_trades(
    backtest_id: int, strategy: str | None = None, limit: int = 500,
    db: Session = Depends(get_db),
) -> list[BacktestTrade]:
    if db.get(Backtest, backtest_id) is None:
        raise HTTPException(status_code=404, detail="Backtest not found")
    stmt = select(BacktestTrade).where(BacktestTrade.backtest_id == backtest_id)
    if strategy:
        stmt = stmt.where(BacktestTrade.strategy == strategy)
    stmt = stmt.order_by(BacktestTrade.closed_at).limit(limit)
    return list(db.scalars(stmt).all())


# ===========================================================================
# Attribution & signal quality
# ===========================================================================
@app.get("/api/attribution/wallets", response_model=list[WalletAttributionOut])
def attribution_wallets(db: Session = Depends(get_db)) -> list[WalletAttributionOut]:
    return [WalletAttributionOut(**row) for row in attribution.compute_wallet_attribution(db)]


# ===========================================================================
# Discovery
# ===========================================================================
@app.post("/api/discovery/run", response_model=MessageOut)
def discovery_run(payload: DiscoveryRunRequest | None = None, db: Session = Depends(get_db)) -> MessageOut:
    max_backfill = payload.max_backfill if payload else None
    result = services.run_discovery(db, max_backfill=max_backfill)
    return MessageOut(message="Discovery complete", detail=result)


@app.get("/api/discovery/candidates", response_model=list[CandidateOut])
def discovery_candidates(
    classification: str | None = None, state: str | None = None,
    db: Session = Depends(get_db),
) -> list[CandidateOut]:
    return [CandidateOut(**row) for row in services.list_candidates(db, classification, state)]


@app.get("/api/discovery/candidates/{address}", response_model=CandidateDetailOut)
def discovery_candidate_detail(address: str, db: Session = Depends(get_db)) -> CandidateDetailOut:
    detail = services.candidate_detail(db, address)
    if detail is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return CandidateDetailOut(**detail)


@app.post("/api/discovery/candidates/{address}/track", response_model=MessageOut)
def discovery_track(address: str, db: Session = Depends(get_db)) -> MessageOut:
    cand = discovery.set_candidate_state(db, address, "tracked")
    if cand is None:
        raise HTTPException(status_code=404, detail="Wallet not found")
    return MessageOut(message=f"Tracking {address} — copying enabled")


@app.post("/api/discovery/candidates/{address}/ignore", response_model=MessageOut)
def discovery_ignore(address: str, db: Session = Depends(get_db)) -> MessageOut:
    cand = discovery.set_candidate_state(db, address, "ignored")
    if cand is None:
        raise HTTPException(status_code=404, detail="Wallet not found")
    return MessageOut(message=f"Ignoring {address} — copying disabled")


@app.get("/api/signals/quality", response_model=list[SignalQualityOut])
def signals_quality(limit: int = 200, db: Session = Depends(get_db)) -> list[SignalQualityOut]:
    rows = db.scalars(
        select(PaperSignal).order_by(PaperSignal.created_at.desc()).limit(limit)
    ).all()
    out: list[SignalQualityOut] = []
    for s in rows:
        out.append(
            SignalQualityOut(
                id=s.id, created_at=s.created_at,
                wallet_address=s.wallet.address if s.wallet else None,
                market_question=s.market.question if s.market else None,
                outcome=s.outcome, observed_price=s.observed_price, confidence=s.confidence,
                edge_estimate=s.edge_estimate, copied=s.copied,
                move_5m=s.move_5m, move_30m=s.move_30m, move_2h=s.move_2h,
                move_close=s.move_close, mfe=s.mfe, mae=s.mae,
            )
        )
    return out


# ===========================================================================
# TOP 20 — paper strategy lab (PAPER ONLY; never places real orders)
# ===========================================================================
@app.get("/api/top-20/strategies")
def top20_strategies(db: Session = Depends(get_db)) -> dict:
    return {"paper_only": True, "strategies": top20.list_strategies(db)}


@app.get("/api/top-20/strategies/{strategy_id}")
def top20_strategy_detail(strategy_id: int, db: Session = Depends(get_db)) -> dict:
    detail = top20.strategy_detail(db, strategy_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return detail


@app.get("/api/top-20/trades")
def top20_trades(strategy_id: int | None = None, limit: int = 100,
                 db: Session = Depends(get_db)) -> dict:
    return {"paper_only": True, "trades": top20.list_trades(db, strategy_id, limit)}


@app.post("/api/top-20/recompute", response_model=MessageOut)
def top20_recompute(db: Session = Depends(get_db)) -> MessageOut:
    """Evaluate outstanding signals across all 20 strategies, settle/mark, snapshot."""
    settings = services.get_settings(db)
    result = top20.run_cycle(db, settings)
    return MessageOut(message="TOP 20 recomputed", detail=result)


@app.post("/api/top-20/reset-paper", response_model=MessageOut)
def top20_reset(db: Session = Depends(get_db)) -> MessageOut:
    """Wipe TOP 20 paper trades + snapshots (paper-only dev action)."""
    result = top20.reset_paper(db)
    return MessageOut(message="TOP 20 paper state reset", detail=result)


@app.get("/api/top-20/leaderboard")
def top20_leaderboard(db: Session = Depends(get_db)) -> dict:
    """Risk-adjusted weighted ranking + why #1 beats #2 (Phase 7)."""
    return top20.leaderboard_view(db)


@app.get("/api/top-20/portfolio")
def top20_portfolio(db: Session = Depends(get_db)) -> dict:
    """Aggregate paper portfolio analytics across all 20 strategies (Phase 6)."""
    return top20.portfolio(db)


@app.get("/api/top-20/explain/{signal_id}")
def top20_explain(signal_id: int, db: Session = Depends(get_db)) -> dict:
    """How each of the 20 strategies decided on one signal (Phase 8)."""
    out = top20.explain_signal(db, signal_id)
    if out is None:
        raise HTTPException(status_code=404, detail="Signal not found")
    return out


@app.get("/api/top-20/forward-test")
def top20_forward_test(db: Session = Depends(get_db)) -> dict:
    """Train / validation / forward split metrics per strategy (Phase 9)."""
    return top20.forward_test(db)


@app.get("/api/wallets/{address}/profile")
def wallet_profile(address: str, db: Session = Depends(get_db)) -> dict:
    """Quant profile for one wallet: ROI, Sharpe, categories, equity curve (Phase 3)."""
    out = top20.wallet_profile(db, address)
    if out is None:
        raise HTTPException(status_code=404, detail="Wallet not found")
    return out


# --- research platform (Phases 12-20) ---------------------------------------
@app.get("/api/top-20/optimize/{param}")
def top20_optimize(param: str, db: Session = Depends(get_db)) -> dict:
    """Parameter sweep over historical labeled data (Phase 12)."""
    return top20.optimize_param(db, param)


@app.get("/api/top-20/walk-forward/{param}")
def top20_walk_forward(param: str, windows: int = 4, db: Session = Depends(get_db)) -> dict:
    """Walk-forward stability analysis for a parameter (Phase 13)."""
    return top20.walk_forward_param(db, param, windows)


@app.get("/api/top-20/montecarlo/{strategy_id}")
def top20_montecarlo(strategy_id: int, sims: int = 2000, seed: int = 42,
                     db: Session = Depends(get_db)) -> dict:
    """Monte Carlo risk analysis for a strategy (Phase 14)."""
    return top20.monte_carlo(db, strategy_id, sims, seed)


@app.get("/api/top-20/market-intel")
def top20_market_intel(db: Session = Depends(get_db)) -> dict:
    """What kinds of markets are easiest to beat? (Phase 16)."""
    return top20.market_intelligence(db)


@app.get("/api/top-20/ensembles")
def top20_ensembles(db: Session = Depends(get_db)) -> dict:
    """Ensemble strategy performance (Phase 17)."""
    return top20.ensemble_view(db)


@app.get("/api/top-20/retirement")
def top20_retirement(db: Session = Depends(get_db)) -> dict:
    """Strategies recommended for retirement after meaningful samples (Phase 18)."""
    return top20.recommend_retirements(db)


@app.post("/api/top-20/strategies/{key}/status")
def top20_set_status(key: str, status: str, db: Session = Depends(get_db)) -> dict:
    """Move a strategy through its lifecycle (Phase 18). Paper-only metadata."""
    out = top20.set_status(db, key, status)
    if out is None:
        raise HTTPException(status_code=400, detail="Unknown strategy or invalid status")
    return out


@app.get("/api/top-20/report")
def top20_report(db: Session = Depends(get_db)) -> dict:
    """Daily research report as Markdown (Phase 19)."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return top20.research_report(db, today)


@app.get("/api/top-20/dataset")
def top20_dataset(limit: int = 100, settled_only: bool = False,
                  db: Session = Depends(get_db)) -> dict:
    """Collected feature-vector dataset for future ML (Phase 20)."""
    return top20.feature_vectors(db, limit, settled_only)


# --- historical replay engine (Phases 21-24) --------------------------------
@app.get("/api/replay/status")
def replay_status(db: Session = Depends(get_db)) -> dict:
    return top20.replay_status(db)


@app.post("/api/replay/backfill-markets")
def replay_backfill_markets(pages: int = 3, db: Session = Depends(get_db)) -> dict:
    """Backfill a chunk of historical closed markets (Phase 21). Checkpointed."""
    return top20.replay_backfill_markets(db, pages)


@app.post("/api/replay/backfill-wallets")
def replay_backfill_wallets(max_wallets: int = 5, db: Session = Depends(get_db)) -> dict:
    """Backfill more wallets' historical trades via discovery (Phase 22)."""
    return top20.replay_backfill_wallets(db, max_wallets)


@app.post("/api/replay/run")
def replay_run(max_trades: int = 400, db: Session = Depends(get_db)) -> dict:
    """Process a chunk of the chronological replay (Phase 23/24). Resumable."""
    return top20.replay_run(db, max_trades)


@app.post("/api/replay/reset")
def replay_reset(db: Session = Depends(get_db)) -> dict:
    return top20.replay_reset(db)


@app.post("/api/replay/run-realistic")
def replay_run_realistic(db: Session = Depends(get_db)) -> dict:
    """Capital-constrained realistic portfolio simulation (Issue 2)."""
    return top20.replay_run_realistic(db)


@app.get("/api/replay/realistic")
def replay_realistic(db: Session = Depends(get_db)) -> dict:
    return top20.realistic_view(db)


@app.get("/api/replay/comparison")
def replay_comparison(db: Session = Depends(get_db)) -> dict:
    """Notional (unlimited capital) vs realistic ($10k constrained) per strategy."""
    return top20.replay_comparison(db)


@app.post("/api/replay/reset-realistic")
def replay_reset_realistic(db: Session = Depends(get_db)) -> dict:
    return top20.replay_reset_realistic(db)


# --- research analytics (Phases 26-29) --------------------------------------
@app.get("/api/research/benchmark")
def research_benchmark(db: Session = Depends(get_db)) -> dict:
    """Probability-estimator benchmark vs baselines (Phase 29)."""
    return top20.probability_benchmark(db)


@app.get("/api/research/drift")
def research_drift(db: Session = Depends(get_db)) -> dict:
    """Strategy drift by month (Phase 27)."""
    return top20.strategy_drift(db)


@app.get("/api/research/regimes")
def research_regimes(db: Session = Depends(get_db)) -> dict:
    """Market regimes + per-regime strategy performance (Phase 28)."""
    return top20.market_regimes(db)


@app.get("/api/wallets/{address}/evolution")
def wallet_evolution(address: str, db: Session = Depends(get_db)) -> dict:
    """Point-in-time wallet reputation through history (Phase 26)."""
    out = top20.wallet_evolution(db, address)
    if out is None:
        raise HTTPException(status_code=404, detail="Wallet not found")
    return out
