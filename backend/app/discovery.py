"""
Wallet discovery pipeline.

Turns "a stream of trades" into "ranked copy candidates":

  live mode:  scan recent live trades -> extract unique wallets -> filter out
              tiny/dust traders -> backfill the top N (read-only) -> score
              copyability -> upsert candidates.
  mock mode:  there is no per-wallet live endpoint, so evaluate every wallet that
              already has trade history in the DB (the seeded cohorts) and score
              their copyability.

The extraction/filtering/selection steps are pure functions so they're easy to
unit test (candidate filtering, backfill-limit, etc.). The DB orchestration is
`run_discovery()`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import copyability
from .models import IngestStatus, Trade, Wallet, WalletCandidate, WalletStat


@dataclass
class CandidateSeed:
    address: str
    batch_trades: int
    batch_notional: float

    @property
    def avg_notional(self) -> float:
        return self.batch_notional / self.batch_trades if self.batch_trades else 0.0


# ---------------------------------------------------------------------------
# pure helpers (unit-testable, no DB / network)
# ---------------------------------------------------------------------------
def extract_candidates(trades) -> dict[str, CandidateSeed]:
    """Aggregate a batch of trades by wallet address."""
    seeds: dict[str, CandidateSeed] = {}
    for t in trades:
        addr = getattr(t, "wallet_address", None) or getattr(t, "address", None)
        if not addr:
            continue
        s = seeds.get(addr)
        if s is None:
            seeds[addr] = CandidateSeed(address=addr, batch_trades=1, batch_notional=float(t.size))
        else:
            s.batch_trades += 1
            s.batch_notional += float(t.size)
    return seeds


def filter_candidates(seeds: dict[str, CandidateSeed], min_notional: float) -> list[CandidateSeed]:
    """Drop dust traders: require average batch notional >= min_notional."""
    return [s for s in seeds.values() if s.avg_notional >= min_notional]


def select_for_backfill(candidates: list[CandidateSeed], max_n: int) -> list[CandidateSeed]:
    """Pick the top `max_n` candidates by batch notional (biggest traders first)."""
    ordered = sorted(candidates, key=lambda s: s.batch_notional, reverse=True)
    return ordered[: max(0, max_n)]


# ---------------------------------------------------------------------------
# DB orchestration
# ---------------------------------------------------------------------------
def _evaluate_wallet(db: Session, wallet: Wallet, min_trade_count: int) -> WalletCandidate:
    """Score one wallet's copyability and upsert its candidate row (preserving
    any manual track/ignore decision)."""
    stat = db.get(WalletStat, wallet.id)
    trades = db.scalars(select(Trade).where(Trade.wallet_id == wallet.id)).all()
    if stat is None:
        # build a minimal stat-less result
        result = copyability.score_copyability(
            _StubStat(), list(trades), min_trade_count=min_trade_count
        )
    else:
        result = copyability.score_copyability(stat, list(trades), min_trade_count=min_trade_count)

    cand = db.get(WalletCandidate, wallet.id)
    if cand is None:
        cand = WalletCandidate(wallet_id=wallet.id, state="new")
        db.add(cand)
    cand.copyability_score = result.copyability_score
    cand.classification = result.classification
    cand.suspected_noise = result.suspected_noise
    cand.distinct_markets = result.distinct_markets
    cand.reasons = result.reasons
    return cand


class _StubStat:
    num_trades = 0
    realized_roi = 0.0
    win_rate = 0.0
    avg_trade_size = 0.0
    recency_score = 0.0
    consistency = 0.0
    category_performance: dict = {}


def run_discovery(db: Session, settings: dict, backfill_fn=None) -> dict:
    """Discover + score copy candidates.

    `backfill_fn(db, address, limit) -> dict` is injected (services.backfill_wallet)
    so this module doesn't import services (avoids a circular import)."""
    data_mode = settings["data_mode"]
    min_trade_count = int(settings["min_candidate_trade_count"])
    min_notional = float(settings["min_candidate_notional"])
    max_backfill = int(settings["max_wallets_to_backfill_per_cycle"])

    n_backfilled = 0
    candidate_wallet_ids: list[int] = []

    scan_error: str | None = None
    if data_mode == "live":
        from .polymarket_client import LivePolymarketClient

        # Scan recent live trades for fresh wallets to backfill. A failure here
        # (e.g. the public data-api rate-limiting us) must not abort discovery —
        # we degrade to re-evaluating the wallets we already hold history for.
        client = LivePolymarketClient()
        try:
            trades = client.get_recent_trades(limit=200)
        except Exception as exc:  # noqa: BLE001
            trades = []
            scan_error = str(exc)
            print(f"[discovery] recent-trades scan failed: {exc}")
        finally:
            client.close()
        seeds = extract_candidates(trades)
        picked = select_for_backfill(filter_candidates(seeds, min_notional), max_backfill)
        for seed in picked:
            if backfill_fn is not None:
                res = backfill_fn(db, seed.address, limit=300)
                if res.get("ok"):
                    n_backfilled += 1
            wallet = db.scalar(select(Wallet).where(Wallet.address == seed.address))
            if wallet:
                candidate_wallet_ids.append(wallet.id)
        # Always also (re)evaluate every wallet we already have history for, so
        # previously-backfilled cohorts get scored even when the scan is empty.
        existing = [wid for (wid,) in db.execute(
            select(Trade.wallet_id).group_by(Trade.wallet_id)
        ).all()]
        candidate_wallet_ids = list(dict.fromkeys(candidate_wallet_ids + existing))
    else:
        # mock: evaluate every wallet that has any trade history
        rows = db.execute(
            select(Trade.wallet_id, func.count()).group_by(Trade.wallet_id)
        ).all()
        candidate_wallet_ids = [wid for (wid, _) in rows]

    summary: dict[str, int] = {
        "elite_candidate": 0, "good_candidate": 0, "watchlist": 0,
        "ignore": 0, "insufficient_data": 0,
    }
    for wid in candidate_wallet_ids:
        wallet = db.get(Wallet, wid)
        if wallet is None:
            continue
        cand = _evaluate_wallet(db, wallet, min_trade_count)
        summary[cand.classification] = summary.get(cand.classification, 0) + 1
    db.commit()

    # stamp last discovery time
    st = db.get(IngestStatus, 1)
    if st is None:
        st = IngestStatus(id=1)
        db.add(st)
    st.last_discovery_at = datetime.utcnow()
    db.commit()

    return {
        "data_mode": data_mode,
        "evaluated": len(candidate_wallet_ids),
        "backfilled": n_backfilled,
        "by_classification": summary,
        "scan_error": scan_error,
    }


def set_candidate_state(db: Session, address: str, state: str) -> WalletCandidate | None:
    """Track / ignore a candidate. Track enables copying; ignore disables it."""
    wallet = db.scalar(select(Wallet).where(Wallet.address == address))
    if wallet is None:
        return None
    cand = db.get(WalletCandidate, wallet.id)
    if cand is None:
        cand = WalletCandidate(wallet_id=wallet.id)
        db.add(cand)
    cand.state = state
    if state == "tracked":
        wallet.copy_enabled = True
    elif state == "ignored":
        wallet.copy_enabled = False
    db.commit()
    return cand
