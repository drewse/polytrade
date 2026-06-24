"""
Deterministic mock data provider (v2 — research-grade).

Generates a realistic fake Polymarket "world" so the whole app + backtester +
discovery run end-to-end with no network access. Wallets are generated as
discovery *cohorts* (see COHORTS) so the copyability engine surfaces a realistic
mix of candidates:

  * 46 wallets across cohorts (tuned so discovery finds ~5 elite, ~8 good,
    ~10 watchlist, and a large ignore/noise/insufficient bucket):
      - elite        : high win rate, many diversified trades, recent
      - good         : solid edge, moderate sample
      - watchlist    : marginal edge and/or small sample
      - noise        : micro-notional spam in few markets (spoof-like) -> ignore
      - bad          : reliably wrong (fade candidates) -> ignore
      - insider      : ~90% win but tiny sample (too-good-to-be-true) -> flagged
      - insufficient : too few trades to judge
  * 100 markets across 6 categories, ~20% of them low-liquidity (slippage trap)
  * per-wallet historical trades (~4k total) -> realized PnL
  * whale trades (occasional very large size) -> drives whale_shock_reversion
  * 50 recent trades + ongoing fresh trades each poll
  * resolved_at timestamps so the backtester can build an equity curve over time

The world is built from a fixed seed, so the wallets/markets the worker sees in
`get_recent_trades()` always match the ones written by the seed routine.
"""
from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .polymarket_client import MarketDTO, TradeDTO

WORLD_SEED = 42
N_WALLETS = 30
N_MARKETS = 100
N_HISTORICAL_TRADES = 5_000
N_RECENT_TRADES = 50

CATEGORIES = ["Politics", "Sports", "Crypto", "Economics", "Pop Culture", "Science"]
WHALE_SIZE = 6_000.0  # trades at/above this are treated as "whale" shocks

_TEMPLATES = {
    "Politics": [
        "Will {who} win the {year} {race}?",
        "Will {who} be confirmed before {year}?",
        "Will {race} turnout exceed 60% in {year}?",
    ],
    "Sports": [
        "Will the {team} win the {year} championship?",
        "Will {who} score in the next match?",
        "Will the {team} make the playoffs in {year}?",
    ],
    "Crypto": [
        "Will BTC close above ${k}k by end of {year}?",
        "Will ETH flip ${k}00 before {year}?",
        "Will a spot {coin} ETF launch in {year}?",
    ],
    "Economics": [
        "Will the Fed cut rates by {year}?",
        "Will US CPI come in below 3% in {year}?",
        "Will unemployment exceed 5% in {year}?",
    ],
    "Pop Culture": [
        "Will {who} win Album of the Year in {year}?",
        "Will the {team} movie gross over ${k}00M in {year}?",
        "Will {who} announce a tour before {year}?",
    ],
    "Science": [
        "Will a crewed mission reach {coin} by {year}?",
        "Will fusion net-energy be confirmed in {year}?",
        "Will an AGI claim be widely accepted in {year}?",
    ],
}
_WHO = ["Smith", "Johnson", "Lee", "Patel", "Garcia", "Nguyen", "Brown", "the incumbent"]
_TEAM = ["Eagles", "Lakers", "Rovers", "United", "Dynamo", "Comets", "Titans"]
_RACE = ["presidential election", "Senate race", "mayoral race", "primary"]
_COIN = ["Mars", "the Moon", "Solana", "Bitcoin"]


@dataclass
class _World:
    wallets: list[dict] = field(default_factory=list)
    markets: list[MarketDTO] = field(default_factory=list)
    historical_trades: list[TradeDTO] = field(default_factory=list)
    recent_trades: list[TradeDTO] = field(default_factory=list)
    # market_id -> winning outcome (for resolved markets), used by helpers
    resolution: dict[str, str] = field(default_factory=dict)


def _addr(i: int) -> str:
    return "0x" + format(0xA11CE0000 + i * 0x1F2B3C, "040x")[-40:]


def _p_win(skill: float) -> float:
    """Map a skill in [-1, 1] to a per-trade probability of picking the winner."""
    return min(0.96, max(0.04, 0.5 + 0.42 * skill))


# Discovery cohorts. Tuned so the copyability engine classifies them into the
# intended buckets (~5 elite, ~8 good, ~10 watchlist, the rest ignore/noise/
# insufficient). Each tuple:
#   name, count, (p_win lo,hi), (n_trades lo,hi), (notional mu,sigma),
#   (market_pool lo,hi), recency_days
COHORTS = [
    ("elite",        5, (0.71, 0.80), (95, 170), (340, 150), (22, 38), 5),
    ("good",         8, (0.60, 0.66), (45, 85),  (190, 90),  (13, 22), 12),
    ("watchlist",   10, (0.52, 0.575), (18, 32), (120, 55),  (8, 14),  22),
    ("noise",       12, (0.47, 0.53), (38, 85),  (4.0, 2.0), (2, 4),   25),
    ("bad",          6, (0.28, 0.39), (30, 55),  (150, 80),  (10, 18), 30),
    ("insider",      2, (0.90, 0.96), (10, 16),  (240, 120), (6, 10),  10),
    ("insufficient", 3, (0.50, 0.70), (6, 11),   (120, 60),  (4, 8),   45),
]


