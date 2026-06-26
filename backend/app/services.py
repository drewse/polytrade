"""
Service layer: the glue between the data provider, the engines, and the DB.

Everything that mutates the database lives here so the API routes and the worker
stay thin. Functions take an explicit SQLAlchemy `Session`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import auto_worker
from . import backtest as bt
from . import discovery
from . import paper_trading as pt
from . import positions as positions_mod
from . import scoring
from . import signal_quality
from . import top20
from .models import (
    Backtest,
    BacktestResult,
    BacktestTrade,
    EquitySnapshot,
    IngestStatus,
    Market,
    MarketPriceSnapshot,
    PaperFill,
    PaperPosition,
    PaperSignal,
    PaperStrategy,
    Setting,
    Trade,
    Wallet,
    WalletCandidate,
    WalletStat,
)
from .mock_provider import WORLD_SEED
from .polymarket_client import MarketDTO, TradeDTO, get_provider
from .settings import DEFAULT_SETTINGS, INT_SETTING_KEYS, STR_SETTING_KEYS
from .signals import SignalRules, detect_signals


# ===========================================================================
# Settings
# ===========================================================================
def ensure_settings(db: Session) -> dict:
    """Make sure every default setting exists; return the full settings dict."""
    existing = {s.key: s.value for s in db.scalars(select(Setting)).all()}
    changed = False
    for key, val in DEFAULT_SETTINGS.items():
        if key not in existing:
            db.add(Setting(key=key, value=str(val)))
            existing[key] = str(val)
            changed = True
    if changed:
        db.commit()
    # also ensure a default strategy row exists
    if db.scalar(select(func.count()).select_from(PaperStrategy)) == 0:
        db.add(PaperStrategy(name="default"))
        db.commit()
    return get_settings(db)


def _coerce(key: str, raw: str):
    if key in INT_SETTING_KEYS:
        return int(float(raw))
    if key in STR_SETTING_KEYS:
        return raw
    return float(raw)


def get_settings(db: Session) -> dict:
    rows = {s.key: s.value for s in db.scalars(select(Setting)).all()}
    return {k: _coerce(k, rows.get(k, str(v))) for k, v in DEFAULT_SETTINGS.items()}


def update_settings(db: Session, updates: dict) -> dict:
    for key, val in updates.items():
        if val is None or key not in DEFAULT_SETTINGS:
            continue
        row = db.get(Setting, key)
        if row is None:
            db.add(Setting(key=key, value=str(val)))
        else:
            row.value = str(val)
    db.commit()
    return get_settings(db)


# ===========================================================================
# Ingestion helpers
# ===========================================================================
def upsert_market(db: Session, dto: MarketDTO) -> Market:
    m = db.get(Market, dto.id)
    if m is None:
        m = Market(id=dto.id)
        db.add(m)
    # Never un-resolve a market that the DB already marked resolved (a fresh mock
    # provider instance reports open markets as open each poll).
    already_resolved = bool(m.resolved)
    m.question = dto.question
    m.slug = dto.slug
    m.category = dto.category
    m.outcomes = list(dto.outcomes)
    m.prices = [float(p) for p in dto.prices]
    m.token_ids = list(getattr(dto, "token_ids", []) or [])
    m.best_bid = getattr(dto, "best_bid", None)
    m.best_ask = getattr(dto, "best_ask", None)
    m.liquidity = dto.liquidity
    m.volume = dto.volume
    if not already_resolved:
        m.resolved = dto.resolved
        m.resolved_outcome = dto.resolved_outcome
        if dto.resolved:
            ts = dto.resolved_at
            if ts is not None and ts.tzinfo is not None:
                ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
            m.resolved_at = ts or datetime.utcnow()
    return m


def get_or_create_wallet(db: Session, address: str, label: str | None = None) -> Wallet:
    w = db.scalar(select(Wallet).where(Wallet.address == address))
    if w is None:
        w = Wallet(address=address, label=label)
        db.add(w)
        db.flush()  # assign id
    return w


def insert_trade(db: Session, dto: TradeDTO, wallet: Wallet) -> Trade | None:
    """Insert a trade if its external_id is new. Returns the Trade or None."""
    if dto.external_id:
        exists = db.scalar(select(Trade).where(Trade.external_id == dto.external_id))
        if exists:
            return None
    # Store naive UTC so it never clashes with SQLite-read naive datetimes.
    ts = dto.timestamp
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    t = Trade(
        external_id=dto.external_id,
        wallet_id=wallet.id,
        market_id=dto.market_id,
        outcome=dto.outcome,
        side=dto.side,
        price=dto.price,
        size=dto.size,
        timestamp=ts,
        realized_pnl=getattr(dto, "realized_pnl", 0.0),
    )
    db.add(t)
    if wallet.last_active is None or ts > wallet.last_active:
        wallet.last_active = ts
    return t


# ===========================================================================
# Mock seeding
# ===========================================================================
# Mock entities have deterministic, synthetic ids (see mock_provider): markets
# are "0xmkt0001"… and wallet addresses are 0x + many leading zeros. Real
# Polymarket condition ids / addresses never collide with these patterns.
_MOCK_MARKET_PREFIX = "0xmkt"
_MOCK_WALLET_PREFIX = "0x0000000000"  # 10 leading zero nibbles


def purge_mock_data(db: Session) -> dict:
    """Remove mock-seeded entities so live mode shows only live-derived data.

    Identifies mock rows by their synthetic id patterns and deletes them plus
    everything that references them. A no-op when no mock data is present, so it
    is safe to call on every live cycle. Mock *support* is untouched — re-seeding
    recreates the world.
    """
    mock_wallet_ids = [
        wid for (wid,) in db.execute(
            select(Wallet.id).where(Wallet.address.like(_MOCK_WALLET_PREFIX + "%"))
        ).all()
    ]
    mock_market_ids = [
        mid for (mid,) in db.execute(
            select(Market.id).where(Market.id.like(_MOCK_MARKET_PREFIX + "%"))
        ).all()
    ]
    if not mock_wallet_ids and not mock_market_ids:
        return {"wallets": 0, "markets": 0}

    w = set(mock_wallet_ids)
    m = set(mock_market_ids)

    def _touches_mock(row) -> bool:
        return getattr(row, "wallet_id", None) in w or getattr(row, "market_id", None) in m

    # Delete dependents first (FK order). PaperFill cascades from PaperPosition.
    for pos in db.scalars(select(PaperPosition)).all():
        if _touches_mock(pos):
            db.delete(pos)
    for sig in db.scalars(select(PaperSignal)).all():
        if _touches_mock(sig):
            db.delete(sig)
    db.flush()
    for tr in db.scalars(select(Trade)).all():
        if _touches_mock(tr):
            db.delete(tr)
    for snap in db.scalars(select(MarketPriceSnapshot)).all():
        if snap.market_id in m:
            db.delete(snap)
    for wid in mock_wallet_ids:
        st = db.get(WalletStat, wid)
        if st:
            db.delete(st)
        cand = db.get(WalletCandidate, wid)
        if cand:
            db.delete(cand)
    db.flush()
    for wid in mock_wallet_ids:
        wallet = db.get(Wallet, wid)
        if wallet:
            db.delete(wallet)
    for mid in mock_market_ids:
        market = db.get(Market, mid)
        if market:
            db.delete(market)
    # Equity snapshots are global mock/live-mixed history; clear so the curve
    # reflects only live going forward (it is rebuilt each cycle).
    db.query(EquitySnapshot).delete()
    db.commit()
    return {"wallets": len(mock_wallet_ids), "markets": len(mock_market_ids)}


def seed_mock_data(db: Session) -> dict:
    """Wipe paper/trade state and load the full deterministic mock world."""
    from .mock_provider import MockProvider

    # Clear existing rows (order matters for FKs).
    for model in (
        PaperFill, BacktestTrade, BacktestResult, Backtest, MarketPriceSnapshot,
        IngestStatus, WalletCandidate, PaperPosition, PaperSignal, EquitySnapshot,
        Trade, WalletStat,
    ):
        db.query(model).delete()
    db.query(Wallet).delete()
    db.query(Market).delete()
    db.commit()

    provider = MockProvider()
    world = provider.world

    # markets
    for mdto in world.markets:
        upsert_market(db, mdto)
    db.commit()

    # wallets
    wallet_by_addr: dict[str, Wallet] = {}
    for w in world.wallets:
        wallet = Wallet(address=w["address"], label=w["label"], copy_enabled=True)
        db.add(wallet)
        wallet_by_addr[w["address"]] = wallet
    db.flush()

    # historical + recent trades
    for dto in [*world.historical_trades, *world.recent_trades]:
        wallet = wallet_by_addr.get(dto.wallet_address)
        if wallet:
            insert_trade(db, dto, wallet)
    db.commit()

    # compute stats / scores
    recompute_all_wallet_stats(db)

    # generate signals from the recent trades and open some positions
    settings = get_settings(db)
    recent_cutoff = datetime.utcnow() - timedelta(hours=6)
    recent_trades = db.scalars(
        select(Trade).where(Trade.timestamp >= recent_cutoff)
    ).all()
    n_signals = _create_signals_for_trades(db, recent_trades, settings)
    n_positions = open_positions_for_new_signals(db, settings)

    # Resolve a chunk of markets up front so the dashboard ships with some
    # closed positions / realized PnL alongside the open ones.
    for _ in range(6):
        maybe_resolve_markets(db, max_resolve=4)
    refresh_positions(db)
    record_equity_snapshot(db)

    # Synthesize signal-quality so the Signals page has data right after seeding
    # (no real price history exists yet; the worker refines this from snapshots).
    import random as _random
    signal_quality.synthesize(db, _random.Random(WORLD_SEED))

    record_ingest_status(db, "mock", len(world.markets), len(world.recent_trades),
                         {"markets_ok": True, "trades_ok": True, "prices_ok": True, "errors": []})

    # Run discovery so the Discovery page is populated immediately.
    disc = run_discovery(db)

    n_closed = db.scalar(
        select(func.count()).select_from(PaperPosition).where(PaperPosition.status == "closed")
    )

    return {
        "wallets": len(world.wallets),
        "markets": len(world.markets),
        "historical_trades": len(world.historical_trades),
        "recent_trades": len(world.recent_trades),
        "signals": n_signals,
        "positions_opened": n_positions,
        "positions_closed": int(n_closed or 0),
        "discovery": disc["by_classification"],
    }


# ===========================================================================
# Wallet stats / scoring
# ===========================================================================
def recompute_all_wallet_stats(db: Session, partial: bool = False, reconstruct: bool = False) -> int:
    wallets = db.scalars(select(Wallet)).all()
    for wallet in wallets:
        recompute_wallet_stats(db, wallet, partial=partial, reconstruct=reconstruct)
    db.commit()
    return len(wallets)


def recompute_wallet_stats(
    db: Session, wallet: Wallet, partial: bool = False, reconstruct: bool = False
) -> WalletStat:
    """Recompute a wallet's stats from its trades.

    In live mode (`reconstruct=True`) raw fills carry no P&L, so resolved
    positions are reconstructed from the fills + real market resolutions and fed
    to the scorer as the settled units. In mock mode the fills already carry
    realized P&L, so the scorer derives settled units itself.
    """
    trades = list(db.scalars(select(Trade).where(Trade.wallet_id == wallet.id)).all())
    settled = None
    if reconstruct:
        market_ids = {t.market_id for t in trades}
        markets_by_id = {
            m.id: m
            for m in db.scalars(select(Market).where(Market.id.in_(market_ids))).all()
        }
        settled = positions_mod.settled_positions(trades, markets_by_id)
    result = scoring.score_wallet(trades, settled=settled)
    stat = db.get(WalletStat, wallet.id)
    if stat is None:
        stat = WalletStat(wallet_id=wallet.id)
        db.add(stat)
    stat.partial_history = partial
    stat.num_trades = result.num_trades
    stat.num_settled = result.num_settled
    stat.realized_pnl = result.realized_pnl
    stat.realized_roi = result.realized_roi
    stat.win_rate = result.win_rate
    stat.profit_factor = result.profit_factor
    stat.expectancy = result.expectancy
    stat.sharpe = result.sharpe
    stat.max_drawdown = result.max_drawdown
    stat.avg_trade_size = result.avg_trade_size
    stat.consistency = result.consistency
    stat.recency_score = result.recency_score
    stat.category_performance = result.category_performance
    stat.score = result.score
    stat.classification = result.classification
    return stat


# ===========================================================================
# Signals
# ===========================================================================
def _create_signals_for_trades(db: Session, trades: list[Trade], settings: dict) -> int:
    if not trades:
        return 0
    wallets_by_id = {w.id: w for w in db.scalars(select(Wallet)).all()}
    stats_by_id = {s.wallet_id: s for s in db.scalars(select(WalletStat)).all()}
    market_ids = {t.market_id for t in trades}
    markets_by_id = {
        m.id: m for m in db.scalars(select(Market).where(Market.id.in_(market_ids))).all()
    }
    rules = SignalRules(
        min_wallet_score=settings["min_wallet_score"],
        min_trade_count=settings["min_trade_count"],
        min_trade_size=settings["min_trade_size"],
        min_market_liquidity=settings["min_market_liquidity"],
        max_price_staleness_min=settings["max_price_staleness_min"],
        min_volume=settings["min_volume"],
        min_edge=settings["min_edge"],
    )
    signals = detect_signals(trades, wallets_by_id, stats_by_id, markets_by_id, rules)
    for sig in signals:
        db.add(sig)
    db.commit()
    return len(signals)


# ===========================================================================
# Paper positions
# ===========================================================================
def _open_positions(db: Session) -> list[PaperPosition]:
    return list(db.scalars(select(PaperPosition).where(PaperPosition.status == "open")).all())


def _today_realized_pnl(db: Session) -> float:
    start = datetime(datetime.utcnow().year, datetime.utcnow().month, datetime.utcnow().day)
    rows = db.scalars(
        select(PaperPosition).where(
            PaperPosition.status == "closed", PaperPosition.closed_at >= start
        )
    ).all()
    return round(sum(p.realized_pnl for p in rows), 2)


def _in_loss_cooldown(db: Session, settings: dict) -> bool:
    """True if the last `cooldown_losses` closes were all losers and the most
    recent loss is within the cooldown window."""
    n = int(settings["cooldown_losses"])
    if n <= 0:
        return False
    recent = db.scalars(
        select(PaperPosition)
        .where(PaperPosition.status == "closed")
        .order_by(PaperPosition.closed_at.desc())
        .limit(n)
    ).all()
    if len(recent) < n or any(p.realized_pnl >= 0 for p in recent):
        return False
    last_close = recent[0].closed_at
    if last_close is None:
        return False
    age_min = (datetime.utcnow() - last_close).total_seconds() / 60.0
    return age_min <= float(settings["cooldown_minutes"])


def _category_exposure(db: Session, open_pos: list[PaperPosition], category: str | None) -> float:
    total = 0.0
    for p in open_pos:
        m = db.get(Market, p.market_id)
        if m and m.category == category:
            total += p.size
    return total


def open_positions_for_new_signals(db: Session, settings: dict) -> int:
    """Open paper positions for uncopied signals that pass risk rules."""
    risk = pt.RiskConfig(
        bankroll=current_bankroll(db, settings),
        max_position_pct=settings["max_position_pct"],
        max_market_exposure_pct=settings["max_market_exposure_pct"],
        slippage_cents=settings["slippage_cents"],
        min_confidence=settings["min_confidence"],
    )
    # --- account-level risk gates (checked once) ----------------------------
    if _today_realized_pnl(db) <= -abs(settings["max_daily_loss"]):
        return 0  # daily loss limit hit — stop opening today
    if _in_loss_cooldown(db, settings):
        return 0  # cooling off after a losing streak

    max_open = int(settings["max_open_positions"])
    cat_cap = risk.bankroll * (settings["max_correlated_exposure_pct"] / 100.0)

    signals = db.scalars(
        select(PaperSignal).where(PaperSignal.copied == False)  # noqa: E712
    ).all()
    open_pos = _open_positions(db)
    opened = 0
    for sig in signals:
        if len(open_pos) >= max_open:
            break
        market = db.get(Market, sig.market_id)
        if market is None or market.resolved:
            sig.copied = False
            continue
        size = pt.position_size(risk.bankroll, risk.max_position_pct)
        allowed, why = pt.can_open(risk, sig.confidence, open_pos, sig.market_id, size)
        if not allowed:
            # leave the signal as "not copied"; reason recorded for transparency
            continue
        # correlated-exposure cap: limit total open size within one category
        if _category_exposure(db, open_pos, market.category) + size > cat_cap + 1e-6:
            continue
        fill_price = pt.apply_slippage(sig.suggested_entry, sig.side, risk.slippage_cents)
        shares = size / max(fill_price, 1e-6)
        pos = PaperPosition(
            signal_id=sig.id,
            wallet_id=sig.wallet_id,
            market_id=sig.market_id,
            outcome=sig.outcome,
            side=sig.side,
            size=size,
            shares=round(shares, 4),
            entry_price=fill_price,
            current_price=fill_price,
            status="open",
            reason=f"Copied signal #{sig.id}: {sig.reason}",
        )
        db.add(pos)
        db.flush()
        db.add(
            PaperFill(
                position_id=pos.id, kind="entry", price=fill_price, size=size,
                slippage=round(fill_price - sig.suggested_entry, 4),
            )
        )
        sig.copied = True
        open_pos.append(pos)
        opened += 1
    db.commit()
    return opened


def maybe_resolve_markets(db: Session, max_resolve: int = 2) -> int:
    """Simulate market resolutions for markets that currently hold open positions.

    Sharp wallets are skilled, so we resolve a market in favor of the
    score-weighted consensus outcome of its open positions ~62% of the time.
    This is what lets the dashboard answer "would copying have made money?" in
    mock mode. (In live mode, resolution comes from real market data instead.)
    """
    import random

    open_pos = _open_positions(db)
    # group open positions by market
    by_market: dict[str, list[PaperPosition]] = {}
    for p in open_pos:
        market = db.get(Market, p.market_id)
        if market and not market.resolved:
            by_market.setdefault(p.market_id, []).append(p)
    if not by_market:
        return 0

    candidates = list(by_market.keys())
    random.shuffle(candidates)
    resolved = 0
    for market_id in candidates[:max_resolve]:
        # ~35% chance per eligible market each cycle, so resolution is gradual.
        if random.random() > 0.35:
            continue
        positions = by_market[market_id]
        market = db.get(Market, market_id)
        # score-weighted vote for the consensus outcome
        votes: dict[str, float] = {}
        for p in positions:
            stat = db.get(WalletStat, p.wallet_id)
            weight = (stat.score if stat else 50.0) / 100.0
            votes[p.outcome] = votes.get(p.outcome, 0.0) + weight
        consensus = max(votes, key=votes.get)
        winner = consensus if random.random() < 0.62 else next(
            (o for o in market.outcomes if o != consensus), consensus
        )
        market.resolved = True
        market.resolved_outcome = winner
        market.resolved_at = datetime.utcnow()
        market.prices = [1.0 if o == winner else 0.0 for o in market.outcomes]
        resolved += 1
    db.commit()
    return resolved


def refresh_positions(db: Session) -> dict:
    """Mark open positions to market; auto-close any whose market resolved."""
    open_pos = _open_positions(db)
    closed = 0
    for pos in open_pos:
        market = db.get(Market, pos.market_id)
        if market is None:
            continue
        res_price = pt.resolution_exit_price(market, pos.outcome)
        if res_price is not None:
            _close_position(db, pos, res_price, reason="auto-closed: market resolved")
            closed += 1
            continue
        price = market.price_for(pos.outcome)
        if price is not None:
            pos.current_price = price
            pos.unrealized_pnl = pt.mark_to_market(pos, price)
    db.commit()
    return {"open": len(open_pos) - closed, "auto_closed": closed}


def _close_position(db: Session, pos: PaperPosition, exit_price: float, reason: str) -> None:
    pos.exit_price = exit_price
    pos.current_price = exit_price
    pos.realized_pnl = pt.realized_on_close(pos, exit_price)
    pos.unrealized_pnl = 0.0
    pos.status = "closed"
    pos.closed_at = datetime.utcnow()
    pos.reason = (pos.reason + " | " + reason).strip(" |")
    db.add(PaperFill(position_id=pos.id, kind="exit", price=exit_price, size=pos.size))


def close_position_manual(db: Session, position_id: int, settings: dict) -> PaperPosition | None:
    pos = db.get(PaperPosition, position_id)
    if pos is None or pos.status != "open":
        return None
    market = db.get(Market, pos.market_id)
    price = (market.price_for(pos.outcome) if market else None) or pos.current_price
    # apply exit slippage (selling fills worse = lower)
    exit_price = pt.apply_slippage(price, "sell", settings["slippage_cents"])
    _close_position(db, pos, exit_price, reason="manual close")
    db.commit()
    return pos


# ===========================================================================
# Bankroll / equity
# ===========================================================================
def current_bankroll(db: Session, settings: dict) -> float:
    """Starting bankroll + realized PnL from all closed positions."""
    realized = db.scalar(
        select(func.coalesce(func.sum(PaperPosition.realized_pnl), 0.0)).where(
            PaperPosition.status == "closed"
        )
    )
    return round(settings["bankroll"] + float(realized or 0.0), 2)


def record_equity_snapshot(db: Session) -> EquitySnapshot:
    settings = get_settings(db)
    bankroll = current_bankroll(db, settings)
    open_pos = _open_positions(db)
    unrealized = sum(p.unrealized_pnl for p in open_pos)
    exposure = sum(p.size for p in open_pos)
    realized = bankroll - settings["bankroll"]
    snap = EquitySnapshot(
        bankroll=bankroll,
        open_exposure=round(exposure, 2),
        equity=round(bankroll + unrealized, 2),
        total_pnl=round(realized + unrealized, 2),
        open_positions=len(open_pos),
    )
    db.add(snap)
    db.commit()
    return snap


# ===========================================================================
# Full ingest cycle (used by worker and POST /api/ingest/run)
# ===========================================================================
def _poll_tracked_wallets(db: Session, settings: dict, max_wallets: int = 25,
                          limit: int = 40) -> list[Trade]:
    """Live signal source: pull recent trades for the copy-enabled, high-scoring
    wallets we would actually copy, so the signal pipeline watches *them* rather
    than only the global recent-trade stream (which rarely contains our small set
    of tracked wallets). Returns freshly-inserted trades within the staleness
    window, ready to feed the unchanged signal gates.
    """
    min_score = float(settings["min_wallet_score"])
    cutoff_min = float(settings["max_price_staleness_min"])
    wallets = db.scalars(
        select(Wallet)
        .join(WalletStat, WalletStat.wallet_id == Wallet.id)
        .where(Wallet.copy_enabled == True, WalletStat.score >= min_score)  # noqa: E712
        .order_by(WalletStat.score.desc())
        .limit(max_wallets)
    ).all()
    if not wallets:
        return []

    from .polymarket_client import LivePolymarketClient

    now = datetime.utcnow()
    fresh_by_wallet: list[tuple[Wallet, list]] = []
    fresh_market_ids: set[str] = set()
    client = LivePolymarketClient()
    try:
        for w in wallets:
            try:
                dtos = client.get_wallet_trades(w.address, limit=limit)
            except Exception as exc:  # noqa: BLE001  (skip a flaky wallet, keep going)
                print(f"[poll] wallet trades failed for {w.address[:10]}: {exc}")
                continue
            fresh = []
            for dto in dtos:
                ts = dto.timestamp
                if ts.tzinfo is not None:
                    ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
                if (now - ts).total_seconds() / 60.0 <= cutoff_min:
                    fresh.append(dto)
                    fresh_market_ids.add(dto.market_id)
            if fresh:
                fresh_by_wallet.append((w, fresh))
        # Real metadata for the (small) set of fresh markets, so the signal gates
        # see true liquidity + resolution status.
        if fresh_market_ids:
            try:
                for mdto in client.get_markets_by_conditions(list(fresh_market_ids)):
                    upsert_market(db, mdto)
            except Exception as exc:  # noqa: BLE001
                print(f"[poll] fresh-market metadata failed: {exc}")
    finally:
        client.close()
    db.flush()  # persist upserts before the FK-stub check (autoflush=False)

    existing = {
        m for (m,) in db.execute(
            select(Market.id).where(Market.id.in_(fresh_market_ids))
        ).all()
    }
    for mid in fresh_market_ids - existing:
        db.add(Market(id=mid, question=f"(market {mid[:10]}…)",
                      outcomes=["Yes", "No"], prices=[0.5, 0.5]))
    db.flush()

    new_trades: list[Trade] = []
    for w, fresh in fresh_by_wallet:
        for dto in fresh:
            t = insert_trade(db, dto, w)
            if t is not None:
                new_trades.append(t)
    db.commit()
    return new_trades


def run_ingest_cycle(db: Session) -> dict:
    settings = ensure_settings(db)
    data_mode = settings["data_mode"]
    live = data_mode == "live"
    provider = get_provider(data_mode)
    status = {"markets_ok": True, "trades_ok": True, "prices_ok": True, "errors": []}

    # In live mode, drop any leftover mock entities so the dashboard shows only
    # live-derived data (no-op once the DB is clean).
    if live:
        purge_mock_data(db)

    # 1. refresh markets / prices
    n_markets = 0
    try:
        for mdto in provider.get_markets(limit=200):
            upsert_market(db, mdto)
            n_markets += 1
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        status["markets_ok"] = False
        status["prices_ok"] = False  # prices come from the same market refresh
        status["errors"].append(f"markets: {exc}")
        print(f"[ingest] market refresh error: {exc}")

    # 2. pull recent trades
    new_trades: list[Trade] = []
    try:
        for dto in provider.get_recent_trades(limit=50):
            wallet = get_or_create_wallet(db, dto.wallet_address)
            t = insert_trade(db, dto, wallet)
            if t is not None:
                new_trades.append(t)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        status["trades_ok"] = False
        status["errors"].append(f"trades: {exc}")
        print(f"[ingest] trade pull error: {exc}")

    # 2b. live: also watch the wallets we'd actually copy (tracked sharp wallets'
    #     own fresh trades), not just the global stream — this is where genuine
    #     copy signals come from.
    if live:
        try:
            tracked_new = _poll_tracked_wallets(db, settings)
            new_trades.extend(tracked_new)
        except Exception as exc:  # noqa: BLE001  (never let polling abort a cycle)
            db.rollback()
            status["errors"].append(f"tracked-poll: {exc}")
            print(f"[ingest] tracked-wallet poll error: {exc}")

    # 3. update stats for wallets that traded. In live mode these are derived
    #    from a recent window only, so mark them partial.
    touched_wallet_ids = {t.wallet_id for t in new_trades}
    for wid in touched_wallet_ids:
        wallet = db.get(Wallet, wid)
        if wallet:
            recompute_wallet_stats(db, wallet, partial=live, reconstruct=live)
    db.commit()

    # 4. signals + positions (only from observed trades)
    n_signals = _create_signals_for_trades(db, new_trades, settings)
    n_opened = open_positions_for_new_signals(db, settings)

    # 5. record price snapshots + refresh signal quality
    record_price_snapshots(db)
    n_quality = signal_quality.update_from_snapshots(db)

    # 6. simulate some resolutions (mock only), then mark-to-market + snapshot.
    #    In live mode, resolution comes from real market data (closed markets).
    if not live:
        maybe_resolve_markets(db)
    refresh = refresh_positions(db)
    record_equity_snapshot(db)

    # 6b. TOP 20 paper-strategy lab: evaluate the same signals across 20 variants,
    #     settle/mark, snapshot. Guarded so it can never break the main cycle.
    top20_summary = None
    try:
        top20_summary = top20.run_cycle(db, settings)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        status["errors"].append(f"top20: {exc}")
        print(f"[ingest] top20 error: {exc}")

    # 6c. Live execution layer: settle/monitor live positions always; place new
    #     orders only when LIVE_TRADING_ENABLED (default false). Fully guarded.
    try:
        from . import live
        live.process_new_signals(db)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        status["errors"].append(f"live: {exc}")
        print(f"[ingest] live error: {exc}")

    # close live client if any
    close = getattr(provider, "close", None)
    if callable(close):
        close()

    # 7. persist ingest health for the dashboard badges
    record_ingest_status(db, data_mode, n_markets, len(new_trades), status)

    # 8. optional auto-discovery (rate-limited by discovery_interval_minutes)
    discovery_summary = None
    if should_run_discovery(db, settings):
        discovery_summary = run_discovery(db)

    return {
        "markets": n_markets,
        "new_trades": len(new_trades),
        "signals": n_signals,
        "positions_opened": n_opened,
        "auto_closed": refresh["auto_closed"],
        "signals_quality_updated": n_quality,
        "discovery": discovery_summary["by_classification"] if discovery_summary else None,
        "top20": top20_summary,
        "data_mode": data_mode,
        "ok": not status["errors"],
        "errors": status["errors"],
    }


# ===========================================================================
# Ingest status (dashboard badges) + wallet backfill
# ===========================================================================
def record_ingest_status(db: Session, data_mode: str, n_markets: int, n_trades: int,
                         status: dict) -> IngestStatus:
    row = db.get(IngestStatus, 1)
    if row is None:
        row = IngestStatus(id=1)
        db.add(row)
    row.last_run_at = datetime.utcnow()
    row.data_mode = data_mode
    row.markets_ok = status["markets_ok"]
    row.trades_ok = status["trades_ok"]
    row.prices_ok = status["prices_ok"]
    row.ok = not status["errors"]
    row.n_markets = n_markets
    row.n_trades = n_trades
    row.error = "; ".join(status["errors"])[:1000] if status["errors"] else None
    db.commit()
    return row


def get_ingest_status(db: Session) -> dict:
    settings = get_settings(db)
    row = db.get(IngestStatus, 1)
    partial_wallets = db.scalar(
        select(func.count()).select_from(WalletStat).where(WalletStat.partial_history == True)  # noqa: E712
    )
    age_seconds = None
    stale = False
    if row and row.last_run_at:
        age_seconds = (datetime.utcnow() - row.last_run_at).total_seconds()
        # stale if older than ~4 polling intervals
        stale = age_seconds > max(120, settings["polling_interval_seconds"] * 4)
    return {
        "data_mode": settings["data_mode"],
        "last_run_at": row.last_run_at if row else None,
        "ok": row.ok if row else True,
        "markets_ok": row.markets_ok if row else True,
        "trades_ok": row.trades_ok if row else True,
        "prices_ok": row.prices_ok if row else True,
        "n_markets": row.n_markets if row else 0,
        "n_trades": row.n_trades if row else 0,
        "error": row.error if row else None,
        "age_seconds": age_seconds,
        "stale": stale,
        "partial_wallets": int(partial_wallets or 0),
        **auto_worker.status(),
    }


def backfill_wallet(db: Session, address: str, limit: int = 200) -> dict:
    """Pull a wallet's recent live trade history, upsert it, and (re)score it.

    Returns a summary. Marks the wallet's stats partial_history=True because the
    data-api returns a recent window, not guaranteed full lifetime history.
    Only works in live mode (mock has no per-wallet endpoint)."""
    settings = get_settings(db)
    if settings["data_mode"] != "live":
        return {"ok": False, "error": "backfill requires data_mode=live"}

    from .polymarket_client import LivePolymarketClient

    client = LivePolymarketClient()
    try:
        dtos = client.get_wallet_trades(address, limit=limit)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    finally:
        client.close()

    wallet = get_or_create_wallet(db, address)

    # Fetch REAL metadata + resolution for every market the wallet touched. This
    # is what makes P&L reconstruction possible: a placeholder market (resolved
    # =false, prices [0.5,0.5]) can never settle, so the wallet would stay
    # forever "insufficient_data". We only fetch markets we don't already know to
    # be resolved (re-runs stay cheap; upsert_market never un-resolves).
    needed = {dto.market_id for dto in dtos}
    known_resolved = {
        m for (m,) in db.execute(
            select(Market.id).where(Market.id.in_(needed), Market.resolved == True)  # noqa: E712
        ).all()
    }
    to_fetch = list(needed - known_resolved)
    n_resolved_fetched = 0
    try:
        client2 = LivePolymarketClient()
        try:
            for mdto in client2.get_markets_by_conditions(to_fetch):
                upsert_market(db, mdto)
                if mdto.resolved:
                    n_resolved_fetched += 1
        finally:
            client2.close()
    except Exception as exc:  # noqa: BLE001  (best-effort; trades still ingest)
        print(f"[backfill] market metadata fetch failed for {address[:10]}: {exc}")
    # Persist the upserted markets before the existence check below. SessionLocal
    # runs with autoflush=False, so without this the just-added markets are still
    # pending and the stub loop would re-insert them -> duplicate-PK IntegrityError.
    db.flush()
    # Any market still missing (delisted / not returned) gets a minimal stub so
    # the trade FK resolves; it simply stays unsettled.
    existing = {
        m for (m,) in db.execute(select(Market.id).where(Market.id.in_(needed))).all()
    }
    for mid in needed - existing:
        db.add(Market(id=mid, question=f"(market {mid[:10]}…)",
                      outcomes=["Yes", "No"], prices=[0.5, 0.5]))
    db.commit()

    inserted = 0
    for dto in dtos:
        if insert_trade(db, dto, wallet) is not None:
            inserted += 1
    db.commit()
    recompute_wallet_stats(db, wallet, partial=True, reconstruct=True)
    db.commit()
    stat = db.get(WalletStat, wallet.id)
    n_resolved = db.scalar(
        select(func.count()).select_from(Market).where(
            Market.id.in_(needed), Market.resolved == True  # noqa: E712
        )
    )
    return {
        "ok": True, "address": address, "trades_fetched": len(dtos), "trades_inserted": inserted,
        "markets": len(needed),
        "resolved_markets": int(n_resolved or 0),
        "num_settled": stat.num_settled if stat else 0,
        "score": stat.score if stat else 0.0,
        "classification": stat.classification if stat else "insufficient_data",
        "partial_history": True,
    }


# ===========================================================================
# Wallet discovery
# ===========================================================================
def run_discovery(db: Session, max_backfill: int | None = None) -> dict:
    """Discover + rank copy candidates (mock evaluates all wallets; live scans
    recent trades and backfills the top movers)."""
    settings = get_settings(db)
    if settings["data_mode"] == "live":
        purge_mock_data(db)
    if max_backfill is not None:
        settings = {**settings, "max_wallets_to_backfill_per_cycle": max_backfill}
    return discovery.run_discovery(db, settings, backfill_fn=backfill_wallet)


def list_candidates(db: Session, classification: str | None = None,
                    state: str | None = None) -> list[dict]:
    """Candidate rows joined with wallet + profitability stats, ranked by score."""
    stmt = select(WalletCandidate)
    if classification:
        stmt = stmt.where(WalletCandidate.classification == classification)
    if state:
        stmt = stmt.where(WalletCandidate.state == state)
    cands = db.scalars(stmt.order_by(WalletCandidate.copyability_score.desc())).all()
    out: list[dict] = []
    for c in cands:
        wallet = db.get(Wallet, c.wallet_id)
        stat = db.get(WalletStat, c.wallet_id)
        if wallet is None:
            continue
        out.append({
            "wallet_id": c.wallet_id,
            "address": wallet.address,
            "label": wallet.label,
            "copyability_score": c.copyability_score,
            "classification": c.classification,
            "state": c.state,
            "suspected_noise": c.suspected_noise,
            "distinct_markets": c.distinct_markets,
            "reasons": c.reasons or [],
            "copy_enabled": wallet.copy_enabled,
            "last_active": wallet.last_active,
            "partial_history": stat.partial_history if stat else False,
            "profitability_score": stat.score if stat else 0.0,
            "realized_roi": stat.realized_roi if stat else 0.0,
            "win_rate": stat.win_rate if stat else 0.0,
            "num_trades": stat.num_trades if stat else 0,
            "avg_trade_size": stat.avg_trade_size if stat else 0.0,
        })
    return out


def should_run_discovery(db: Session, settings: dict) -> bool:
    if not int(settings.get("auto_discovery_enabled", 0)):
        return False
    row = db.get(IngestStatus, 1)
    if row is None or row.last_discovery_at is None:
        return True
    elapsed_min = (datetime.utcnow() - row.last_discovery_at).total_seconds() / 60.0
    return elapsed_min >= float(settings["discovery_interval_minutes"])


def candidate_detail(db: Session, address: str) -> dict | None:
    """Rich detail for one candidate: recent trades, best/worst categories,
    profit curve, copied paper PnL (if tracked), and a weak-sample warning."""
    wallet = db.scalar(select(Wallet).where(Wallet.address == address))
    if wallet is None:
        return None
    cand = db.get(WalletCandidate, wallet.id)
    stat = db.get(WalletStat, wallet.id)
    trades = db.scalars(
        select(Trade).where(Trade.wallet_id == wallet.id).order_by(Trade.timestamp.desc())
    ).all()

    # best / worst categories from stored category performance
    cats = (stat.category_performance if stat else {}) or {}
    ranked = sorted(cats.items(), key=lambda kv: kv[1], reverse=True)
    best_categories = [{"category": c, "roi": r} for c, r in ranked[:3] if r > 0]
    worst_categories = [{"category": c, "roi": r} for c, r in ranked[-3:] if r < 0]

    # profit curve = cumulative realized pnl over settled units, oldest first.
    # Live wallets carry no per-fill P&L, so reconstruct resolved positions;
    # mock wallets already carry realized P&L on the fills themselves.
    recon = positions_mod.settled_positions(
        trades,
        {m.id: m for m in db.scalars(
            select(Market).where(Market.id.in_({t.market_id for t in trades}))
        ).all()},
    )
    if recon:
        settled = sorted(recon, key=lambda p: p.timestamp)
    else:
        settled = sorted([t for t in trades if t.realized_pnl], key=lambda t: t.timestamp)
    cum = 0.0
    profit_curve = []
    for t in settled:
        cum += t.realized_pnl
        profit_curve.append({"t": t.timestamp.isoformat(), "pnl": round(cum, 2)})

    # copied paper PnL if we've been copying this wallet
    positions = db.scalars(select(PaperPosition).where(PaperPosition.wallet_id == wallet.id)).all()
    copied_pnl = round(sum(p.realized_pnl + p.unrealized_pnl for p in positions), 2)

    recent = [
        {
            "market_id": t.market_id, "outcome": t.outcome, "side": t.side,
            "price": t.price, "size": t.size, "timestamp": t.timestamp.isoformat(),
            "realized_pnl": t.realized_pnl,
        }
        for t in trades[:15]
    ]
    weak_sample = bool(cand and cand.classification == "insufficient_data") or len(settled) < 12

    return {
        "address": wallet.address,
        "label": wallet.label,
        "copy_enabled": wallet.copy_enabled,
        "state": cand.state if cand else "new",
        "copyability_score": cand.copyability_score if cand else 0.0,
        "classification": cand.classification if cand else "insufficient_data",
        "suspected_noise": cand.suspected_noise if cand else False,
        "reasons": cand.reasons if cand else [],
        "partial_history": stat.partial_history if stat else False,
        "num_trades": stat.num_trades if stat else 0,
        "realized_roi": stat.realized_roi if stat else 0.0,
        "win_rate": stat.win_rate if stat else 0.0,
        "avg_trade_size": stat.avg_trade_size if stat else 0.0,
        "distinct_markets": cand.distinct_markets if cand else 0,
        "best_categories": best_categories,
        "worst_categories": worst_categories,
        "profit_curve": profit_curve,
        "copied_paper_pnl": copied_pnl,
        "copied_positions": len(positions),
        "recent_trades": recent,
        "weak_sample": weak_sample,
    }


# ===========================================================================
# Price snapshots (for signal quality)
# ===========================================================================
def record_price_snapshots(db: Session, prune_hours: int = 8) -> int:
    """Snapshot each open market's outcome[0] price; prune old rows."""
    markets = db.scalars(select(Market).where(Market.resolved == False)).all()  # noqa: E712
    now = datetime.utcnow()
    n = 0
    for m in markets:
        if not m.outcomes or not m.prices:
            continue
        db.add(MarketPriceSnapshot(
            market_id=m.id, timestamp=now, outcome=m.outcomes[0], price=float(m.prices[0]),
        ))
        n += 1
    # prune snapshots older than the longest horizon we need (+ margin)
    cutoff = now - timedelta(hours=prune_hours)
    db.query(MarketPriceSnapshot).filter(MarketPriceSnapshot.timestamp < cutoff).delete()
    db.commit()
    return n


