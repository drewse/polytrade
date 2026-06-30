# BTC 5M — Strategy Research v1 (post-Session-44)

**Mandate:** think like a quant, not an executor. Use everything (live results, paper
sim, alpha/ML research, microstructure, wallet research, calibration) to design the
highest-EV strategy — even if it looks nothing like the join-best-bid baseline. Then
recommend ONE strategy and the minimum-risk way to validate it.

**Bottom line up front:**
1. The current baseline (passive join-best-bid maker at ~50/50) is **structurally
   weak** and should not get more live capital as-is. The evidence converges on this.
2. The single most striking signal in all our data is a **market under-reaction /
   favorite-underpricing** pattern (the >0.5 side wins far more than priced, +5 to
   +11 pts). It is the highest-*potential*-EV lead — **but it is confounded with a
   2-week UP-drift** and has never been backtested on the favorite side or with
   execution costs.
3. Therefore the highest-EV **action** is a **zero-capital offline experiment** that
   disambiguates "durable under-reaction edge" from "UP-drift variance." It costs $0,
   risks nothing, and is decisive. Only if it survives do we spend ~$40 on a live test.

---

## 1. Consolidated evidence (the numbers that matter)

| Source | Finding |
|---|---|
| **Live Session 44** (n=3 fills, 45s rest, join-bid) | fill 37.5%; **realized spread −0.025** (filled above mid); **5s mark-out −0.058 on all 3**; 30s reverted (+0.022); all UP, all won (+$7.30 = directional variance). |
| **Paper maker forward test** (911 quotes, join-bid/5s/worst-queue) | **fill 1.1%**; EV/fill +0.058 but **P(EV>0)=0.64, CI95 [−0.25,+0.36]** (straddles 0); 2/7 validation gates; never reached 100 fills. spread_captured +0.068 is *entry-time* (optimistic) vs live *fill-time* −0.025. |
| **Alpha / ML / strategy lab** | Recurring explicit verdict: **"efficient market, no durable post-cost edge."** BTC leads PM by ~seconds (xcorr ~0.05) but "too weak to overcome spread/slippage." Fair-value AUC up to ~0.85 yet EV not significant after costs. Momentum/reversal/fade fail OOS gates. |
| **Microstructure / validation failure profile** | Losers over-represented in: **strong BTC move (>0.12%), high vol (>0.09%), BTC leading (lag>8%), heavy imbalanced flow (>0.4), thin liquidity (<$100), large-trade present.** Tick 0.01; "wide" spread >0.06. No time-of-day effect. L2 book capture is currently broken (separate bug). |
| **Profitable wallets** (drew-finds: @std0 +$3,818, 12 lookalikes net-profitable) | They **make (passive), buy the CHEAP side avg ~0.41, hold to resolution**, very high frequency (std0 ~252 trades/day, buy_pct 85%). |
| **Longshot lab — cheap side** (n=3,302) | Buying the cheap side is **significantly −EV**: mid −0.035 (t=−5.9), maker −0.15, taker −0.24, all P(EV>0)=0. Verdict: **"wallets' edge is NOT longshot bias (likely execution/latency)."** |
| **Longshot lab — CALIBRATION (the buried signal)** | slope **1.105** (market under-reacts). Favorite (>0.5) side underpriced: 0.54→0.65 (**+0.109**), 0.65→0.75 (+0.104), 0.85→0.93 (+0.081). **Never backtested on the favorite side.** |
| **My confound check on the calibration** | All-favorite mid-EV **+0.0355/trade**, but split: **UP-favorite +0.064 (n=1623) vs DOWN-favorite +0.008 (n=1679).** The edge is almost entirely UP-side ⇒ likely a sample UP-drift, not a symmetric bias. |

### What this rules in and out
- **Direction from features (BTC move, flow, momentum):** ruled OUT — markets efficient
  after costs; this is the most-replicated result in the codebase.
- **Pure spread-capture making at 1¢:** ruled OUT as a standalone — half-spread ≤0.5¢ is
  swamped by adverse selection (live 5s mark-out −5.8¢; realized spread negative);
  paper P(EV>0)=0.64. The spread is too thin to pay for the liquidity risk.
- **Copying the cheap-buying wallets:** ruled OUT — buying the cheap side is −EV in our
  data; their P&L is execution/latency/HFT (≈252 trades/day) or survivorship, not a
  signal we can replicate as a small, slow maker.
