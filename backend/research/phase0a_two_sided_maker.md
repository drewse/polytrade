# Phase 0a — Two-Sided Maker Simulator (BTC / ETH / SOL / XRP 5-minute)

**Question:** *Can a non-HFT, small-account two-sided maker earn positive net EV through
spread capture, merge mechanics, and rebates in these fast crypto markets?*

**Answer: NO — not on current evidence, for any of the four assets.** Under realistic
execution (worst-case queue, no fast cancellation) every asset is net-negative. The one
near-breakeven case (BTC) only "breaks even" by almost never filling. **However**, an
idealized perfect-cancellation ceiling flips *all four* strongly positive — which pinpoints
the real edge: it exists, but it lives **entirely in latency-advantaged quote cancellation**
(avoiding adverse fills), a capability a slow account does not have. The spread/merge/rebate
mechanics *by themselves* do not clear the bar for us.

No live trades were placed; no executor change was made. (Scripts: `research/mm_harvest.py`,
`research/mm_simulate.py`.)

---

## Method
- **Dataset:** harvested **~115–119 resolved 5-minute markets per asset** (BTC 113, ETH 116,
  SOL 118, XRP 119 usable) with full trade tapes + on-chain resolution, by crawling prolific
  traders' activity (Gamma drops closed candles, so enumeration required crawling). BTC/ETH
  tapes are near the 500-trade API cap (late-window activity slightly truncated → if anything
  *understates* fills/adverse for those two).
- **Model:** quote a BUY on Up at `mid−h` and a BUY on Down at `mid+h` (= Up ask), re-quoted
  every `L` seconds, 5-share orders. **Maker-only.** Matched Up+Down → merge into $1 (locked);
  unmatched held to resolution. **Adverse selection is not assumed — it emerges from the real
  price path** (an Up bid fills on a downtick, i.e. exactly when Down is becoming likelier to
  win). **Economics:** maker fee 0; rebate = 20% of the crypto taker fee `size·0.07·p·(1−p)`
  on each fill; LP reward modeled at 0 (candles don't appear to carry classic LP pools).
- **Queue realism:** default **worst-case** (fill only when the tape trades *through* your
  quote) — the correct assumption for a slow account sitting behind HFT bots. `best` (front-
  of-queue) and `mid` are optimistic controls. `cancel` = perfect-cancellation ceiling.

## Per-asset results — baseline (h=0.005, L=30s, worst queue, full window)

| Asset | Markets | Quotes | Fill rate | Fills/mkt | Matched-pair rate | Merged pairs | Trading P&L | Rebate | **Net EV / market** | 95% CI | ROI on cost | Drawdown |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **BTC** | 113 | 2,018 | 16% | 2.9 | 0.88 | 680 | −$22.1 | +$2.1 | **−$0.178** | [−0.29, −0.08] | −2.8% | −$20.9 |
| **ETH** | 116 | 2,070 | 72% | 12.8 | 0.98 | 3,180 | −$234.5 | +$19.5 | **−$1.853** | [−2.41, −1.26] | −6.1% | −$215 |
| **SOL** | 118 | 2,084 | 78% | 13.7 | 1.00 | 3,400 | −$331.9 | +$21.3 | **−$2.632** | [−3.17, −2.07] | −8.2% | −$314 |
| **XRP** | 119 | 2,154 | 74% | 13.4 | 1.00 | 3,335 | −$296.4 | +$20.7 | **−$2.317** | [−3.00, −1.62] | −7.3% | −$279 |

**Read:** matched-pair rate is near-total (both sides fill in ~88–100% of markets), and the
merges *do* lock a small spread — but the **unmatched inventory** accumulates on the losing
side in trending windows and its adverse-selection loss **dwarfs** the spread capture and the
rebate. The more you fill (ETH/SOL/XRP at 72–78%), the more you lose. BTC loses least only
because its deep, tight book rarely trades *through* a slow maker (16% fill).

## Sensitivity (net EV/market) — nothing flips it positive

- **Queue mode:** worst ≈ best, mid worse. **Front-of-queue does NOT help** (BTC −0.178 worst
  = −0.178 best; ETH −1.85 vs −1.83). *It is not a queue-position problem.*
- **Quote lifetime L:** longer is less-bad (fewer re-quotes → less inventory): BTC −0.28(15s)
  → −0.09(60s); ETH −2.81 → −0.99. Still negative.
- **Half-spread h:** no value of h (0.005/0.01/0.015) is positive on any asset.
- **Window phase:** "early" best for BTC (−0.002 ≈ breakeven), "late" best for alts (−0.85 to
  −0.98). All still ≤ 0.
- **Inventory cap:** capping net inventory helps (BTC −0.14, ETH −1.38 at cap 5) but stays
  negative.
- **Stacked best case** (early + L60 + cap5): BTC **−$0.001** (CI [−0.023, +0.018] — a true
  breakeven, but only **0.31 fills/market** = barely trading); ETH −0.51, SOL −0.82, XRP −0.97.

## Break-even reward analysis
To reach EV=0, each asset would need an LP reward of:
| Asset | Reward needed / market | Per fill | vs. actual rebate |
|---|---|---|---|
| BTC | +$0.178 | $0.061 | rebate $0.018 (need ~3.4×… but at 2.9 fills) |
| ETH | +$1.85 | $0.145 | rebate $0.168 — reward'd need **~11×** the rebate |
| SOL | +$2.63 | $0.192 | **~15×** |
| XRP | +$2.32 | $0.173 | **~13×** |

The maker rebate is real but **~10–15× too small** to bridge the adverse-selection gap on the
alts, and the candle markets do not appear to carry classic LP pools of that size.

## The decisive test — why the pros win and we can't (yet)
The only capability the profitable wallets (w1 +$272k, w2 +$37k) have that this model lacks is
the **maker latency advantage to cancel quotes before adverse fills land**. I modeled an
idealized **perfect-cancellation ceiling** (keep a fill only if price reverts back across the
quote — i.e. drop the sustained-adverse fills):

| Asset | Net EV/market — **worst queue (us)** | Net EV/market — **perfect-cancel ceiling (pros)** |
|---|---|---|
| BTC | −$0.178 | **+$0.206** (CI [0.13, 0.30]) |
| ETH | −$1.853 | **+$1.185** (CI [0.68, 1.69]) |
| SOL | −$2.632 | **+$1.334** (CI [0.71, 1.96]) |
| XRP | −$2.317 | **+$1.423** (CI [0.83, 2.03]) |

**The sign flips entirely.** The benign, spread-capturing fills (+ rebate) ARE profitable;
the **sustained-adverse fills are the entire loss.** Whoever can dodge the adverse fills keeps
a real edge (+$0.2 to +$1.4/market, larger on the wider-spread alts); whoever can't (a slow
API account) eats them and is net-negative. This *exactly* explains the winners' profitability
and our −EV — the edge is **execution-speed-gated**, not strategy-gated.

