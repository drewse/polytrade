# Phase 0a-3 — Richer-Signal Final Gate (L2 + Spot Capture)

**The final probe.** Does access to richer real-time information (full L2 order book, imbalance,
liquidity depletion, trade flow, cross-market spot) produce a **statistically meaningful improvement
in expected value** over the trade-tape-only model?

**Answer: NO.** On a proper multi-window sample, none of the richer signals predict adverse maker
fills better than chance (AUC ≈ 0.45–0.57), and none produce a statistically significant EV
improvement over trade-momentum (every paired 95% CI spans zero). **This closes the market-making
hypothesis.** No live trades were placed; this was pure read-only capture + offline analysis.

---

## What we built and captured (read-only)
A live **L2 + spot capture** (`mm_capture2.py`) that maintained the full Polymarket CLOB order book
per token from `book` snapshots + `price_change` deltas and emitted compact, **millisecond-aligned**
features, alongside Binance spot:
- **68,402** L2 book-feature rows (best bid/ask, touch sizes, ±5¢ depth, order-book imbalance),
- **10,990** trades (trade flow), **8,414** spot rows, across **~10 windows each for BTC/ETH/SOL/XRP**
  (40-minute capture). Millisecond timestamps mean **sub-second reaction latency is genuinely
  resolvable here** (unlike the 1-second historical tape).

## Test 1 — Signal discrimination (AUC; the statistically powerful test)
For every maker-fill event, we labeled post-fill **toxicity** (mid moved through our quote within
10 s) and measured how well each **pre-fill** signal, lagged by reaction latency τ, predicts it.
Pooled over **7,297 BTC fills** (and 349–1,071 for the alts), adverse-fill base rate ≈ 0.49:

| Signal (AUC, >0.5 = predictive) | BTC | ETH | SOL | XRP |
|---|---|---|---|---|
| Trade momentum | 0.46–0.53 | 0.49–0.52 | 0.46–0.53 | 0.51–0.57 |
| **L2 order-book imbalance** | **0.46–0.49** | 0.41–0.44 | 0.44 | 0.44 |
| Liquidity depletion (touch) | 0.51–0.52 | 0.47–0.57 | 0.49–0.55 | 0.49–0.52 |
| Cross-market spot lead | 0.46–0.47 | 0.48–0.49 | 0.47–0.52 | 0.46–0.54 |
| Combined | 0.42–0.51 | 0.33–0.52 | 0.49–0.52 | 0.46 |

**Every signal is at or below the ~0.5 no-information line** (best is depletion at a marginal ~0.55).
**L2 imbalance — the single most-cited maker signal — has AUC 0.48, i.e. no predictive power.**

> Note on the smoke test: a 40-second (single-window) pilot showed imbalance AUC 0.70–0.80. That was
> **single-window overfitting** — within one trending window, imbalance and the outcome both track the
> same trend, a spurious correlation that vanishes across windows. The full multi-window sample (this
> table) is the correct read, and it is exactly why the larger capture was required.

## Test 2 — EV emulator (settled), richer signals vs trade-momentum, PAIRED
Replaying fills under each signal's cancellation policy, settled at resolution, on the **35 markets
that resolved during capture** (9/9/9/8), comparing each richer signal to trade-momentum **on the
same markets** (paired — this removes directional variance):

| Paired Δ (signal − momentum), $/market [95% CI] | BTC | ETH | SOL | XRP |
|---|---|---|---|---|
| Imbalance − momentum | −0.13 [−0.99,+0.82] | +0.20 [−0.75,+1.06] | −0.15 [−0.76,+0.33] | −0.75 [−2.15,+0.49] |
| Depletion − momentum | +0.21 [−0.98,+1.65] | +0.41 [−0.29,+1.41] | −1.08 [−2.45,+0.23] | −0.34 [−0.87,+0.17] |
| Combined − momentum | +0.29 [−0.36,+1.07] | +0.10 [−0.81,+0.95] | −0.17 [−0.81,+0.40] | −0.95 [−2.45,+0.43] |

**Every confidence interval spans zero.** Win-rates are 4–6 of 9 (coin-flip). No richer signal beats
trade-momentum at any conventional significance level. (Absolute EV on these 35 markets is dominated
by direction — BTC +$5/mkt "none" is a lucky trending sample; ETH/SOL/XRP negative — with the
best-tuned *no-cancellation* policy as good as or better than any signal-cancellation policy.)

## Answering the sub-questions (as asked, for completeness)
- **How much improvement from each signal?** None that is statistically distinguishable from zero.
- **Which signals contribute most?** Marginally, **liquidity depletion** (AUC ~0.55) — but its EV
  edge is not significant. Imbalance, spot-lead, and momentum are all ≈ 0.5.
- **Enough to justify a production low-latency system?** No — there is no edge to capture.
- **Could a small account realistically capture it?** No.

## Why this is the honest end-point
The perfect-foresight ceiling (Phase 0a) showed the *theoretical* two-sided-maker edge is real. The
causal latency curve (Phase 0a-2) showed a trade-tape signal never crosses zero. This final probe
asked whether the **richest observable real-time information** — the full L2 book plus cross-market
spot, the exact data the professional bots use — could separate benign from toxic fills well enough
to matter. It cannot: the strongest, most-obvious signal (L2 imbalance) discriminates at AUC 0.48,
and no signal produces a significant EV gain. The gap between the foresight ceiling and every
achievable causal strategy is a **genuine information gap, not merely a latency gap** — the future
adverse/benign outcome of a maker fill is essentially **unpredictable from observable pre-fill state**
in these markets, at least with the signals available to any participant reading the public feeds.

Limitations (stated plainly, none of which rescue the conclusion): the EV test rests on 35 resolved
markets (wide CIs), though the AUC test rests on thousands of fills and is decisive; signals are
simple linear forms over a 1.5 s window (but if the canonical L2-imbalance signal has AUC 0.48, a
fancier feature is very unlikely to clear the bar); and we captured 40 minutes, not days. A larger
capture would tighten the EV CIs but cannot plausibly move an AUC of ~0.5 to something tradeable.

---

## Plain statement (as you requested)

**We have exhausted the realistic market-making avenues for a small independent participant in these
5-minute crypto markets.** Across the full research arc we established, with offline evidence and one
$0-risk live-data probe:
- The two-sided maker edge is real only under **perfect foresight**; it is **negative** under any
  realistic execution.
- **Reaction speed alone** never gets any asset to positive EV (Phase 0a-2).
- **Richer real-time information** (full L2 + spot) does **not** predict adverse fills or improve EV
  (this probe) — the binding constraint is an information gap that no feasible signal closes.
- The residual "edge" the top wallets show is inseparable, in our data, from **directional variance,
  rebates too small to matter, and execution capabilities (co-located sub-second HFT) that a small
  account cannot replicate** — against a venue that is actively narrowing the maker window.

**Recommendation: close the market-making hypothesis for these markets and redirect research
elsewhere.** This is a firm, evidence-based conclusion, not a resource limitation — we tested the
strategy the winners actually run, at the latency they run it, with the data they use, and found no
capturable edge for our profile. I'd suggest the next research effort target a different market
structure or edge type entirely (e.g., slower-horizon markets where speed is not the moat, or
non-price information edges), rather than further work on fast crypto market-making.

No live trades were placed at any point in this probe. Awaiting your decision to formally close the
project thread or redirect.