- **Under-reaction / favorite-underpricing:** the one OPEN lead — large mid-edge,
  but confounded with UP-drift and untested on execution. **Must be disambiguated.**

---

## 2. The reframe: where can edge actually live?

A binary maker BUY at price `p` has **EV/fill = P(win | filled) − p**. There are only
three ways to be +EV:

1. **Capture spread** so that even at `p ≈ fair`, buying below mid nets the half-spread
   faster than adverse selection erodes it. → Needs spread ≫ adverse cost. Here spread
   ≈1¢, adverse ≈5¢. **Fails.**
2. **Be paid by uninformed flow** (provide liquidity where takers are noise). → We can't
   yet identify noise vs informed; validation losers cluster exactly where flow is
   heavy/informed. **Unproven, hard.**
3. **The price itself is biased** (`p ≠ fair`), so you take a directional value position.
   → This is the favorite/under-reaction signal. It does NOT require predicting from
   features (which is efficient); it only requires the *price level* to be miscalibrated.
   **The one place the data hints at edge — pending the confound test.**

Key execution corollary: for #1 (spread capture) being a *taker* is suicide; for #3
(a real pricing edge) being a *taker* is fine — you pay 1¢ once to lock a multi-point
edge. **The baseline optimized for #1 in markets that don't offer it.** If edge exists
here, it's #3, and the right execution is aggressive/taker, not passive-at-the-touch.

---

## 3. Candidate strategies (intuition / evidence / weakness / why-outperform / validation)

### H1 — Under-reaction "favorite value" bet  ★ recommended lead
- **Intuition:** the 5m market prices too timidly; when one side leans >0.5 the outcome
  resolves more decisively than priced (slope 1.105). Buy the favorite (the >0.5 side),
  hold to resolution.
- **Evidence:** calibration n=3,302; favorite mid-EV +0.0355 avg, **+0.10 in the 0.55–0.70
  band**; monotonic; underlying cheap-side t=−5.9 ⇒ favorite-side ≈ +5.9.
- **Weaknesses / risks:** (a) **confound** — UP-favorite +0.064 vs DOWN-favorite +0.008,
  so it may be 2-week UP-drift, not bias; (b) signals are not independent (≈419 markets
  generate 3,302 signals → effective n much smaller, t inflated); (c) **mid ≠
  executable** — cheap-side taker cost was a brutal −0.20 vs mid, so execution can erase
  it; (d) momentum strategies already failed OOS after costs — tension to resolve.
