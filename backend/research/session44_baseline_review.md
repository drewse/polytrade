# Session 44 — Baseline Research Review (BTC 5M Live Maker)

**Status: BASELINE. No optimization applied. Data-only review.**

Live session 44 · mode=LIVE · 2026-06-30 05:35:51 → 05:50:51 UTC (15 min armed) ·
join-best-bid · queue lifetime 45s · 5 shares/order · caps: $3/order, $8 exposure,
$10 session-loss, $100 budget, $100 cumulative-loss lock.

> ⚠️ Two instrumentation defects shaped this session and must be read first
> (details in §"Data-integrity caveats"). They do **not** invalidate the
> execution-microstructure data, but they mean settlement P&L below is
> **reconstructed by hand from CLOB**, not produced by the executor.

---

## 1. Overall session summary

| Metric | Value |
|---|---|
| Orders posted (real) | 8 |
| Orders filled | 3 |
| Orders cancelled (unfilled) | 5 |
| Partial fills | 0 |
| Fill rate | **37.5%** (3/8) |
| Distinct markets quoted | 2 (1:35–1:40 ET ×1, 1:40–1:45 ET ×7) |
| Venue rejections | 0 |
| Risk-check rejections | 99 (98 × "max concurrent exposure", 1 × "arm expired") |
| Settled fills (in DB) | 0 — settlement never ran (bug) |
| Settled fills (reconstructed) | 3/3 |
| Win rate at settlement | **3/3 = 100%** (all bought UP; BTC closed up both windows) |
| Gross P&L (reconstructed) | **+$7.30** |
| Fees | $0.00 (maker fee = 0) |
| Net P&L (reconstructed) | **+$7.30** |
| Capital deployed (filled cost) | $7.70 |
| Cumulative-loss lock | Not triggered (budget intact) |

**One-line read:** 3 fills, all UP, all won, +$7.30 — but this is **directional
variance over a ~10-minute BTC up-move, not evidence of maker edge**. Every fill
showed immediate adverse selection at 5 s. The session also **deadlocked after
~3 minutes** because filled positions never settled to free exposure.

---

## 2. Orders — full ledger

| # | Order | Market (ET) | Status | Px | Mid@quote | Spread | Queue ms | Submit ms | adv_5s | adv_30s |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 4e4f2057 | 1:35–1:40 | **filled** | 0.52 | 0.525 | 0.01 | — (filled) | 583.1 | −0.075 | −0.045 |
| 2 | 1742b4cc | 1:40–1:45 | **filled** | 0.51 | 0.515 | 0.01 | — (filled) | 86.5 | −0.065 | +0.055 |
| 3 | 550b4c25 | 1:40–1:45 | **filled** | 0.51 | 0.515 | 0.01 | — (filled) | 86.4 | −0.035 | +0.055 |
| 4 | bf4bb805 | 1:40–1:45 | cancelled | 0.50 | 0.505 | 0.01 | 45,971 | 93.8 | — | — |
| 5 | d815ccf7 | 1:40–1:45 | cancelled | 0.50 | 0.510 | 0.02 | 45,450 | 101.2 | — | — |
| 6 | 72d4d8bd | 1:40–1:45 | cancelled | 0.50 | 0.505 | 0.01 | 46,130 | 84.6 | — | — |
| 7 | 3dafe6d3 | 1:40–1:45 | cancelled | 0.50 | 0.505 | 0.01 | 46,110 | 109.3 | — | — |
| 8 | 425465bc | 1:40–1:45 | cancelled | 0.50 | 0.505 | 0.01 | 46,119 | 90.6 | — | — |

### Settlement (reconstructed from CLOB `tokens[].winner`)

Both windows resolved **UP** (Up token winner=True, price=1). We bought UP/YES on all three.

| Fill | Px | Shares | Cost | Outcome | Payout | Realized P&L | Settlement mark-out |
|---|---|---|---|---|---|---|---|
| 1 (1:35–1:40) | 0.52 | 5 | $2.60 | UP won | $5.00 | **+$2.40** | +0.48/sh |
| 2 (1:40–1:45) | 0.51 | 5 | $2.55 | UP won | $5.00 | **+$2.45** | +0.49/sh |
| 3 (1:40–1:45) | 0.51 | 5 | $2.55 | UP won | $5.00 | **+$2.45** | +0.49/sh |
| **Total** | | 15 | **$7.70** | 3/3 | $15.00 | **+$7.30** | |

---

## 3. Execution-quality statistics

**Latency**
- Submit latency: mean 154.5 ms, **median 92.2 ms** (mean skewed by the first/cold
  order at 583 ms; steady-state ~85–110 ms).
- Ack latency: mean 154.4 ms (≈ submit; ack is effectively immediate on post).
- Fill latency (submit → first fill): mean **5,234 ms** (~5.2 s).
- Cancel latency: ~30–35 ms; **cancel success rate 100%** (5/5).