# ===========================================================================
# Backtesting
# ===========================================================================
def run_backtest(db: Session, config: dict) -> Backtest:
    """Replay historical trades through every strategy and persist results.

    `config` keys: name, starting_bankroll, train_fraction, category, start_date,
    end_date, min_wallet_score, strategies.
    """
    settings = get_settings(db)
    starting = float(config.get("starting_bankroll") or settings["bankroll"])
    train_fraction = float(config.get("train_fraction") or 0.5)
    category = config.get("category")
    start_date = config.get("start_date")
    end_date = config.get("end_date")
    min_score = config.get("min_wallet_score")
    if min_score is None:
        min_score = settings["min_wallet_score"]
    strategies = config.get("strategies") or list(bt.STRATEGIES)

    # --- load + filter trades -----------------------------------------------
    stmt = select(Trade)
    if start_date:
        stmt = stmt.where(Trade.timestamp >= start_date)
    if end_date:
        stmt = stmt.where(Trade.timestamp <= end_date)
    trades = list(db.scalars(stmt).all())
    if category:
        market_cat = {m.id: m.category for m in db.scalars(select(Market)).all()}
        trades = [t for t in trades if market_cat.get(t.market_id) == category]

    # attach category onto each trade object for the engine (transient attr)
    market_map = {m.id: m for m in db.scalars(select(Market)).all()}
    for t in trades:
        m = market_map.get(t.market_id)
        t.category = m.category if m else None  # type: ignore[attr-defined]

    # --- walk-forward split + train-window classification --------------------
    train, test = bt.split_by_time(trades, train_fraction)
    train_by_wallet: dict[int, list] = {}
    for t in train:
        train_by_wallet.setdefault(t.wallet_id, []).append(t)
    wallet_class: dict[int, str] = {}
    wallet_score: dict[int, float] = {}
    for wid, wtrades in train_by_wallet.items():
        res = scoring.score_wallet(wtrades)
        wallet_class[wid] = res.classification
        wallet_score[wid] = res.score

    params = bt.BacktestParams(
        starting_bankroll=starting,
        max_position_pct=settings["max_position_pct"],
        slippage_cents=settings["slippage_cents"],
        min_wallet_score=float(min_score),
        strategies=strategies,
    )
    results = bt.replay(test, market_map, wallet_class, wallet_score, params)

    # --- persist -------------------------------------------------------------
    backtest = Backtest(
        name=config.get("name") or "backtest",
        config={
            "starting_bankroll": starting, "train_fraction": train_fraction,
            "category": category, "min_wallet_score": float(min_score),
            "strategies": strategies, "n_train_trades": len(train), "n_test_trades": len(test),
            "start_date": start_date.isoformat() if hasattr(start_date, "isoformat") else start_date,
            "end_date": end_date.isoformat() if hasattr(end_date, "isoformat") else end_date,
        },
        summary={
            s: {"roi": r.roi, "total_pnl": r.total_pnl, "num_trades": r.num_trades,
                "win_rate": r.win_rate, "max_drawdown": r.max_drawdown}
            for s, r in results.items()
        },
    )
    db.add(backtest)
    db.flush()
    for strat, r in results.items():
        db.add(BacktestResult(
            backtest_id=backtest.id, strategy=strat,
            starting_bankroll=r.starting_bankroll, ending_bankroll=r.ending_bankroll,
            total_pnl=r.total_pnl, roi=r.roi, max_drawdown=r.max_drawdown,
            win_rate=r.win_rate, num_trades=r.num_trades, avg_trade_return=r.avg_trade_return,
            best_trade=r.best_trade, worst_trade=r.worst_trade, equity_curve=r.equity_curve,
        ))
        for st in r.trades:
            db.add(BacktestTrade(
                backtest_id=backtest.id, strategy=strat, wallet_id=st.wallet_id,
                market_id=st.market_id, category=st.category, outcome=st.outcome, side=st.side,
                size=st.size, entry_price=st.entry_price, exit_price=st.exit_price,
                pnl=st.pnl, return_pct=st.return_pct, opened_at=st.opened_at,
                closed_at=st.closed_at, reason=st.reason,
            ))
    db.commit()
    return backtest
