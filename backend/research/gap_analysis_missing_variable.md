# Gap Analysis — The Smallest Missing Variable (Behavioral Reverse-Engineering)

**Question:** what is the smallest missing variable that reconciles our emulator with the observed
profitability of the top wallets — and is "it's HFT" actually uniquely supported?

**Answer:** The smallest missing variable is **queue position** (front-of-queue vs our worst-queue
assumption). It is worth **~+0.04 per fill** and closes **~90%** of the discrepancy — turning our
strongly-negative emulator into a ~breakeven per-fill maker. We CAN predict the wallets' behavior
(so we did reverse-engineer the strategy — it is a textbook continuous two-sided touch-maker; this
is Case 1, a missing execution variable, not Case 2, a wrong interpretation). But queue position
alone only reaches **breakeven**; the residual step to their actual profit is **rebates + reward
optimization + thin-edge × volume**, and holding front-of-queue priority at that volume through a
fast book is what genuinely requires speed. So "speed" survives — but as a *specific mechanism
(queue priority → benign-fill capture → thin edge scaled by volume)*, not a hand-wave, and only
after ruling the observable alternatives in or out below.

---

## 1. Can we predict their behavior? (If not, we haven't reverse-engineered it.)
Measured on w1 (`0x04b6`, +$272k) and w2 (`0x568b`, +$37k), from their real fills + on-chain activity:

| Decision | Predictability | Evidence |
|---|---|---|
| **Enter this market?** | **~100%** | Both trade **100% of consecutive 5-minute windows** — they never skip; they are always in. |
| **Which side(s)?** | **~100% (both)** | **100% two-sided**, ~50/50 Up/Down (w2 1675/1825, w1 1692/1637). |
| **Quote price?** | **High** | Fills track the moving touch across the full 0.1–0.8 range — they *join the touch* and re-quote as the mid moves. |
| **Quote timing?** | **High** | Continuous from early window (p10 26–35s, p50 106–137s of 300s). A continuous-quoting process, not a point in time. |
| **Inventory path?** | **High** | Two-sided + balanced ⇒ inventory hovers near-neutral (mean-reverting). |
| **Merge timing?** | **Moderate** | w2 merges mid-to-late window (p50 ~mid); w1 doesn't merge at all (holds → redeem). |
| **Quote SIZE?** | **LOW** | 2–111 shares, highly variable — the one dimension we cannot predict from observable state. |
| **Cancel timing?** | **Unobservable** | The public feed shows fills, not a wallet's un-filled quotes/cancels — we cannot see this per wallet at all. |

**Conclusion:** the *strategy* is highly predictable and we have reverse-engineered it — always enter,
quote both sides at the touch continuously, hold/merge/redeem, stay near-neutral. The residual
un-predictable dimensions are **size** (observable but variable) and **cancellation** (not observable
per wallet). So the profit gap is **not** a misunderstood strategy; it is a **missing execution
variable** — which the analysis pins to queue position.

## 2. The "why" questions, answered from data
- **Why enter this market / at this time / this price?** Because the strategy is *quote every window,
  continuously, at the current touch.* There is no selective "why here" — they enter **all** of them,
  early and throughout. Market selection is **not** a filter (contradicts an earlier hypothesis).
- **Why both sides here but not there?** They quote both sides **everywhere** (100% two-sided). The
  "not there" cases in our earlier data were artifacts of only seeing *taker* fills.
- **Why this size?** Variable (2–111 sh) — the genuine unknown. Plausibly scales with book depth /
  reward-eligible size / inventory rebalancing (see §4).
- **Why merge now vs later / why stop quoting / why skip next?** w2 merges to recycle capital
  mid/late; they don't meaningfully "stop" or "skip" (100% window coverage). w1 doesn't merge (redeems).
- **Why is inventory / capital distributed this way?** Balanced two-sided ⇒ near-neutral inventory;
  capital scales with the (variable) size. This is inventory-neutral market-making, not directional.

## 3. The smallest missing variable — quantified
Direction-neutral per-fill markout (mid-move + captured spread; thousands of fills, 5s horizon):

| | FRONT-of-queue (all touch flow) | WORST-queue (through-trades only) |
|---|---|---|
| BTC | −0.001/fill | **−0.042/fill** |
| ETH | +0.001 | −0.068 |
| SOL | −0.001 | −0.042 |
| XRP | +0.006 | −0.032 |

Our emulator sampled **only through-trades** (the adverse tail) → −0.04/fill. A front-of-queue maker
samples **all touch flow** (mostly benign) → **≈ 0/fill**. **Queue position is the dominant missing
variable**, worth ~+0.04/fill (~90% of the negative gap). It gets the strategy to **breakeven**, not
yet to profit — consistent with the wallets earning a *thin* per-fill edge scaled by huge volume.

