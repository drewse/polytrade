# Phase 0a-2 — Causal-Cancellation Emulator: EV vs Reaction Latency

**The decision experiment.** Using only information available in real time (no foresight), how
fast must a small two-sided maker react to become net-profitable, per asset? The deliverable is the
**EV-vs-reaction-latency curve**, and the precise transition point — if any — from negative to
positive EV.

**Answer: there is no transition. The curve does not cross zero for any asset at any reaction
latency from 100 ms to 5 s.** BTC reaches **breakeven** (~−$0.03/market, CI straddles 0) at fast
reaction; ETH/SOL/XRP are **deeply negative (−$5 to −$9/market) at every latency**. The binding
constraint turned out **not to be latency but SIGNAL QUALITY** — a realistic trade-flow cancellation
signal recovers only a sliver of the perfect-foresight ceiling. This is strong evidence the edge
requires speed **and** richer real-time signals (L2 book imbalance, cross-market) that a public
trade-tape strategy cannot supply. No live trades; no executor change. (Script: `research/mm_causal.py`.)

---

## Model (causal — past information only)
- Fixed quote cadence (re-quote every 1–2 s); **reaction latency `τ` controls only the
  cancellation**. We **pull** a side's quote when adverse **mid-momentum** is observable, computed
  from `mid(t−τ) − mid(t−τ−W)` (W≈1 s) — i.e. using data lagged by our reaction time. A through-trade
  (worst-queue) fills us **only if that adverse signal was not yet visible τ before the trade** (we
  couldn't pull in time). Adverse selection emerges from the real price path.
- Realistic small-account constraints: 5-share orders, net-inventory cap (10 shares/side),
  maker-only, 20% rebate, settlement P&L. Per (asset, τ) we report the **best** of a small
  threshold/cadence grid — i.e. the strategy's *best case*.
- **Resolution caveat (stated up front):** trade tapes are 1-second; sub-second τ (<1 s) uses
  deterministic intra-second jitter and is **lower-confidence + trade-density-dependent** (thin
  assets can't resolve sub-second — nothing trades in between). τ ≥ 1 s is directly supported.

## THE CURVE — net EV per market vs reaction latency (best-case tuning; market-level 95% CI)

| τ | BTC | ETH | SOL | XRP |
|---|---|---|---|---|
| 100 ms | −0.087 [−0.34,+0.18] | −6.66 | −7.91 | −8.85 |
| 250 ms | −0.175 [−0.38,+0.02] | −6.47 | −7.77 | −8.74 |
| **500 ms** | **−0.026 [−0.21,+0.15]** | −6.45 | −7.40 | −8.65 |
| 750 ms | −0.255 | −6.26 | −7.16 | −8.50 |
| 1.0 s | −0.141 [−0.35,+0.04] | −6.10 | −7.15 | −8.14 |
| 1.5 s | −0.162 | −5.52 | −7.21 | −7.90 |
| 2.0 s | −0.182 | −5.40 | −6.82 | −7.74 |
| 3.0 s | −0.260 | −6.64 | −7.08 | −7.96 |
| 5.0 s | −0.288 | −5.56 | −6.89 | −7.65 |

**Zero-crossing: NONE.** No asset is significantly net-positive at any latency. BTC's best cell
(τ≈0.5 s, −$0.026, CI [−0.21,+0.15]) is **statistical breakeven, not profit**. The alts never come
within $5/market of zero.

## Per-asset / per-τ metrics (representative, τ = 1 s)
| Asset | net EV/mkt | CI95 | fills/mkt | cancel-rate | matched-pair | adverse 5s | rebate/mkt | cap-eff | avg cap |
|---|---|---|---|---|---|---|---|---|---|
| BTC | −$0.14 | [−0.35,+0.04] | 14.6 | 0.17 | 1.00 | −0.007 | $0.07 | −0.4% | $40 |
| ETH | −$6.10 | [−6.74,−5.54] | 58.6 | 0.18 | 1.00 | −0.022 | $0.74 | −4.0% | $152 |
| SOL | −$7.15 | [−7.82,−6.52] | 53.9 | 0.12 | 1.00 | −0.028 | $0.69 | −5.1% | $141 |
| XRP | −$8.14 | [−8.80,−7.52] | 42.0 | 0.11 | 1.00 | −0.040 | $0.53 | −7.4% | $110 |

(Matched-pair rate is ~1.0 everywhere — both sides always fill — but, as Phase 0a showed, matched
legs sum to ≥$1 so there's no free merge spread; the loss is unmatched adverse inventory. Rebates
are real but ~10× too small. Capital efficiency is negative on every asset.)

## Why the curve never crosses zero — the real finding
1. **Latency is no longer the bottleneck; signal quality is.** Even at 100 ms, a **trade-momentum**
   cancel signal only dodges **6–28%** of adverse through-trades (cancel-rate column). The
   perfect-foresight ceiling (Phase 0a: +$0.2 BTC … +$1.4 XRP) assumed you *keep only benign fills*;
   a real, laggy momentum signal can't separate benign from toxic well enough, so most adverse fills
   still land. The gap between the ceiling (+) and this causal result (−) **is** the signal-quality gap.
2. **The asset ranking INVERTS vs the foresight ceiling.** With perfect foresight the thin, wide-spread
   alts looked best (XRP +$1.4). With a *realistic* signal they are **worst** (XRP −$8): at ~1 trade/
   second the momentum signal is too sparse/noisy to fire, so the maker just eats the wide adverse
   moves. **BTC is now the *least*-bad** because its dense tape (5–10 trades/s) makes the signal
   usable. *This reverses the earlier roadmap's "favor XRP/SOL" recommendation.*
3. **The pros must have signals we can't derive from the trade tape** — L2 order-book imbalance,
   near-touch liquidity depletion, queue position, and cross-market BTC-spot lead. Those are stronger
   adverse-fill predictors than trade momentum, but require **more real-time feeds** (full L2
   websocket + a spot data plane), i.e. *more* infrastructure, not just a faster cancel loop.

## What this means for the project decision
- **It is NOT "XRP works at 1.5 s, BTC needs 200 ms."** The honest result is **no asset works at any
  latency** with the signals a trade-tape strategy can compute. BTC merely reaches breakeven.
- The edge is therefore gated by **(speed) AND (multi-signal real-time infrastructure)** — a *higher*
  bar than the feasibility report's latency-only framing. Combined with that report's economics
  (small absolute EV at our size, ~0.55% of makers take ~50% of profit, and the venue actively
  closing the maker-cancel loophole), the evidence now points one way.

## Recommendation
**This is the natural end-point for the live two-sided-maker pursuit.** We set out to find the
minimum reaction speed for profitability; the answer is that **reaction speed alone does not get any
asset to positive EV** — BTC tops out at breakeven, the alts stay deeply negative, and closing the
remaining gap needs signal/data infrastructure (L2 + cross-market) on top of sub-second co-located
execution, for an absolute payoff that doesn't justify it at a small account's scale. I recommend we
**stop here on building a live maker for these markets.**

Two honest caveats that bound this conclusion (neither changes the recommendation, both are
offline-checkable if you want):
1. **Signal richness:** I could only test trade-flow momentum (the tape has no L2/cross-market). A
   richer-signal emulator would need live L2 + spot capture first — itself the infrastructure whose
   value is in question. If you want, the cheapest next probe is to *capture* L2 + BTC-spot for a few
   hours (read-only, free) and re-run the emulator with an order-book-imbalance signal to see whether
   it lifts BTC clearly above zero. If even that fails, the case is closed.
2. **Resolution:** sub-second τ is jitter-modeled; but the τ ≥ 1 s region (fully supported) is already
   uniformly negative-to-breakeven, and BTC's sub-second cells aren't significantly positive either,
   so finer resolution wouldn't rescue it.

**Net:** the project's central question is answered. The edge is real but reserved for operators with
both sub-second co-located execution *and* multi-signal data infrastructure; for a small account on a
trade-tape strategy, **no reaction latency makes it profitable.** Recommend ending the live pursuit
(or, at most, one free L2/spot-capture probe before final close-out). No live trades pending your call.
