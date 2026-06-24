from datetime import datetime

from app import attribution
from app.models import Market, PaperPosition, PaperSignal, Wallet, WalletStat


def _setup(db):
    w = Wallet(address="0xsharp", label="sharp1", copy_enabled=True)
    db.add(w)
    db.flush()
    db.add(WalletStat(wallet_id=w.id, score=80, classification="sharp", num_trades=50, win_rate=0.7))
    db.add(Market(id="m1", question="Q1", outcomes=["Yes", "No"], prices=[0.5, 0.5]))
    # a copied signal
    sig = PaperSignal(wallet_id=w.id, market_id="m1", outcome="Yes", observed_price=0.4,
                      suggested_entry=0.4, confidence=80, copied=True)
    db.add(sig)
    db.flush()
    # one winning closed, one losing closed, one open
    db.add(PaperPosition(signal_id=sig.id, wallet_id=w.id, market_id="m1", outcome="Yes",
                         side="buy", size=100, shares=250, entry_price=0.40, current_price=1.0,
                         exit_price=1.0, status="closed", realized_pnl=150.0,
                         opened_at=datetime(2026, 1, 1), closed_at=datetime(2026, 1, 2)))
    db.add(PaperPosition(signal_id=sig.id, wallet_id=w.id, market_id="m1", outcome="No",
                         side="buy", size=100, shares=200, entry_price=0.50, current_price=0.0,
                         exit_price=0.0, status="closed", realized_pnl=-100.0,
                         opened_at=datetime(2026, 1, 1), closed_at=datetime(2026, 1, 2)))
    db.add(PaperPosition(signal_id=sig.id, wallet_id=w.id, market_id="m1", outcome="Yes",
                         side="buy", size=100, shares=250, entry_price=0.40, current_price=0.50,
                         unrealized_pnl=25.0, status="open", opened_at=datetime(2026, 1, 3)))
    db.commit()
    return w


def test_wallet_attribution_aggregates_pnl(in_memory_db):
    db = in_memory_db
    w = _setup(db)
    rows = attribution.compute_wallet_attribution(db)
    assert len(rows) == 1
    r = rows[0]
    assert r["wallet_id"] == w.id
    assert r["copied_positions"] == 3
    assert r["closed_positions"] == 2
    assert r["winning_positions"] == 1
    assert r["win_rate"] == 0.5
    assert r["realized_pnl"] == 50.0           # 150 - 100
    assert r["unrealized_pnl"] == 25.0
    assert r["total_pnl"] == 75.0              # realized + unrealized
    assert r["copied_signals"] == 1
    # avg entry over 3 positions: (0.40 + 0.50 + 0.40)/3
    assert abs(r["avg_entry_price"] - 0.4333) < 0.001


def test_attribution_roi_uses_total_size(in_memory_db):
    db = in_memory_db
    _setup(db)
    r = attribution.compute_wallet_attribution(db)[0]
    # total size = 300, total pnl = 75 -> roi 0.25
    assert abs(r["roi"] - 0.25) < 1e-6
