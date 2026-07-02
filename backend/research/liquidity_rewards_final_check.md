# Liquidity-Reward Final Check — 5-Minute Crypto Candle Markets

**Verdict: the BTC/ETH/SOL/XRP 5-minute candle markets carry NO active liquidity-reward pool.**
The reward *eligibility scaffolding* exists (min order size 50 shares, max spread 4.5¢ from mid) but
the actual reward **rate is `null`** and `clobRewards` is `null` on every asset — no pool is funded,
so **$0/day in liquidity rewards** flows to makers on these markets. This closes the last non-speed
door. Read-only API check; no live trades, no executor change.

---

## Evidence (direct, market-specific, verified both ways)
Live 5m market reward config — identical across BTC/ETH/SOL/XRP:
```
Gamma:  rewardsMinSize=50, rewardsMaxSpread=4.5, clobRewards=null, holdingRewardsEnabled=false
CLOB:   rewards={"rates": null, "min_size": 50, "max_spread": 4.5}
```
`rates: null` / `clobRewards: null` = **no active reward program.** Confirmed by the contrast: of 100
top-volume open markets, **36 DO carry non-empty `clobRewards` with a populated `rewardsDailyRate`**
(e.g., World Cup markets), and the CLOB `/rewards/markets/current` endpoint returns those funded
markets with a `rewards_config`. The candle markets are **not** among them — the field populates when
rewards are active, and here it is null.

## Answers to the eight questions
1. **Active reward program on the candle markets?** **No** — `rates: null`, `clobRewards: null`.
2. **Market- / asset- / category-specific?** Rewards are **per-market** (each market has its own
   `clobRewards`). Funded markets are high-profile events (World Cup, etc.); the fast crypto candles
   specifically carry none — for all four assets equally.
3. **Reward size per market/window/day?** **$0.** No pool.
4. **Eligibility (min size / max spread / duration / two-sided / uptime / volume)?** The *parameters*
   are set (min 50 sh, max 4.5¢ from mid; two-sided/uptime scoring would apply *if* a pool existed),
   but they are moot with a null rate — meeting them earns nothing here.
5. **Do w1/w2 look reward-optimized?** **No.** Their median fill size is **~8–10 shares** — well under
   the 50-share reward minimum — and there are no rewards to optimize for anyway. They are
   spread-trading with small orders, **not** reward-farming.
6. **Could rewards + rebates explain the profit gap?** **Rewards: no (they are $0).** Only the maker
   **rebate** remains — 20% of the crypto taker fee, ≈ **+0.0015/fill** (measured). Small: it tips a
   ~breakeven front-of-queue maker slightly positive at volume, but cannot by itself explain the gap.
7. **Could a small account qualify / does it need scale?** Moot — no rewards to qualify for. (Even if a
   pool existed, the 50-share min and pro-rata scoring favor large, high-uptime, fast makers.)
8. **Do ETH/SOL/XRP have a better reward-to-liquidity ratio than BTC?** **No** — all four carry the
   identical null reward config. There is no reward-based reason to prefer the alts.

## FINAL ANSWER — what are these wallets earning from?
**Primarily execution / queue-position spread capture at volume, plus a small maker-rebate component.
NOT liquidity rewards (unavailable), and NOT a trading/prediction edge (no signal).**

Decomposition, consolidated across the whole research arc:
- **Liquidity rewards: ~0%** — confirmed unavailable on these markets (this check).
- **Trading/prediction edge: ~0%** — every observable signal tested at AUC ≈ 0.5 (Phase 0a-3); markets
  efficient on direction.
- **Maker rebate: small, real** — ~+0.0015/fill; meaningful only at their volume.
- **Queue-position spread capture: dominant** — front-of-queue makers capture the ~half-spread on
  benign touch flow and lock it on matched (delta-neutral, mergeable) pairs; per fill this is
  ~breakeven-to-thin-positive after adverse selection, scaled by 100–175 fills/market × every window.
  Holding that front-of-queue priority through fast books is the speed-gated capability.

So it is a **combination dominated by execution (queue priority) + volume, with a small rebate tail —
not rewards, not signal.**

## Conclusion (meets your stated decision rule)
You said: *"If liquidity rewards are not material or unavailable, I accept the conclusion that the
remaining edge is execution/queue-position/volume and likely inaccessible to us."*

**Liquidity rewards are unavailable** ($0 pool, verified). Therefore the remaining edge is
**execution / queue-position / volume**, plus a small maker rebate — and it is **inaccessible to a
small, non-co-located account**: it requires holding front-of-queue priority at high volume through
fast books, which is a speed/infrastructure capability, not information or capital we can substitute
for. There is no low-capital reward-farming opportunity here because there are no rewards.

**This is the honest, evidence-based end of the market-making investigation for these markets.** Every
observable explanation has now been tested and either quantified or ruled out:
- misunderstood strategy → ruled out (we predict their behavior well),
- hidden signal (order book / cross-market / spot) → ruled out (AUC ≈ 0.5),
- liquidity rewards → ruled out (no pool),
- queue position → identified as the dominant missing variable (~90% of the gap), speed-gated,
- rebate → small, real, insufficient alone.

**Recommendation: formally close the fast-crypto market-making hypothesis and redirect research.** The
edge is real but structural-execution-based and reserved for co-located high-volume operators; there
is no accessible low-capital path (trading, signal, or reward) for a small independent participant.
No live trades were placed at any point.
