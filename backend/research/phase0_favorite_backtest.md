# Phase-0 Offline Backtest — Favorite / Under-reaction Signal

**Question:** is the "favorite value / market under-reaction" signal a real, durable,
tradeable edge — or just a short-sample UP-drift artifact?

**Verdict: REJECT (0/5 gates). It is predominantly a 2-week UP-drift artifact, not a
durable edge. It does NOT justify a live trial.** No live trades were placed; no change
was made to the live executor. (Code: `btc5m_favorite_lab.py`, endpoints
`/api/btc5m/favorite/{run,status}` — research-only.)

---

## Dataset
- **913 BTC-5m markets**, **4,505 decision points (lab points)**, weeks **2026-W25 →
  2026-W26** (~2 weeks ending 2026-06-30). Same dataset the longshot lab used, plus the
  historical trade stream for maker-fill simulation.
- **Method:** ONE independent trade per market = the first decision point whose favorite
  price enters the band, **held to resolution**. One trade per market ⇒ trades are
  independent (no signal-correlation inflation). Confidence = **market-level bootstrap**
  (4,000 resamples).
- **Primary config:** favorite side (the side priced >0.5), band **0.55–0.75**.
- **Qualifying trades in band:** **488 markets/trades.**

## Headline numbers (band 0.55–0.75, hold-to-resolution, n=488)
| | EV/trade | Win rate | Avg entry | P(EV>0) | 95% CI |
|---|---|---|---|---|---|
| **Gross (at mid, no cost)** | **+0.0279** | 68.0% | 0.652 | **0.906** | **[−0.015, +0.068]** |
| Net @ realistic ~1–2¢ cost | +0.008 to +0.018 | 68.0% | ~0.66 | 0.64–0.73 | straddles 0 |
| Net @ data-spread taker | −0.110 | 68.0% | 0.790 | 0.00 | [−0.153, −0.069] |

> **Cost caveat (important):** the lab's `spread` proxy is inflated for these illiquid
> micro-markets (implied taker cost ≈ **14¢**, vs the **~1¢** we actually saw live in
> Session 44). So the **data-spread taker line is unrealistically pessimistic** — ignore
> its magnitude. The honest read is the **gross (mid)** edge and the realistic fixed-cost
> sensitivity. Even on the most generous (gross) basis the edge is small and **not
> statistically significant** (P(EV>0)=0.91 < 0.95; CI includes 0). Break-even cost ≈
> **2.3–2.8¢/trade**.

## The decisive confound test — UP-favorite vs DOWN-favorite (at mid)
| Subset | EV/trade @ mid | Win rate | n |
|---|---|---|---|
| **UP-favorite** | **+0.0396** | 68.7% | 291 |
| **DOWN-favorite** | **+0.0040** | 65.3% | 294 |
| **Control: always-buy-UP** | +0.0396 (≈ UP-fav) | 68.7% | 291 |

**This is the smoking gun.** A genuine, durable under-reaction/favorite bias would show a
clear positive edge on **both** UP- and DOWN-favorites. Instead the edge lives almost
entirely on UP-favorites (+4.0¢) while DOWN-favorites are ~flat (+0.4¢), and the
"buy-favorite" strategy is **indistinguishable from just "buy UP."** Over W25–W26 BTC
drifted up, so buying the up-leaning side won more than priced — exactly the artifact that
made Session 44 look like a 3/3 winner. It is **directional drift, not a pricing edge.**

Corroborating: by **trend alignment**, the edge concentrates in favorites that **align**
with the recent BTC move (continuation/momentum) and **disappears/​reverses** for
favorites **against** the move — i.e. it's momentum/drift, not calibration mispricing.

## Win rate, entry, P&L distribution (n=488)
- **Win rate 68.0%**, average entry **0.652** (mid).
- Binary payoff distribution: median +0.12, avg win +0.20, avg loss −0.78, **332 wins /
  156 losses**. The high win rate is expected (favorites win often) — the question is
  whether they win *more than priced*, and the answer (+2.8¢ gross, not significant) is
  "barely, and not reliably."

## Splits & sensitivity (all consistent with REJECT)
- **OOS (chronological holdout):** holdout does **not** improve on train — it's weaker.
  No out-of-sample support.
- **By week:** both W25 and W26 marginal/negative; only 2 weeks ⇒ cannot establish
  time-robustness either way.
- **By regime:** no regime rescues it (chop and mixed both fail).
- **Band:** the gross edge is best around 0.55–0.65; it does not become significant in any
  band.
- **Entry time:** entering later in the window is marginally better (less remaining
  variance) but does not change the verdict.
- **Maker on the favorite:** only **10 fills** in 2 weeks (favorites rarely get sold to
  you within 5s) — not a viable execution path.
- **Baseline:** the passive-maker paper test (EV/fill +0.058, P(EV>0)=0.64, n=10) is *also*
  not significant; the favorite strategy does not beat it on a risk-adjusted basis.

## Gate scorecard
| Gate | Result |
|---|---|
| 1. Positive net EV after costs | **FAIL** — strongly negative at data-spread; only +0.8–1.8¢ at realistic 1–2¢, and not significant |
| 2. Positive out-of-sample | **FAIL** — holdout weaker than train |
| 3. Non-negative on BOTH UP- and DOWN-favorites | **FAIL** — edge is UP-only (UP +4.0¢ vs DOWN +0.4¢); ≈ "buy UP" |
| 4. Robust across time periods | **FAIL** — only 2 weeks, both marginal; ≈ one BTC uptrend |
| 5. Statistical confidence | **FAIL** — gross P(EV>0)=0.91 < 0.95; CI [−0.015, +0.068] includes 0 |

**Passed: 0/5.**

## Statistical confidence
On 488 independent markets, the **gross** (best-case, zero-cost) edge is +2.8¢/trade with
a market-level bootstrap **P(EV>0) = 0.91** and **95% CI [−1.5¢, +6.8¢]** — i.e. we cannot
reject zero even before paying any spread. The decisive UP/DOWN asymmetry (and the
always-UP control matching the favorite EV) does not depend on the cost model at all.

## Recommendation
**Do not run a live trial of the favorite/under-reaction strategy.** The signal is
predominantly a 2-week UP-drift; its symmetric (DOWN-favorite) component shows no edge; the
gross edge is small and statistically insignificant; it doesn't strengthen out-of-sample;
and at any realistic cost it's indistinguishable from zero. This Phase-0 test did its job —
**it cost $0 and steered us away from a −EV live program.**

Net position after this phase: across **all** approaches examined (directional/feature
alpha, passive spread-capture making, cheap-side longshot, favorite/under-reaction), **no
durable, executable, statistically-supported edge has been demonstrated** on BTC 5m. The
honest stance is capital preservation: no live deployment is justified on current evidence.

### Options I can take next (your call — no live trades without your approval)
1. **Stop the live-maker program** and treat BTC 5m as "no demonstrated edge" (defensible).
2. **Widen the evidence base cheaply (offline):** extend the dataset beyond 2 weeks to
   include genuine BTC down-trend periods and re-run the UP/DOWN-balanced test — the only
   way to truly separate "under-reaction" from "drift." Still zero live capital.
3. **Pivot the research question** to whether *we* can be the informed taker on a faster
   cross-market BTC signal (separate track; the labs say it's "real but too weak after
   costs," so I'd expect this to fail too, but it's offline-testable).

My recommendation: **option 2 first** (extend to a down-trend sample and re-test offline),
and **do not deploy live** in the meantime.
