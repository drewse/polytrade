"""
Per-wallet attribution.

Answers "which copied wallets actually made us money?" by aggregating the paper
positions (and the signals that spawned them) back to their source wallet.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import PaperPosition, PaperSignal, Wallet, WalletStat


def compute_wallet_attribution(db: Session) -> list[dict]:
    wallets = {w.id: w for w in db.scalars(select(Wallet)).all()}
    stats = {s.wallet_id: s for s in db.scalars(select(WalletStat)).all()}
    positions = db.scalars(select(PaperPosition)).all()
    copied_signal_counts: dict[int, int] = {}
    for sig in db.scalars(select(PaperSignal).where(PaperSignal.copied == True)).all():  # noqa: E712
        copied_signal_counts[sig.wallet_id] = copied_signal_counts.get(sig.wallet_id, 0) + 1

    agg: dict[int, dict] = {}
    for p in positions:
        a = agg.setdefault(
            p.wallet_id,
            {
                "copied_positions": 0, "closed_positions": 0, "winning_positions": 0,
                "realized_pnl": 0.0, "unrealized_pnl": 0.0, "entry_sum": 0.0,
            },
        )
        a["copied_positions"] += 1
        a["entry_sum"] += p.entry_price
        if p.status == "closed":
            a["closed_positions"] += 1
            a["realized_pnl"] += p.realized_pnl
            if p.realized_pnl > 0:
                a["winning_positions"] += 1
        else:
            a["unrealized_pnl"] += p.unrealized_pnl

    out: list[dict] = []
    for wid, a in agg.items():
        wallet = wallets.get(wid)
        if wallet is None:
            continue
        stat = stats.get(wid)
        total_size = sum(
            p.size for p in positions if p.wallet_id == wid
        ) or 1.0
        total_pnl = a["realized_pnl"] + a["unrealized_pnl"]
        out.append(
            {
                "wallet_id": wid,
                "address": wallet.address,
                "label": wallet.label,
                "score": stat.score if stat else 0.0,
                "classification": stat.classification if stat else "insufficient_data",
                "copied_signals": copied_signal_counts.get(wid, 0),
                "copied_positions": a["copied_positions"],
                "closed_positions": a["closed_positions"],
                "winning_positions": a["winning_positions"],
                "win_rate": round(a["winning_positions"] / a["closed_positions"], 4)
                if a["closed_positions"] else 0.0,
                "realized_pnl": round(a["realized_pnl"], 2),
                "unrealized_pnl": round(a["unrealized_pnl"], 2),
                "total_pnl": round(total_pnl, 2),
                "roi": round(total_pnl / total_size, 4),
                "avg_entry_price": round(a["entry_sum"] / a["copied_positions"], 4)
                if a["copied_positions"] else 0.0,
            }
        )
    out.sort(key=lambda r: r["total_pnl"], reverse=True)
    return out