def _make_wallets(rng: random.Random) -> list[dict]:
    """Build the wallet roster as discovery cohorts (see COHORTS)."""
    roster: list[dict] = []
    i = 0
    for name, count, pwin, nrange, notional, pool, recency in COHORTS:
        for _ in range(count):
            roster.append({
                "address": _addr(i), "label": f"wallet-{i:02d}", "archetype": name,
                "p_win": round(rng.uniform(*pwin), 3),
                "n_trades": rng.randint(*nrange),
                "notional_mu": notional[0], "notional_sigma": notional[1],
                "pool_size": rng.randint(*pool),
                "recency_days": recency,
            })
            i += 1
    rng.shuffle(roster)
    return roster


def _other_outcome(market: MarketDTO, outcome: str) -> str:
    return next((o for o in market.outcomes if o != outcome), outcome)


def _trade_pnl(side: str, price: float, size: float, winning_outcome: str, outcome: str) -> float:
    won = outcome == winning_outcome
    shares = size / max(price, 1e-6)
    if side == "buy":
        return shares * (1.0 - price) if won else -size
    return size if not won else -shares * (1.0 - price)


def build_world(seed: int = WORLD_SEED) -> _World:
    rng = random.Random(seed)
    world = _World()
    now = datetime.now(timezone.utc)

    world.wallets = _make_wallets(rng)

    # --- markets -------------------------------------------------------------
    for i in range(N_MARKETS):
        cat = CATEGORIES[i % len(CATEGORIES)]
        tmpl = rng.choice(_TEMPLATES[cat])
        question = tmpl.format(
            who=rng.choice(_WHO), team=rng.choice(_TEAM), race=rng.choice(_RACE),
            coin=rng.choice(_COIN), year=rng.choice([2026, 2027]),
            k=rng.choice([80, 100, 120, 150]),
        )
        price_yes = round(rng.uniform(0.08, 0.92), 3)
        low_liq = rng.random() < 0.2  # ~20% are thin / slippage traps
        liquidity = round(rng.uniform(120, 800) if low_liq else rng.uniform(2_000, 60_000), 2)
        volume = round(liquidity * rng.uniform(1.5, 12), 2)
        resolved = rng.random() < 0.6
        resolved_outcome = ("Yes" if rng.random() < price_yes else "No") if resolved else None
        m = MarketDTO(
            id=f"0xmkt{i:04d}",
            question=question,
            slug=f"market-{i:04d}",
            category=cat,
            outcomes=["Yes", "No"],
            prices=[price_yes, round(1 - price_yes, 3)],
            liquidity=liquidity,
            volume=volume,
            resolved=resolved,
            resolved_outcome=resolved_outcome,
        )
        world.markets.append(m)
        if resolved_outcome:
            world.resolution[m.id] = resolved_outcome

    resolved_markets = [m for m in world.markets if m.resolved]
    counter = itertools.count(1)
    # track latest trade timestamp per market so resolved_at can sit *after* it
    latest_trade_ts: dict[str, datetime] = {}

    # --- historical trades (per-wallet, cohort-driven) -----------------------
    pool_source = resolved_markets or world.markets
    for w in world.wallets:
        pool = rng.sample(pool_source, k=min(w["pool_size"], len(pool_source)))
        last_age = rng.uniform(0.2, w["recency_days"])  # most-recent trade age (days)
        for j in range(w["n_trades"]):
            market = rng.choice(pool)
            age = last_age if j == 0 else rng.uniform(last_age, 240)
            ts = now - timedelta(days=age, hours=rng.uniform(0, 24))
            won = rng.random() < w["p_win"]
            outcome = market.resolved_outcome if (market.resolved and won) \
                else (_other_outcome(market, market.resolved_outcome) if market.resolved
                      else rng.choice(market.outcomes))
            base = market.price_for(outcome) or 0.5
            price = min(0.97, max(0.03, round(base + rng.uniform(-0.03, 0.03), 3)))
            size = round(max(1.0, rng.gauss(w["notional_mu"], w["notional_sigma"])), 2)
            if w["archetype"] != "noise" and rng.random() < 0.015:  # occasional whale
                size = round(rng.uniform(WHALE_SIZE, 20_000), 2)
            pnl = _trade_pnl("buy", price, size, market.resolved_outcome, outcome) \
                if market.resolved else 0.0
            dto = TradeDTO(
                external_id=f"hist-{next(counter)}", wallet_address=w["address"],
                market_id=market.id, outcome=outcome, side="buy", price=price, size=size,
                timestamp=ts, category=market.category,
            )
            dto.realized_pnl = round(pnl, 2)  # type: ignore[attr-defined]
            world.historical_trades.append(dto)
            if market.id not in latest_trade_ts or ts > latest_trade_ts[market.id]:
                latest_trade_ts[market.id] = ts

    # --- resolution timestamps (spread across the timeline) ------------------
    for m in world.markets:
        if not m.resolved:
            continue
        base_ts = latest_trade_ts.get(m.id)
        if base_ts is None:
            m.resolved_at = now - timedelta(days=rng.uniform(1, 200))
        else:
            m.resolved_at = min(now - timedelta(minutes=5), base_ts + timedelta(days=rng.uniform(0.5, 20)))

    # --- recent trades on OPEN markets (drive fresh signals) -----------------
    open_markets = [m for m in world.markets if not m.resolved]
    # bias recent activity toward skilled wallets + a few insiders
    skilled = [w for w in world.wallets if w["archetype"] in
               ("elite", "good", "insider")]
    for _ in range(N_RECENT_TRADES):
        w = rng.choice(skilled if rng.random() < 0.7 else world.wallets)
        market = rng.choice(open_markets or world.markets)
        outcome = rng.choice(market.outcomes)
        price = market.price_for(outcome) or 0.5
        if rng.random() < 0.06:
            size = round(rng.uniform(WHALE_SIZE, 18_000), 2)
        else:
            size = round(abs(rng.gauss(260, 160)) + 40, 2)
        world.recent_trades.append(
            TradeDTO(
                external_id=f"recent-{next(counter)}", wallet_address=w["address"],
                market_id=market.id, outcome=outcome, side="buy",
                price=round(price, 3), size=size,
                timestamp=now - timedelta(minutes=rng.uniform(1, 90)),
                category=market.category,
            )
        )

    return world


