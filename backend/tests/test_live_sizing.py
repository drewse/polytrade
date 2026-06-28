"""Dynamic risk-aware sizing tests — the 4 spec examples, confidence tiers, edge
scaling, the share cap, the per-market cap, min-stake guard, the flat fallback,
and a full breakdown. Sizing ONLY: no execution/routing/slippage involved."""
from __future__ import annotations

import pytest

from app import live


@pytest.fixture
def big_cfg(monkeypatch):
    """Config where only the share cap / per-market cap can bind (target large)."""
    monkeypatch.setenv("LIVE_POSITION_USD", "50")
    monkeypatch.setenv("LIVE_MAX_PER_MARKET", "6")
    monkeypatch.setenv("LIVE_MAX_SHARES_PER_TRADE", "20")
    monkeypatch.setenv("LIVE_MAX_TOTAL_RISK", "1000")
    monkeypatch.setenv("LIVE_MAX_PER_WALLET", "1000")
    monkeypatch.setenv("LIVE_MIN_STAKE", "1.0")
    monkeypatch.setenv("LIVE_ENABLE_DYNAMIC_SIZING", "true")
    return live.get_config()


def _stake(cfg, price, confidence=85, edge=0.0, **caps):
    kw = dict(available_cash=1e9, total_open=0.0, wallet_exposure=0.0, market_exposure=0.0)
    kw.update(caps)
    return live.dynamic_stake(cfg, price=price, confidence=confidence, edge=edge, **kw)


# --- the 4 spec examples ----------------------------------------------------
def test_spec_examples(big_cfg):
    # 15c: 20 shares * 0.15 = $3.00 (share cap)
    s, d = _stake(big_cfg, 0.15)
    assert s == 3.00 and d["limiting_constraint"] == "share_cap" and d["final_shares"] == 20.0
    # 25c: 20 * 0.25 = $5.00 (share cap)
    s, d = _stake(big_cfg, 0.25)
    assert s == 5.00 and d["limiting_constraint"] == "share_cap"
    # 35c: 20 * 0.35 = $7.00 -> per-market $6 wins
    s, d = _stake(big_cfg, 0.35)
    assert s == 6.00 and d["limiting_constraint"] == "remaining_per_market"
    # 80c: 20 * 0.80 = $16 -> per-market $6 wins
    s, d = _stake(big_cfg, 0.80)
    assert s == 6.00 and d["limiting_constraint"] == "remaining_per_market"


def test_share_cap_prevents_oversized_cheap_contracts(big_cfg):
    # a 5c contract: flat $5 would buy 100 shares; the cap holds it to 20 shares = $1
    s, d = _stake(big_cfg, 0.05)
    assert d["final_shares"] <= big_cfg.max_shares_per_trade
    assert s == round(0.05 * 20, 2)


# --- confidence tiers -------------------------------------------------------
@pytest.mark.parametrize("conf,mult", [(72, 0.60), (77, 0.75), (82, 0.90),
                                       (87, 1.00), (92, 1.10), (66, 0.50)])
def test_confidence_tiers(monkeypatch, conf, mult):
    monkeypatch.setenv("LIVE_POSITION_USD", "5")
    monkeypatch.setenv("LIVE_MAX_SHARES_PER_TRADE", "20")
    monkeypatch.setenv("LIVE_MAX_PER_MARKET", "6")
    cfg = live.get_config()
    # price 0.5 so the share cap (10) and per-market (6) don't bind below target
    s, d = live.dynamic_stake(cfg, price=0.5, confidence=conf, edge=0.0, available_cash=1e9,
                              total_open=0, wallet_exposure=0, market_exposure=0)
    assert d["confidence_multiplier"] == mult
    assert d["raw_target_stake"] == round(5 * mult, 4)


# --- edge scaling -----------------------------------------------------------
def test_edge_increases_target_capped_at_10pct(monkeypatch):
    monkeypatch.setenv("LIVE_POSITION_USD", "5")
    cfg = live.get_config()
    base = live.dynamic_stake(cfg, price=0.5, confidence=85, edge=0.0, available_cash=1e9,
                              total_open=0, wallet_exposure=0, market_exposure=0)[1]["raw_target_stake"]
    hi = live.dynamic_stake(cfg, price=0.5, confidence=85, edge=0.20, available_cash=1e9,
                            total_open=0, wallet_exposure=0, market_exposure=0)[1]["raw_target_stake"]
    over = live.dynamic_stake(cfg, price=0.5, confidence=85, edge=0.90, available_cash=1e9,
                              total_open=0, wallet_exposure=0, market_exposure=0)[1]["raw_target_stake"]
    assert hi > base                      # higher edge -> larger target
    assert hi == round(base * 1.10, 4)    # capped at +10%
    assert over == hi                     # edge beyond 0.20 does not exceed the cap


