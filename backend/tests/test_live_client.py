"""Fixture-based tests for the live Polymarket parser.

Fixtures in tests/fixtures/ were recorded from the real public APIs. These tests
never hit the network — they prove the parser handles the real shapes plus the
awkward edge cases (JSON-string arrays, missing optional fields, empties, and
malformed responses that must fail loudly).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import polymarket_client as pc
from app.polymarket_client import (
    LiveDataError,
    LiveParseError,
    parse_market,
    parse_markets,
    parse_midpoint,
    parse_orderbook,
    parse_trade,
    parse_trades,
    resolved_outcome_from_prices,
)

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


# --- normal responses --------------------------------------------------------
def test_parse_active_markets_fixture():
    markets = parse_markets(load("active_markets.json"))
    assert len(markets) >= 1
    for m in markets:
        assert m.id and m.question
        assert isinstance(m.outcomes, list) and len(m.outcomes) >= 2
        assert len(m.prices) == len(m.outcomes)
        assert all(isinstance(p, float) for p in m.prices)
        assert isinstance(m.token_ids, list)


def test_parse_market_detail_fixture():
    m = parse_market(load("market_detail.json"))
    assert m.id.startswith("0x") or m.id  # conditionId
    assert m.outcomes == ["Yes", "No"] or len(m.outcomes) >= 2
    assert m.liquidity >= 0 and m.volume >= 0


def test_parse_trades_fixture_and_usd_size():
    trades = parse_trades(load("trades.json"))
    assert len(trades) >= 1
    for t in trades:
        assert t.wallet_address and t.market_id
        assert t.side in ("buy", "sell")
        # USD notional = shares * price
        assert abs(t.size - round(t.shares * t.price, 4)) < 1e-6


def test_parse_wallet_trades_fixture():
    trades = parse_trades(load("wallet_trades.json"))
    assert all(t.wallet_address for t in trades)
    # all rows should be the same wallet (it was a user-filtered query)
    assert len({t.wallet_address.lower() for t in trades}) == 1


def test_parse_midpoint_and_orderbook_fixtures():
    assert 0.0 <= parse_midpoint(load("price_midpoint.json")) <= 1.0
    book = parse_orderbook(load("orderbook.json"))
    assert book["bid"] is not None and book["ask"] is not None
    assert 0.0 <= book["mid"] <= 1.0


def test_resolved_market_fixture_has_winner():
    m = parse_market(load("resolved_market.json"))
    assert m.resolved is True
    assert m.resolved_outcome in m.outcomes
    assert m.resolved_at is not None


# --- JSON-string vs array outcomes ------------------------------------------
def test_outcomes_as_json_string():
    row = {"conditionId": "0xabc", "question": "Q?",
           "outcomes": '["Yes", "No"]', "outcomePrices": '["0.3", "0.7"]'}
    m = parse_market(row)
    assert m.outcomes == ["Yes", "No"]
    assert m.prices == [0.3, 0.7]


def test_outcomes_as_real_array():
    row = {"conditionId": "0xabc", "question": "Q?",
           "outcomes": ["A", "B", "C"], "outcomePrices": [0.2, 0.3, 0.5]}
    m = parse_market(row)
    assert m.outcomes == ["A", "B", "C"]
    assert m.prices == [0.2, 0.3, 0.5]


# --- missing optional fields -------------------------------------------------
def test_market_missing_optional_fields_uses_defaults():
    m = parse_market({"conditionId": "0xabc", "question": "Q?"})  # no prices/liq/vol/cat
    assert m.outcomes == ["Yes", "No"]
    assert len(m.prices) == 2
    assert m.liquidity == 0.0 and m.volume == 0.0
    assert m.category is None
    assert m.resolved is False


def test_prices_padded_to_outcomes():
    m = parse_market({"conditionId": "0xabc", "question": "Q?",
                      "outcomes": ["A", "B", "C"], "outcomePrices": '["0.5"]'})
    assert len(m.prices) == 3  # padded


def test_trade_missing_optional_outcome_defaults():
    t = parse_trade({"proxyWallet": "0xw", "conditionId": "0xm", "price": 0.4, "size": 10})
    assert t.outcome == "Yes"
    assert t.side == "buy"


# --- empty trades ------------------------------------------------------------
def test_empty_trades_list():
    assert parse_trades([]) == []


def test_empty_data_wrapper():
    assert parse_trades({"data": []}) == []


# --- malformed responses must fail loudly -----------------------------------
def test_markets_not_a_list_raises():
    with pytest.raises(LiveDataError):
        parse_markets({"unexpected": "object"})


def test_trades_not_a_list_raises():
    with pytest.raises(LiveDataError):
        parse_trades("totally wrong")


def test_market_missing_required_id_raises():
    with pytest.raises(LiveParseError):
        parse_market({"question": "no id here"})


def test_market_missing_required_question_raises():
    with pytest.raises(LiveParseError):
        parse_market({"conditionId": "0xabc"})


def test_trade_missing_wallet_raises():
    with pytest.raises(LiveParseError):
        parse_trade({"conditionId": "0xm", "price": 0.4, "size": 10})


def test_midpoint_malformed_raises():
    with pytest.raises(LiveDataError):
        parse_midpoint({"no_mid": 1})


# --- helpers -----------------------------------------------------------------
def test_resolved_outcome_from_prices():
    assert resolved_outcome_from_prices(["Yes", "No"], [0.999, 0.001]) == "Yes"
    assert resolved_outcome_from_prices(["Yes", "No"], [0.001, 0.999]) == "No"
    assert resolved_outcome_from_prices(["Yes", "No"], [0.5, 0.5]) is None


def test_maybe_json_list_variants():
    assert pc._maybe_json_list('["a","b"]') == ["a", "b"]
    assert pc._maybe_json_list(["a", "b"]) == ["a", "b"]
    assert pc._maybe_json_list(None) is None
    assert pc._maybe_json_list("not json") is None