## 4. Gap analysis — every explanation, ranked, with estimated contribution
Ranked by how much of the emulator-vs-reality profit gap each plausibly explains:

| # | Explanation | Est. share of gap | Evidence / reasoning | Accessible to small acct? |
|---|---|---|---|---|
| 1 | **Queue position (front vs back)** | **~70–90%** | Direct: −0.04→0 per fill (§3). Our worst-queue assumption was the core error. | Partly — *entering* the queue is free (post early); *holding* priority through a moving book needs speed. |
| 2 | **Thin edge × VOLUME (priority-enabled)** | **~10–20%** | Front-of-queue is ~breakeven/fill; profit = tiny edge × 100–175 fills/mkt × all windows. Requires holding priority = speed. | No — volume needs sustained priority. |
| 3 | **Maker rebates + LP reward optimization** | **~5–15%** | Rebate +~0.0015/fill (measured); LP-reward scoring rewards exactly their behavior (two-sided, near-mid, high uptime). Reward pools on candles unconfirmed → **honest unknown; could be larger than modeled.** | Rebate yes; reward-scoring race favors big/fast makers. |
| 4 | **Variable sizing / inventory optimization** | **~0–10%** | Size varies 2–111 sh — we can't explain it; may hide a sizing/rebalancing edge. **Genuine residual unknown.** | Unclear. |
| 5 | **Fast merge (recycle before adverse)** | **~0–5%** | Front-of-queue edge ~0 at 5s but negative by 30s ⇒ realizing/merging fast matters. w2 merges. | Speed-related. |
| 6 | **Cross-market / spot information** | **~0%** | Tested directly: spot-lead AUC ≈ 0.47–0.54 (no power). **Ruled out.** | N/A |
| 7 | **Order-book features (imbalance/depletion) as alpha** | **~0%** | Tested: imbalance AUC 0.48, depletion ~0.55 (marginal), no EV lift. **Ruled out.** | N/A |
| 8 | **Directional skill / mispricing** | **~0%** | Markets efficient on direction; wallets are ~50/50 two-sided, not directional. **Ruled out.** | N/A |
| 9 | **Internal exchange mechanics / hidden order types** | **unknown, small** | Can't observe; no evidence for it. | N/A |

**What is ruled OUT by evidence (not assumption):** cross-market info, order-book-imbalance alpha,
directional/mispricing edge. These were the most likely "hidden signal" candidates and they carry ~0.
There is **no secret predictive signal** — even granting front-of-queue, the per-fill edge is ~0.

**What remains:** queue position (dominant) + volume + rebates/rewards, with variable-sizing and
LP-reward-optimization as the honest residual unknowns (#3, #4) that I *cannot* fully quantify from
observable data.

## 5. Does the evidence uniquely support "speed"? — Honest verdict
**Mostly yes, but with two caveats I will not paper over.**
- The dominant variable (queue position) and the volume that monetizes the thin edge both require
  **holding front-of-queue priority through a fast-moving book** — that is a speed/infrastructure
  capability. And critically, we proved there is **no hidden alpha signal** (every observable signal
  tested at AUC ≈ 0.5), so the winners are **not** out-predicting the market — they are out-*executing*
  it. That uniquely points to execution/priority (speed), not information.
- **Two things I cannot rule out with the data**, and which are not "speed":
  1. **Liquidity-reward optimization (#3):** their behavior is a textbook reward-farming profile; if
     the candle markets carry LP pools I couldn't confirm, rewards — not trading edge — could be a
     material profit source. This is a *structural incentive*, capturable in principle without elite
     speed (though the reward-scoring competition favors size/uptime).
  2. **Variable sizing (#4):** an unexplained degree of freedom that could encode a real edge.

**So the precise, evidence-based conclusion:** it is **not** "they have a signal we lack" (ruled out)
and **not** "we misread the strategy" (we predict it well). It is **"they hold a structural execution
position (front-of-queue priority) that converts a near-zero per-fill spread into profit via volume +
rebates/rewards, and sustaining that position through these fast books requires speed a small account
can't match."** The one honest door left open is **reward optimization**, which is a *different* lever
than speed and the only remaining thing I would probe before final closure.

## 6. Recommendation
We have now exhausted the **observable** explanations: the missing variable is identified (queue
position), the strategy is reverse-engineered (predictable), and every hidden-*signal* hypothesis is
tested and ruled out. The remaining edge is execution-priority (speed-gated) plus a rebate/reward
component. Unless you want the **one** remaining non-speed probe — confirm whether these specific 5m
candle markets carry Liquidity-Reward pools and size them (a read-only API/docs check, no trading) —
this is the honest end of the observable analysis, and the earlier close-out recommendation stands.
No live trades were placed. Awaiting your call on the reward-pool check vs. formal closure.
