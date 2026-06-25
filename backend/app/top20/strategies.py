"""
Strategy definitions (Phase 2) as compositions of modular components
(Phase 10): a WalletSelector + SignalFilter + MarketFilter + SizingPolicy +
ExitPolicy. Each strategy is a genuinely different *philosophy*, not just a
parameter tweak, and carries a human-readable description.

`decide()` is the pure entry-logic: given a per-signal context and shared
rankings, it returns (admit, skip_reason). Sizing/exit are separate components.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .sizing import SizingPolicy

# ---------------------------------------------------------------------------
# Category inference — live Gamma `category` is often null, so fall back to
# keyword matching on the market question. Transparent + cheap.
# ---------------------------------------------------------------------------
_CATEGORY_KEYWORDS = {
    "Politics": r"\b(election|president|senate|congress|governor|mayor|vote|poll|democrat|republican|parliament|prime minister|referendum)\b",
    "Sports": r"\b(win|beat|vs\.?|game|match|championship|cup|league|playoff|nba|nfl|mlb|nhl|fifa|score|tournament|final)\b",
    "Crypto": r"\b(bitcoin|btc|ethereum|eth|crypto|solana|sol|token|coin|defi|nft|halving)\b",
    "Entertainment": r"\b(movie|film|oscar|grammy|album|box office|gross|celebrity|tv|series|awards|song)\b",
}


def categorize(question: str | None, raw_category: str | None) -> str:
    if raw_category:
        return str(raw_category)
    q = (question or "").lower()
    for cat, pat in _CATEGORY_KEYWORDS.items():
        if re.search(pat, q):
            return cat
    return "Other"


# ---------------------------------------------------------------------------
# Per-signal context the engine builds once per signal.
# ---------------------------------------------------------------------------
@dataclass
class Ctx:
    wallet_id: int
    classification: str | None
    confidence: float
    edge: float
    liquidity: float
    age_min: float
    price: float
    outcome: str
    market_id: str
    category: str
    win_rate: float
    sharpe: float          # wallet Sharpe proxy
    roi: float
    copyability: float
    specialization: float
    recency: float
    num_settled: int


@dataclass
class Shared:
    """Cohort-level data shared across all strategy decisions for one batch."""
    rank_copyability: dict      # wallet_id -> rank (0 best)
    rank_sharpe: dict
    rank_roi: dict
    rank_active: dict
    edge_pct_threshold: float   # edge at the 90th percentile of this batch
    consensus: set              # (market_id, outcome) with >=2 distinct sharp wallets


@dataclass
class WalletSelector:
    kind: str = "any"           # any|top|classification|sharpe|roi|active|copyability|specialist|newest
    n: int = 10
    classes: tuple = ()

    def admit(self, ctx: Ctx, sh: Shared) -> tuple[bool, str | None]:
        k = self.kind
        if k == "any":
            return True, None
        if k == "top" or k == "copyability":
            r = sh.rank_copyability.get(ctx.wallet_id)
            return (r is not None and r < self.n), f"wallet not in top {self.n} by copyability"
        if k == "classification":
            return (ctx.classification in self.classes), f"wallet not {'/'.join(self.classes)}"
        if k == "sharpe":
            r = sh.rank_sharpe.get(ctx.wallet_id)
            return (r is not None and r < self.n), f"wallet not in top {self.n} by Sharpe"
        if k == "roi":
            r = sh.rank_roi.get(ctx.wallet_id)
            return (r is not None and r < self.n), f"wallet not in top {self.n} by ROI"
        if k == "active":
            r = sh.rank_active.get(ctx.wallet_id)
            return (r is not None and r < self.n), f"wallet not in top {self.n} by activity"
        if k == "specialist":
            return (ctx.specialization >= 0.15), "wallet has no strong category edge"
        if k == "newest":
            return (ctx.recency >= 0.6 and ctx.roi > 0), "wallet not a recently-active profitable wallet"
        return True, None


@dataclass
class SignalFilter:
    kind: str = "any"           # any|confidence|edge|edge_pct|contrarian|momentum|consensus
    threshold: float = 0.0

    def admit(self, ctx: Ctx, sh: Shared) -> tuple[bool, str | None]:
        k = self.kind
        if k == "any":
            return True, None
        if k == "confidence":
            return (ctx.confidence >= self.threshold), f"confidence {ctx.confidence:.0f} < {self.threshold:.0f}"
        if k == "edge":
            return (ctx.edge >= self.threshold), f"edge {ctx.edge*100:.1f}% < {self.threshold*100:.1f}%"
        if k == "edge_pct":
            return (ctx.edge >= sh.edge_pct_threshold), "edge below the top-decile threshold"
        if k == "contrarian":
            return (ctx.price <= 0.45), "not a contrarian (underdog) entry"
        if k == "momentum":
            return (ctx.price >= 0.60), "not a momentum (favorite) entry"
        if k == "consensus":
            return ((ctx.market_id, ctx.outcome) in sh.consensus), "no multi-wallet consensus"
        return True, None


@dataclass
class MarketFilter:
    kind: str = "any"           # any|category|liquidity_min
    value: object = None

    def admit(self, ctx: Ctx, sh: Shared) -> tuple[bool, str | None]:
        if self.kind == "any":
            return True, None
        if self.kind == "category":
            return (ctx.category == self.value), f"market category {ctx.category} != {self.value}"
        if self.kind == "liquidity_min":
            return (ctx.liquidity >= float(self.value)), f"liquidity ${ctx.liquidity:.0f} < ${float(self.value):.0f}"
        return True, None


@dataclass
class StrategyDef:
    key: str
    name: str
    description: str
    philosophy: str
    wallet: WalletSelector = field(default_factory=WalletSelector)
    signal: SignalFilter = field(default_factory=SignalFilter)
    market: MarketFilter = field(default_factory=MarketFilter)
    sizing: SizingPolicy = field(default_factory=SizingPolicy)
    exit_policy: str = "hold"

    def to_params(self) -> dict:
        return {
            "wallet": {"kind": self.wallet.kind, "n": self.wallet.n, "classes": list(self.wallet.classes)},
            "signal": {"kind": self.signal.kind, "threshold": self.signal.threshold},
            "market": {"kind": self.market.kind, "value": self.market.value},
            "sizing": {"mode": self.sizing.mode, "kelly_multiplier": self.sizing.kelly_multiplier,
                       "adjust": self.sizing.adjust, "fixed_dollar": self.sizing.fixed_dollar,
                       "fixed_pct": self.sizing.fixed_pct},
            "exit_policy": self.exit_policy,
        }


def decide(s: StrategyDef, ctx: Ctx, sh: Shared) -> tuple[bool, str | None]:
    """Pure entry decision. Returns (admit, skip_reason)."""
    for comp in (s.wallet, s.signal, s.market):
        ok, why = comp.admit(ctx, sh)
        if not ok:
            return False, why
    return True, None


# ---------------------------------------------------------------------------
# The 20 strategies — diverse philosophies across wallet/signal/market/sizing/exit.
# ---------------------------------------------------------------------------
def _k(mult, adjust=None):
    return SizingPolicy(mode="kelly", kelly_multiplier=mult, adjust=adjust)


STRATEGIES: list[StrategyDef] = [
    # --- wallet-selection philosophies ---
    StrategyDef("top5", "Top 5 Wallets", "Only copy the 5 highest-copyability wallets; quarter-Kelly, hold to resolution.",
                "wallet", wallet=WalletSelector("top", 5), sizing=_k(0.25)),
    StrategyDef("top10", "Top 10 Wallets", "Copy the 10 highest-copyability wallets; quarter-Kelly.",
                "wallet", wallet=WalletSelector("top", 10), sizing=_k(0.25)),
    StrategyDef("top20", "Top 20 Wallets", "Copy the 20 highest-copyability wallets; quarter-Kelly.",
                "wallet", wallet=WalletSelector("top", 20), sizing=_k(0.25)),
    StrategyDef("highest_sharpe", "Highest-Sharpe Wallets",
                "Copy the 10 wallets with the best risk-adjusted (Sharpe) history; half-Kelly with TP/SL.",
                "wallet", wallet=WalletSelector("sharpe", 10), sizing=_k(0.50), exit_policy="tp_sl"),
    StrategyDef("highest_roi", "Highest-ROI Wallets",
                "Copy the 10 most profitable wallets by realized ROI; confidence-adjusted Kelly.",
                "wallet", wallet=WalletSelector("roi", 10), sizing=_k(0.25, "confidence")),
    StrategyDef("most_active", "Most-Active Wallets",
                "Copy the 10 most active wallets; fixed 2% of bankroll per trade.",
                "wallet", wallet=WalletSelector("active", 10),
                sizing=SizingPolicy(mode="fixed_pct", fixed_pct=0.02)),
    StrategyDef("highest_copyability", "Highest Copyability",
                "Copy the 8 wallets with the top copyability score; wallet-quality-adjusted Kelly.",
                "wallet", wallet=WalletSelector("copyability", 8), sizing=_k(0.25, "quality")),
    StrategyDef("specialists", "Category Specialists",
                "Only copy wallets with a strong edge in a specific category; edge-adjusted Kelly.",
                "wallet", wallet=WalletSelector("specialist"), sizing=_k(0.25, "edge")),
    StrategyDef("newest_profitable", "Newest Profitable Wallets",
                "Copy recently-active, profitable wallets; fixed $100, time-stop exit.",
                "wallet", wallet=WalletSelector("newest"),
                sizing=SizingPolicy(mode="fixed_dollar", fixed_dollar=100), exit_policy="time_stop"),
    StrategyDef("consensus_wallets", "Consensus Wallets",
                "Enter only when 2+ sharp wallets agree on the same market/outcome; mirror exits.",
                "wallet", signal=SignalFilter("consensus"), sizing=_k(0.25), exit_policy="mirror"),
    # --- signal-selection philosophies ---
    StrategyDef("conf80", "Confidence ≥ 80", "Take only high-conviction signals (confidence ≥ 80).",
                "signal", signal=SignalFilter("confidence", 80), sizing=_k(0.25)),
    StrategyDef("conf90", "Confidence ≥ 90", "Take only the strongest signals (confidence ≥ 90).",
                "signal", signal=SignalFilter("confidence", 90), sizing=_k(0.25)),
    StrategyDef("highest_edge", "Top-Decile Edge",
                "Take only signals in the top 10% of observed edge; half-Kelly.",
                "signal", signal=SignalFilter("edge_pct"), sizing=_k(0.50)),
    StrategyDef("edge5", "Edge ≥ 5%", "Take only signals with ≥ 5% estimated edge.",
                "signal", signal=SignalFilter("edge", 0.05), sizing=_k(0.25)),
    StrategyDef("contrarian", "Contrarian",
                "Bet underdogs (price ≤ 0.45) that sharp wallets back; volatility-adjusted Kelly, TP/SL.",
                "signal", signal=SignalFilter("contrarian"), sizing=_k(0.25, "volatility"), exit_policy="tp_sl"),
    StrategyDef("momentum", "Momentum",
                "Back favorites (price ≥ 0.60) that sharp wallets pile into; time-stop exit.",
                "signal", signal=SignalFilter("momentum"), sizing=_k(0.25), exit_policy="time_stop"),
    # --- market-filter philosophies ---
    StrategyDef("politics", "Politics Only", "Only trade political markets.",
                "market", market=MarketFilter("category", "Politics"), sizing=_k(0.25)),
    StrategyDef("sports", "Sports Only", "Only trade sports markets.",
                "market", market=MarketFilter("category", "Sports"), sizing=_k(0.25)),
    StrategyDef("crypto", "Crypto Only", "Only trade crypto markets.",
                "market", market=MarketFilter("category", "Crypto"), sizing=_k(0.25)),
    StrategyDef("high_liquidity", "High Liquidity",
                "Only trade deep markets (liquidity ≥ $10k); half-Kelly.",
                "market", market=MarketFilter("liquidity_min", 10_000), sizing=_k(0.50)),
]

CONFIG_BY_KEY: dict[str, StrategyDef] = {s.key: s for s in STRATEGIES}
assert len(STRATEGIES) == 20, "TOP 20 must define exactly 20 strategies"
