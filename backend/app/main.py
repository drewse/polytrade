"""FastAPI application exposing the dashboard API."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import attribution, auto_worker, discovery, live, services, top20
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
    """Reconcile computed bankroll against the venue-reported balance."""
    return MessageOut(message="reconciliation", detail=live.reconcile(db, balance))


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
