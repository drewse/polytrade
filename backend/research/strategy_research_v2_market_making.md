# Strategy Research v2 — Reverse-Engineering the Winners (Market-Making Thesis)

**This supersedes the "no edge" conclusion of v1.** That conclusion was wrong in an
important way: it tested whether a *directional, single-side* strategy makes money. The
top wallets are not directional and not single-side — they are **two-sided market makers**.
There IS a structural edge here; it is a **liquidity-provision / spread-capture / maker-
rebate** edge, not a prediction edge. Below: the evidence, the reverse-engineered
strategy, the cross-asset picture, and an offline-first validation plan.

---

## 1. Profiles of the top wallets (empirical, pulled from Polymarket data-api)

| Wallet | All-time P&L | What the trade data shows |
|---|---|---|
| **w1 `0x04b6…94c8`** ("Peaceful-Quadrant") | **+$271,852** | **614 trades/day**; **100% BUY** (never sells); buys **both Up & Down in 65% of markets**; prices span **0.01–0.98 (mean 0.467)**; **BTC 5m (1,442) + 15m (590)**; quotes mostly in the **first 3 min** of the 300s window (median 112s); holds to resolution / redeems. Matched-pair fill sum median **1.004** (≈ breakeven on the matched book) ⇒ its profit leans on **rebates + scale + selection**, not pure spread. |
| **w2 `0x568b…00b3b`** | **+$36,757** | **Pure BTC-5m specialist** (100% of flow); **100% BUY**, two-sided; **37 MERGE + 33 REDEEM** events; **ZERO taker trades** (pure maker — never crosses); tiny **$4 median** size, very high frequency. The cleanest template: **buy Up+Down below $1 → MERGE into $1 = locked spread**, redeem the rest. |
| **std0 `0xf3a6…8c6c`** (prior research) | +$3,818 | BTC-5m specialist (86% of flow), **252 trades/day**, buy_pct 85%, avg entry 0.41, holds to resolution. |

*(The third name you gave, `ohioriskmanagement`, would not resolve username→address via
the public APIs; w1/w2/std0 plus the structural research below are sufficient to
triangulate the strategy.)*

**The shared signature:** never sell · buy both sides · all price levels · hold/merge/
redeem · very high frequency · small size · concentrate in the highest-volume crypto 5m
markets · quote early in the window. **That is the fingerprint of a binary market maker,
not a bettor.**

## 2. Common characteristics
1. **Two-sided.** They quote/buy *both* Up and Down — they are providing liquidity, not
   taking a view. Outcome split ≈ 50/50 (w1: Up 1211 / Down 1289).
2. **100% buy, never sell.** They exit via **redemption** (winning side pays $1) or
   **merge** (Up+Down → $1), never by selling back into the book. No exit-execution risk.
3. **Maker-centric.** w2 has *zero* taker trades. This is decisive post-March-2026:
   makers pay **0% fees and earn a 20% rebate** of the crypto taker fee; takers pay up to
   **~1.8–3% near $0.50**. A taker doing this would bleed fees; a maker gets paid.
4. **High frequency, small size** ($4–$56 median) across hundreds of markets/day.
5. **Quote early in the window** (first ~3 min), when both legs can still fill and before
   the outcome-revealing last minute (adverse).
6. **Concentrate in the deepest market** (BTC 5m/15m), where matched-fill volume and
   rebate/reward flow are largest.

## 3. What differentiates winners from losers
- **Structural (the big one):** **maker vs taker.** Makers pay 0 + earn 20% rebate;
  takers pay ~1.8–3% near 0.50. On near-50/50 markets that fee is larger than any plausible
  directional edge — so **takers are structurally short the house, makers are structurally
  long it.** Losers cross the spread; winners rest.
- **Two-sided vs one-sided:** Polymarket's liquidity-reward score pays balanced two-sided
  quoting **~3× single-sided** (scaling constant c=3), and in tail markets single-sided
  scores **zero**. Winners quote both sides; losers pick a side.
- **Hold/merge/redeem vs scalp-exit:** winners never pay the spread to exit; they let
  settlement/merge realise value. Losers round-trip and pay twice.