> ⚠️ The cancel ceiling uses *future* price info (perfect foresight) — it is an **upper bound,
> not achievable**. Real cancellation is imperfect and signal-driven; a pro with the 250–500 ms
> registered-maker advantage captures most of it, a ~150–600 ms-latency Railway API account
> (what we measured in Session 44) captures little. Our realistic EV sits near the **negative
> worst-queue end**, not the positive ceiling.

## Verdict & recommendation

**No asset clears the positive-net-EV bar for a small, non-HFT two-sided maker.** Ranked
least-bad → worst under realistic execution: **BTC** (≈ breakeven, but only by barely filling)
> XRP > ETH > SOL. But "least-bad" is not "positive," and BTC's breakeven is achieved by *not
trading*. **I do not recommend a live two-sided-maker experiment.**

The blocker is **not** capital, asset choice, spread, quote lifetime, or inventory rules — the
sensitivity sweep ruled all of those out. The blocker is **execution speed**: the edge is real
(the ceiling proves it) but is captured only by avoiding adverse fills via fast cancellation +
the registered-maker latency advantage. That is an **infrastructure problem**, not a trading
tweak.

**If we want to pursue this edge, the next step is not a trade — it's an execution-capability
question:** can we obtain (a) the Polymarket registered-maker latency advantage and (b) a
sub-second cancel loop co-located near the CLOB? If yes, re-run this simulator with a realistic
(imperfect) cancellation model to estimate the *achievable* fraction of the ceiling. If no, a
two-sided maker is structurally −EV for us and we should not deploy capital into it.

**Lowest-risk path if you still want a live data point:** the *only* config that is not
clearly money-losing is **BTC, early-window, long quote lifetime, tight inventory cap** —
which is breakeven precisely because it almost never fills. A tiny live run there would mostly
measure our *real* fill/adverse/cancel characteristics (the missing inputs above) at minimal
cost, rather than test a profitable strategy. I'd only do it to calibrate the latency/cancel
model, not as a profit attempt — and only with your explicit approval.