# --- hard caps still bind ---------------------------------------------------
def test_available_cash_and_caps_still_bind(big_cfg):
    # available cash binds
    s, d = _stake(big_cfg, 0.5, available_cash=2.0)
    assert s == 2.0 and d["limiting_constraint"] == "available_cash"
    # per-wallet binds
    s, d = _stake(big_cfg, 0.5, wallet_exposure=998.0)  # 1000-998 = 2
    assert s == 2.0 and d["limiting_constraint"] == "remaining_per_wallet"
    # total-risk binds
    s, d = _stake(big_cfg, 0.5, total_open=998.5)       # 1000-998.5 = 1.5
    assert s == 1.5 and d["limiting_constraint"] == "remaining_total_risk"


def test_below_min_stake_returns_none(big_cfg):
    s, d = _stake(big_cfg, 0.5, available_cash=0.5)
    assert s is None and d["rejected"] is True


# --- breakdown completeness (Phase: logging) --------------------------------
def test_breakdown_has_all_fields(big_cfg):
    _, d = _stake(big_cfg, 0.35, confidence=88, edge=0.1)
    for k in ("market_price", "confidence", "edge", "confidence_multiplier", "edge_factor",
              "share_cap", "raw_target_stake", "final_stake", "final_shares",
              "limiting_constraint", "constraints"):
        assert k in d
    assert set(d["constraints"]) == {"dynamic_target", "available_cash", "remaining_total_risk",
                                     "remaining_per_market", "remaining_per_wallet", "share_cap"}


# --- flat fallback when disabled --------------------------------------------
def test_flat_fallback_when_dynamic_disabled(monkeypatch, in_memory_db):
    from app.models import Market
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("LIVE_EXECUTOR", "dry_run")
    monkeypatch.setenv("LIVE_STARTING_BANKROLL", "40")
    monkeypatch.setenv("LIVE_POSITION_USD", "5")
    monkeypatch.setenv("LIVE_ENABLE_DYNAMIC_SIZING", "false")   # legacy flat sizing
    db = in_memory_db
    m = Market(id="m1", question="Q", outcomes=["Yes", "No"], token_ids=["t1", "t2"], prices=[0.5, 0.5])
    db.add(m); db.flush()
    ex = live.process_signal(db, strategy_key="s", wallet="0xw", signal_id=1, market=m,
                             outcome="Yes", price=0.05, confidence=92, edge=0.3, entry_reason="t")
    # flat: ignores confidence/edge/share-cap -> flat $5 (no share-count limit)
    assert ex is not None and ex.size_usd == 5.0
    assert ex.sizing_detail["method"] == "flat"


def test_dynamic_attaches_breakdown_to_execution(monkeypatch, in_memory_db):
    from app.models import Market
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("LIVE_EXECUTOR", "dry_run")
    monkeypatch.setenv("LIVE_STARTING_BANKROLL", "40")
    monkeypatch.setenv("LIVE_POSITION_USD", "50")
    monkeypatch.setenv("LIVE_MAX_SHARES_PER_TRADE", "20")
    monkeypatch.setenv("LIVE_MAX_PER_MARKET", "6")
    monkeypatch.setenv("LIVE_ENABLE_DYNAMIC_SIZING", "true")
    db = in_memory_db
    m = Market(id="m2", question="Q", outcomes=["Yes", "No"], token_ids=["t1", "t2"], prices=[0.15, 0.85])
    db.add(m); db.flush()
    ex = live.process_signal(db, strategy_key="s", wallet="0xw", signal_id=2, market=m,
                             outcome="Yes", price=0.15, confidence=88, edge=0.05, entry_reason="t")
    assert ex is not None
    assert ex.sizing_detail["method"] == "dynamic"
    assert ex.sizing_detail["limiting_constraint"] == "share_cap"
    assert ex.size_usd == 3.0   # 20 shares * 0.15