class MockProvider:
    """Implements the `DataProvider` protocol on top of the generated world."""

    # Random base so live-trade ids stay unique across worker restarts.
    _live_counter = itertools.count(random.Random().randint(10**6, 10**9))

    def __init__(self, seed: int = WORLD_SEED) -> None:
        self._world = build_world(seed)
        self._jitter = random.Random()

    @property
    def world(self) -> _World:
        return self._world

    def get_markets(self, limit: int = 100) -> list[MarketDTO]:
        out: list[MarketDTO] = []
        for m in self._world.markets[:limit]:
            if m.resolved:
                out.append(m)
                continue
            p = min(0.98, max(0.02, m.prices[0] + self._jitter.uniform(-0.02, 0.02)))
            out.append(
                MarketDTO(
                    id=m.id, question=m.question, slug=m.slug, category=m.category,
                    outcomes=list(m.outcomes), prices=[round(p, 3), round(1 - p, 3)],
                    liquidity=m.liquidity, volume=m.volume, resolved=False,
                )
            )
        return out

    def get_recent_trades(self, limit: int = 50) -> list[TradeDTO]:
        open_markets = [m for m in self._world.markets if not m.resolved]
        skilled = [w for w in self._world.wallets if w["archetype"] in
                   ("elite", "good", "insider")]
        now = datetime.now(timezone.utc)
        n = min(limit, self._jitter.randint(6, 14))
        out: list[TradeDTO] = []
        for _ in range(n):
            w = self._jitter.choice(skilled if self._jitter.random() < 0.75 else self._world.wallets)
            market = self._jitter.choice(open_markets or self._world.markets)
            outcome = self._jitter.choice(market.outcomes)
            price = market.price_for(outcome) or 0.5
            if self._jitter.random() < 0.06:
                size = round(self._jitter.uniform(WHALE_SIZE, 16_000), 2)
            else:
                size = round(abs(self._jitter.gauss(240, 150)) + 40, 2)
            out.append(
                TradeDTO(
                    external_id=f"live-{next(MockProvider._live_counter)}",
                    wallet_address=w["address"], market_id=market.id, outcome=outcome,
                    side="buy",
                    price=round(min(0.98, max(0.02, price + self._jitter.uniform(-0.01, 0.01))), 3),
                    size=size, timestamp=now - timedelta(minutes=self._jitter.uniform(0, 10)),
                    category=market.category,
                )
            )
        return out

    def get_prices(self, market_ids: list[str]) -> dict[str, list[float]]:
        wanted = set(market_ids)
        return {m.id: m.prices for m in self.get_markets(limit=N_MARKETS) if m.id in wanted}