- **Why it could outperform baseline:** it targets pricing bias (#3), a multi-point edge,
  instead of a ≤0.5¢ spread; and it's executed to *guarantee* the position rather than
  fish for adverse fills.
- **Validate with fewest live trades:** **ZERO live trades first.** Extend the longshot
  lab with a *favorite* arm (buy the side priced in [0.55,0.80], hold) across mid/maker/
  taker execution, with: real book spread where available, **market-level** train/val/
  holdout split, walk-forward by week, **UP-favorite vs DOWN-favorite separation**, and
  cost sensitivity. Decision gate: favorite EV/trade > realistic taker cost, P(EV>0)≥0.95,
  positive in BOTH up- and down-trend weeks, DOWN-favorite edge clearly >0. Then, only if
  it passes, ~20–30 live $1–2 taker buys to confirm executable fill prices vs settled win.

### H2 — Spread-conditioned passive maker (only quote when spread ≥ 2¢)
- **Intuition:** making is only +EV when the half-spread exceeds adverse-selection cost;
  quote *only* the rare wide-spread moments.
- **Evidence:** EV math (spread must beat adverse ≈5¢ at 5s); paper edge is marginal at
  1¢; live realized spread negative at 1¢.
- **Weakness:** wide spreads are rare and often *caused by* volatility/uncertainty (more
  adverse, not less); may shrink sample to nothing; "wide>0.06" buckets were not shown to
  be reliably +EV.
- **Why it could outperform:** removes the structurally-losing 1¢ quotes that dominate
  the baseline; at minimum a strict improvement in EV/fill.
- **Validate:** offline filter on the existing paper-quote data (spread bucket × EV); no
  live trades needed for the first read.

### H3 — Toxic-regime avoidance overlay (don't quote in the losing regimes)
- **Intuition:** skip the conditions that over-represent among losers.
- **Evidence:** validation failure profile (strong move / high vol / BTC-lead / heavy
  flow / thin liq / large trade).
- **Weakness:** a risk filter, **not an edge** — improves a marginal strategy, can't make
  a −EV one +EV; risk of overfitting 6 hand-picked conditions.
- **Why it could outperform:** raises EV/fill of whatever core strategy it wraps.
- **Validate:** offline, as an overlay on H1/H2 backtests.

### H4 — Selective/ranked quoting instead of equal treatment
- **Intuition:** rank markets by expected edge (calibration band + spread + regime) and
  only act on the top; the baseline treats every fresh market equally.
- **Evidence:** the calibration shows edge is band-dependent; failure profile shows
  regime-dependent losses. Equal treatment averages winners with structural losers.
- **Weakness:** ranking is only as good as the per-market edge estimate, which is exactly
  what's unproven.
- **Why it could outperform:** concentrates capital where (if) edge exists. This is the
  *form* H1/H2 should take, not a standalone strategy.
- **Validate:** falls out of the H1 offline backtest (EV by band/regime).

### H5 — Baseline join-best-bid maker (status quo)  ✗ reject as standalone
- **Evidence against:** efficient 50/50, 1¢ spread, adverse selection, paper P(EV>0)=0.64,
  live realized spread −0.025. No live capital justified.

### Explicitly rejected: directional feature alpha, copy-the-cheap-wallets, longshots.

---

## 4. Recommendation + minimum-risk experiment

**Recommended strategy (highest potential EV): H1 — under-reaction "favorite value" bet,
executed as a taker/aggressive-maker, selective on the 0.55–0.75 band, hold to
resolution — CONTINGENT on clearing the UP-drift confound offline.**

I'm being deliberately honest about confidence: my genuine prior is that markets here are
mostly efficient and that this signal is **more likely UP-drift than durable bias** (the
DOWN-favorite edge of +0.008 is the tell). But it is the only place in *all* our data
with a multi-point mid-edge, the test to confirm/kill it is **free**, and the payoff if
real is large. That asymmetry (zero cost, decisive, high upside) makes running the test
the highest-EV move — and makes *not* deploying any live maker program until it resolves
also +EV (capital preservation vs a −EV baseline).

**Minimum-risk experiment — offline first, then a tiny live confirm:**

- **Phase 0 — Offline disambiguation (0 live trades, $0 risk, ~hours of compute).**
  Add a favorite arm to the longshot lab and answer one question: *is there a symmetric,
  cost-surviving, out-of-sample under-reaction edge, or is it UP-drift?*
  Gates to pass: (1) favorite EV/trade > realistic taker cost; (2) P(EV>0) ≥ 0.95 at
  **market-level** resampling (not signal-level); (3) positive in BOTH up-trend and
  down-trend weeks; (4) **DOWN-favorite edge clearly > 0** (kills the drift confound);
  (5) stable across the 0.55–0.75 band.
- **Phase 1 — Tiny live confirm (only if Phase 0 passes): ~20–30 taker buys, $1–2 each
  (~$40–60 max), on favorites in the 0.55–0.75 band, hold to resolution.** Measure
  realized fill price vs mid and settled win rate vs the +0.10 calibration prediction.
  This is *far* cheaper and more decisive than collecting 25–50 passive maker fills, and
  it tests the actual edge rather than the structurally-weak spread-capture mechanism.

**Decision tree:**
- Phase 0 fails (most likely) → there is **no demonstrated durable edge**; do not run a
  live maker program. Either stop live work or pivot the research question (e.g. study
  whether *we* can be the informed taker on a faster cross-market BTC signal — separate
  track).
- Phase 0 passes → run Phase 1; if live matches calibration, *then* scale within the $100
  caps with selectivity (H4) + regime avoidance (H3).

---

## 5. What I will NOT do (without your go-ahead)
- No change to the live quoting strategy beyond the settlement/accounting fix already
  shipped.
- No "collect 25–50 baseline fills" run — Session 44 + the paper test already tell us the
  baseline is structurally weak; more baseline fills mostly buy precision on a number we
  expect to be ~0. (This supersedes the earlier control-sample plan, per your "shift to
  strategy research" instruction — happy to still run it if you want the control point.)
- No live capital on H1 until the offline confound test passes.

**Proposed immediate next step:** build & run the Phase-0 offline favorite/under-reaction
backtest (zero risk) and report whether the edge is real or drift. Awaiting your go-ahead.
