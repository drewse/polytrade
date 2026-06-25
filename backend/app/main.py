"""FastAPI application exposing the dashboard API."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import attribution, discovery, services
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
    finally:
        db.close()


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
    result = services.run_ingest_cycle(db)
    return MessageOut(message="Ingest cycle complete", detail=result)


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