**Queue lifetime (cancelled orders, n=5)**
- Mean 45,956 ms; **median 46,110 ms**. Confirms the 45 s rest target (the ~1 s
  overshoot is the 2 s worker poll granularity). Filled orders have no queue
  lifetime (they converted before cancel).

**Spread**
- Average **quoted** spread: **$0.0112** (seven markets at 1¢, one at 2¢) — books
  are tight; there is almost no spread to capture as a maker.
- Average **realized** spread (mid@fill − fill px), server metric: **−$0.025**
  (negative ⇒ we were filled *above* the prevailing mid, i.e. picked off, not
  capturing the half-spread). *(Per-order realized-spread is not surfaced in the
  orders API view — value taken from the server-side session summary.)*

**Mark-outs / adverse selection**
- **5 s mark-out: −0.075, −0.065, −0.035 → mean −0.058. All three negative.**
  Mid moved against us 3.5–7.5¢ within 5 s of *every* fill.
- 30 s mark-out: +0.055, +0.055, −0.045 → mean **+0.022**. Two of three *reversed*
  by 30 s; only one stayed adverse.
- Distribution read: **uniformly adverse at 5 s, mostly mean-reverting by 30 s**,
  and ultimately all three settled in-the-money. The adverse move at the touch
  looks largely transient/microstructural at this sample — but n=3.

**Counterfactual (±1 tick):** **Not available.** `_counterfactual` runs inside
settlement, which never executed → 0 counterfactual rows. Will populate once the
settlement bug is fixed.

---

## 4. Market selection / skips / rejections

**Selected (and why):** Only the freshest open BTC-5m windows with a two-sided
book — 1:35–1:40 and 1:40–1:45 ET. Selection reason on every order:
*"freshest open BTC-5m window with a two-sided book (spread 0.01, edge 0.005)."*
Estimated edge was a flat +0.005 (half of a 1¢ spread) — i.e. the model's
*assumed* maker edge, not a realized one.

**Skipped (and why):** The only decision-time skip reason observed was
*"already quoting this market"* (the busy-market guard preventing duplicate
resting orders in the same window).

**Rejections (99 total, all pre-submission risk-check, 0 from the venue):**
- 98 × **"max concurrent exposure ($10.x > $8.0)"** — fired continuously from
  ~05:48 to 05:50 because 3 positions (~$7.7) were held and a 4th (~$2.8) would
  breach the $8 cap. **This is the deadlock.**
- 1 × "arm expired" at 05:50:51 (the final attempt as the session closed).
- **Zero venue/exchange rejections** — every submitted order was accepted and
  acked; all cancels succeeded.

---

## 5. Data-integrity caveats (READ BEFORE INTERPRETING)

1. **Settlement never ran → no auto P&L.** `get_resolution()` looks up resolution
   via Gamma `…/markets?slug=btc-updown-5m-<ts>`. **Gamma returns empty for closed
   BTC-5m markets** (CLOB still serves them with `tokens[].winner`). So
   `resolved` stayed False forever, no position ever settled, `realized_pnl`/`won`
   stayed null, and the auto-summary shows `settled_fills: 0, net_pnl: 0`. The
   +$7.30 above is reconstructed by hand from CLOB. **Ledger is currently wrong:**
   `committed_capital = $7.70` (phantom — positions are actually resolved to
   $15.00 cash) and `cumulative_realized_pnl = $0` (actually +$7.30). This must be
   corrected before the next session or the $100 budget/loss accounting drifts.

2. **Exposure deadlock.** Settlement is only attempted inside the armed
   `run_cycle`. Because (1) blocked settlement, the 3 early fills pinned exposure
   at the $8 cap and **no new order could be posted for the final ~10 minutes** —
   99 rejects. The session's *effective* working window was ~3 minutes
   (05:36–05:39). So "8 orders in 15 min" overstates throughput; sustained
   throughput under this config is capital-bound, not time-bound.

3. **Events API truncation.** The events endpoint returned only the last 100 rows
   (all rejects + disarm); quote/submit/fill/markout events for the first 3 min
   were not retrievable via the API. Order-level fields were used instead.

None of these touch the latency / fill / spread / mark-out measurements, which are
recorded per-order at event time. They do block settlement-side analytics.

---

## 6. Answers to the six questions

### 1) Evidence the strategy has **positive** EV
- Thin/observational: **fills do happen** at a usable rate (37.5% at 45 s rest),
  latency is low (~90 ms submit, 100% cancel success), and **30 s mark-outs
  mean-reverted (+0.022)** on 2 of 3 fills — consistent with the adverse move
  being partly transient liquidity noise rather than permanent information.
- Net +$7.30 / 3 wins. **But this is not edge evidence** — it is one directional
  up-move (see below). I would not count the P&L as positive-EV evidence.