- **From our own wallet-scoring code:** profitable wallets are distinguished by
  **profit_factor > 1.2, expectancy > 0, consistency over time** — *not* win rate. Our
  copy-trading ranking already de-weights win rate (5%) and weights ROI/PF/Sharpe — which
  is exactly the market-maker signature (many tiny wins, controlled losses).
- **On-chain reality check:** independent studies find **~0.55% of profitable maker
  wallets captured ~50% of all maker profit**, explicitly "market-making algos / HFT /
  arb," and **bots are 55–62% of 5m volume**. The winners are automated MMs.

## 4. Hypotheses for their actual strategy (ranked by likelihood)

**H1 — Two-sided spread-capture MM + maker rebates (MOST LIKELY).**
Rest BUY limit orders on *both* Up and Down a tick or two below their mids. When both fill,
`cost = bid_up + bid_down < 1` → **merge into $1 → bank (1 − cost)** risk-free. When only
one fills, hold to resolution. Revenue = spread capture on matched pairs **+ 20% maker
rebate + LP rewards**, minus adverse loss on unmatched legs.
- *Evidence:* w2's 37 merges + pure-maker + two-sided; w1 two-sided; the rebate/fee
  structure; profit concentration in MM algos. *Timing:* early window. *Inventory:* merge
  matched, hold unmatched. *Placement:* near mid, both sides. *Selection:* deepest market.

**H2 — Reward/rebate-primary farming (LIKELY, blends with H1).**
Quote two-sided near mid mainly to harvest **maker rebates + liquidity rewards**, accepting
~breakeven *trading* P&L. *Evidence:* w1's matched book is ≈breakeven (sum 1.004) yet it
made $271k — consistent with the edge being the rebate/reward layer, not the trade.

**H3 — Latency-advantaged adverse-selection avoidance (LIKELY, enabler).**
Use Polymarket's documented **250–500 ms maker latency advantage** to cancel/re-price stale
quotes before informed takers pick them off — capturing spread while dodging toxic flow.
*Evidence:* the documented maker delay; bot dominance; it's the only way two-sided making
survives adverse selection.

**H4 — Selection/inventory skill on unmatched legs (POSSIBLE, secondary).**
Some of w1's edge may be holding the *right* unmatched side (micro-timing). Less likely the
core — our directional research says the market is efficient on direction.

**H5 — Directional prediction (REJECTED).** The winners' 50/50 Up/Down split, all-price-
level buying, and our exhaustive directional null results rule this out.

**Most probable reality: the winners run H1 + H2 + H3 together** — a latency-managed,
two-sided, never-cross maker that captures spread on merges, earns rebates/rewards, and
holds unmatched inventory to resolution.

## 5. The single strategy that best explains their profitability

**"Delta-neutral two-sided 5m maker."** Concretely:
1. **Quote both Up and Down** with resting BUY limits at/just-below mid; **never cross**
   (pure maker → 0 fees + 20% rebate + reward eligibility).
2. **On matched fills → MERGE** Up+Down → $1, banking `1 − (bid_up+bid_down)` instantly.
3. **On unmatched fills → hold to resolution** (redeem), or re-balance by adjusting the
   other-side quote toward neutral.
4. **Quote in the first ~3 minutes**; pull quotes in the final ~60–90 s (outcome-revealing,
   adverse).
5. **Inventory management:** keep Up/Down exposure roughly balanced; cancel stale quotes
   fast to avoid adverse fills.
6. **Market selection by competition-adjusted spread** (see §6) — not every market equally.

This is structurally different from everything we've tried (join-best-bid was single-side,
directional, fee-paying, reward-blind). It explains *why* the winners win without any
directional edge.

## 6. Cross-asset comparison (BTC vs ETH vs SOL vs XRP)

All four run identical `<asset>-updown-5m-<ts>` markets (also 15m), tick 1¢, min 5 shares.
Multi-window microstructure:

| Asset | Spread | Top-of-book depth | Volume/5min | Maker read |
|---|---|---|---|---|
| **BTC** | 1¢ always | **deepest (~3,000 sh/side)** | **~$26k** (~$60M/day) | Deepest/safest but **most bot-competitive**; the 1¢ is permanently defended ⇒ edge is **queue position**, which bots win. |
| **ETH** | 1¢ | thin top (~40–180 sh) | ~$1.4k | **Best balance for a small maker** — tight structure (low adverse ambiguity) but **uncrowded book**; the 1¢ is *not* deeply defended ⇒ room to provide and earn spread+rebate with far less queue competition. |
| **SOL** | **2¢ avg (often 3¢)** | thin (~200–500 depth) | ~$780 | **More spread to capture per matched pair**, but sparse flow (~41 trades) ⇒ higher per-fill edge, lower fill rate, more inventory risk. |
| **XRP** | 1.5¢ avg (occ. 3¢) | thinnest | ~$680 | Highest theoretical capture, **thinnest/riskiest**, least-contested. |

**Do the behaviors transfer?** Mechanically yes — identical structure, so the two-sided
maker works on all four. The *winners* concentrate in BTC (most volume) but that's also the
most saturated queue.

**Which has the strongest opportunity for *us* (small, slow):** **not BTC.** In BTC we'd
sit behind HFT bots in a deep 1¢ queue and rarely earn priority. The **competition-adjusted**
opportunity is best in **ETH** (tight spread, uncrowded book, still enough flow to fill) and
secondarily **SOL/XRP** (wider spread to capture, least competition, at the cost of fill
frequency and adverse risk). **Hypothesis: the cleanest inefficiency for a small maker is in
ETH/SOL/XRP, not BTC** — the opposite of where the pros cluster, precisely because they
cluster in BTC.

## 7. The honest risk (why this might still not work for us)
The winners are **HFT bots with infrastructure and the maker latency advantage**. The
**spread-capture/merge** component (H1) is comparatively latency-tolerant — it depends on
*both* legs filling below $1, not on winning a microsecond queue race. The **reward-farming**
component (H2) is a queue-position race the bots win. So our realistic shot is the
**merge/spread + rebate** layer in **less-contested assets**, not out-racing bots for BTC
reward share. If even that is dominated, the conclusion changes — but we haven't tested it,
and the structural maker advantages (0 fee, 20% rebate, 3× two-sided reward, merge arb) are
real and were entirely absent from our prior single-side experiments.

## 8. Validation plan — offline first, minimum live risk (no live trades without approval)

**Phase 0a — Two-sided maker simulator (offline, $0).** Extend the existing passive-maker
lab to quote **both legs** and simulate from the historical trade stream, per asset
(BTC/ETH/SOL/XRP). Measure: P(both fill), P(one fill), captured spread on merges, adverse
loss on unmatched legs; then add the **documented economics** (0 maker fee, +20% crypto
rebate, LP reward where present). Output: **net EV per asset** with market-level confidence.
Gate: net EV > 0 after rebate/reward and adverse selection, in ≥2 assets, OOS.

**Phase 0b — Winner/loser differentiator test (offline, $0).** Pull 50–100 more wallets
(co-traders of w1/w2/std0 + the public reward/leaderboard dashboards), and statistically
confirm that **two-sided + maker + never-sell + merge/redeem** separates winners from
losers (effect size, not anecdote). This validates the thesis itself, cheaply.

**Phase 1 — Tiny live confirmation (only if Phase 0 clears).** Run the existing live-maker
executor **rebuilt for two-sided quoting + merge handling + rebate tracking**, smallest size,
ONE asset (the Phase-0 winner, likely ETH), within the $100 cap. Measure realised spread
capture + rebates + adverse vs the model. This tests the *real* edge with far less capital
than collecting single-side fills.

**Build required before Phase 1:** the live executor currently quotes one side. It needs:
two-sided quoting, the **merge** operation, inventory balancing, and rebate accounting.
That's a real build — but only after Phase 0 says the EV is there.

---

### Bottom line
The winners prove there's an edge; it's **market-making, not prediction**. They quote both
sides, never cross (0 fee + 20% rebate), merge matched inventory for locked spread, hold the
rest to resolution, and do it at high frequency in the deepest markets. Our prior strategies
missed it because they were single-side, directional, and fee/reward-blind. The
highest-EV next step is to **simulate the two-sided maker offline across BTC/ETH/SOL/XRP**
and find the asset where a small, slow maker's competition-adjusted EV is positive — with my
prior that it's **ETH or another alt, not BTC.** No live capital until that offline test
clears.