### 2) Evidence it does **not** (or that edge is fragile)
- **Realized spread is negative (−$0.025)** and **every 5 s mark-out is negative
  (mean −0.058).** As a maker we are consistently *picked off* at the touch rather
  than earning the half-spread.
- **Quoted spread is only ~1¢.** There is almost no spread to harvest, so the
  assumed +0.005 edge is razor-thin and easily erased by adverse selection.
- The entire P&L is **directional**: we only make money because the UP token we
  bought settled at $1. A maker buying the touch in a 1¢-wide 50/50 market has P&L
  ≈ (direction) − (adverse selection) − (~0 spread). Nothing here isolates a
  *maker* edge from the *directional* outcome.

### 3) Simulator assumptions **confirmed**
- **Longer rest ⇒ more fills.** Sim: ~2% at 12 s, rising with rest. Live: 37.5% at
  45 s — directionally confirmed (and, at tiny n, even higher than the sim curve).
- **Maker fills are adversely selected.** Sim's core thesis. Live: 3/3 negative
  5 s mark-outs and negative realized spread. Confirmed.
- **Maker/venue mechanics & fees.** 0 venue rejects, 100% cancel success, maker
  fee = $0, ~90 ms submit. The order-plumbing assumptions held.

### 4) Simulator assumptions **disproven / missing**
- **No spread to capture.** The sim's +0.005 edge assumes you sit inside a
  capturable spread; live books are 1¢ wide and you fill *through* the mid
  (realized spread negative). The edge assumption is optimistic.
- **Adverse selection is partly transient.** Sim treated adverse fills as
  permanently bad; live 30 s mark-outs reversed on 2/3. (n=3 — directional only.)
- **Capital-recycling / exposure dynamics were not modeled.** The sim assumed
  continuous quoting; in reality unsettled 5-min positions deadlock the exposure
  cap. This is a first-order operational constraint the backtest ignored.
- **Settlement data source.** Not a strategy assumption, but the Gamma-slug
  resolution path is wrong for closed markets — a pipeline gap the sim never hit.

### 5) Three highest-impact things to test next (ranked by EV)
1. **(Prerequisite, highest leverage) Fix settlement source + decouple it from the
   armed loop, and fix capital recycling.** Resolve via CLOB `tokens[].winner`;
   settle positions independent of `armed`; free exposure on settlement. *Without
   this, no session can produce >3 fills or any settled-P&L distribution — it
   gates every other measurement.* EV impact: unblocks the entire research program.
2. **Reduce adverse selection: test "quote one tick deeper" (passive, below touch)
   vs join-touch, and a shorter rest (e.g. 15–20 s).** Directly targets the
   negative realized spread / negative 5 s mark-out — the clearest negative signal
   we have. Expected to trade fill rate for better per-fill economics; the
   question is whether net EV improves.
3. **Scale to a direction-balanced sample via smaller positions / faster recycling
   within the same caps.** Hold baseline config constant and accumulate ≥100
   settled fills spanning both UP and DOWN outcomes, so we can finally separate
   *maker* edge from *directional* variance. This is the measurement that actually
   answers "is there EV."

### 6) Statistical confidence at this sample size
- **Effectively none for P&L/EV.** n = 3 fills, **all UP, in two adjacent
  (correlated) 5-minute windows** — really ~1–2 independent directional events.
  Win rate 3/3 has a 95% Wilson CI of roughly **[31%, 100%]**. A +$7.30 outcome is
  well within the variance of a single short BTC up-move. **No EV conclusion is
  supportable.**
- **Microstructure signals are directional, not significant.** The uniformly
  negative 5 s mark-out (3/3) and negative realized spread are *suggestive* and
  consistent with theory, but n=3 fills + 5 cancels carries no real power.
- **Target:** on the order of **100–400 settled fills, balanced across UP/DOWN**,
  before any EV claim or parameter optimization.

---

## 7. Recommended next experiment (no changes yet — for your approval)

**Do not optimize parameters.** The baseline is uninterpretable for EV until the
instrumentation is sound and the sample is large and direction-balanced. Proposed
sequence:

1. **Fix-only release (no strategy change):** (a) settlement via CLOB winner,
   (b) settlement runs regardless of armed state until all positions resolve,
   (c) exposure freed on settlement, (d) backfill-settle session 44's 3 positions
   so the ledger reads committed $0 / cumulative +$7.30. Re-verify on a 1-order
   live check.
2. **Baseline data-collection run (unchanged strategy):** repeat join-best-bid /
   45 s across several sessions at different times of day until **≥100 settled
   fills** accumulate, explicitly logging UP vs DOWN balance. Goal: first real
   distributions for fill rate, realized spread, 5 s/30 s mark-out, and settled EV
   with confidence intervals.
3. **Only then** branch into the aggression experiments (deeper quote / shorter
   rest) from §5.2, as controlled A/Bs against the baseline.

I have **not** implemented any of this. Awaiting your review of the data before we
iterate.
