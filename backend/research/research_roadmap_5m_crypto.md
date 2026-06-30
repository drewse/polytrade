# 5-Minute Crypto Markets — Combined Research Roadmap (Track A + Track B)

Goal: exhaust the plausible paths to an accessible edge in BTC/ETH/SOL/XRP 5-minute markets
before abandoning them. Two tracks: **A — reverse-engineer the winners into reproducible rules**
(don't assume speed unless an emulator proves it); **B — realistic low-latency architecture** for
a small account. No live trades; no executor change.

---

## TRACK A — Strategy reverse-engineering (what the winners actually do)

### A.1 Data-access reality (important, shapes everything below)
- Polymarket's `data-api /trades?user=` defaults to **taker fills only**. Pure makers return **0**
  there — you must pass **`takerOnly=false`** to see maker fills. (This is itself a clean
  maker/taker classifier: `taker_fraction = default_count / all_count`.)
- The trades feed is **depth-capped (~offset 3.5k–6k)**. Heavy re-quoters (w2 ≈ **95 fills/market**)
  exhaust that in ~37 markets, so per-wallet *deep* history isn't directly retrievable — pooling
  across many wallets is required for sample size.

### A.2 What the top wallets actually do (measured)
| Wallet | All-time P&L | Signature (from real fills) |
|---|---|---|
| **w1 `0x04b6`** | +$272k | taker-active; 614 trades/day; 100% BUY; two-sided in 65%; all price levels (mean 0.467); BTC 5m+15m; quotes first ~3 min; hold/redeem. |
| **w2 `0x568b`** | +$37k | **pure maker (0 taker fills)**; BTC-5m only; 100% BUY; **100% two-sided**; ~95 fills/market; merge+redeem. |
| **std0 `0xf3a6`** | +$3.8k | BTC-5m specialist; 252 trades/day; buy_pct 85%; avg 0.41; hold to resolution. |

### A.3 The decomposition that breaks the easy thesis
Reconstructing **w2's 5m-specific P&L** from its real maker fills (recent 35 settled markets):
- 5m P&L ≈ **+$796** (+$22.7/market), but split into **merge/spread −$2,206** and
  **directional +$3,002** (+ rebate +$147).
- **Matched-pair cost `price_up+price_down` median = 1.10 (>1).** w2 is **NOT capturing spread on
  merges** — if anything it overpays for matched inventory. The merge mechanic is **capital
  recycling, not arbitrage** (a matched pair pays $1 whether merged or held — P&L-neutral).
- So the profit, such as it is in this short sample, comes from **ending net-long the winning
  side** — i.e. directional inventory — which over 35 recent markets is **statistically
  indistinguishable from a BTC-direction tailwind** (the same trap as Session 44's 3/3).

**Implication:** the seductive "buy both legs <$1, merge for free spread" story is **false for the
real wallets** — their matched legs sum to ≥1. Their edge is *not* clean merge arbitrage.

### A.4 The emulator test (does any *non-speed* rule set reproduce a +EV maker?)
The Phase-0a two-sided simulator **is** the emulator: it replays maker-only two-sided quoting
against the real trade tapes with merge/unmatched/rebate accounting, and lets us sweep rule sets.
Result (115–119 markets/asset): **every realistic (worst-queue, no fast cancellation) rule set is
−EV** — across quote spread, lifetime, window phase, inventory caps, and queue position
(best=worst, so it isn't a queue problem). The sign flips positive **only** with the
perfect-cancellation ceiling (speed). So:
- A simple rule emulator **reproduces the winners' observable behavior** (two-sided, all-price,
  hold/merge) **but not their profitability** without the cancellation/selection (speed) component.
- The one thing that would make it rule-based-accessible — **avoiding the trending markets** — is
  not knowable in advance; the causal version ("stop quoting once in-window volatility spikes") **is
  a cancellation rule**, i.e. speed again.

I also re-ran the emulator at **low quote frequency** (the ~4-fills/market regime of the accessible
roster winner): still **−EV at worst-queue everywhere** — BTC −$0.046/mkt (~1 fill, closest to
breakeven), ETH/SOL/XRP −$0.42 to −$0.61. Fewer fills → less adverse → less-bad, but **never
positive without cancellation.** So a *naive* low-frequency maker doesn't explain the roster
winner either.

### A.5 Track-A verdict
The winners' 5m edge is **not reproducible by any slow, no-cancellation rule set** — heavy *or*
light frequency, any asset, any spread/lifetime/phase/inventory rule (all tested, all −EV at
worst-queue). It is some mix of (a) **execution speed** (cancel before adverse fills — Phase 0a's
proven lever, the only thing that flips EV positive), (b) **directional tailwind/variance**
(not durable — markets are efficient on direction; and our reconstruction can't even sign
heavy-re-quoters' P&L reliably), and (c) **rebates** (real but ~10–15× too small alone). The clean
"merge-arbitrage" story is **disproven** (real matched legs sum to ≥$1). **We do NOT assume "pure
speed" lightly — we tested every accessible rule set and none works without the cancellation
component.** The one genuinely open, accessible-looking lead is the **low-frequency two-sided
multi-asset maker** (roster `0xae3db1cc`), whose small measured edge our naive sim can't reproduce
— meaning if it's real, it comes from *selective cancellation at a ~1–2 s reaction*, which is the
exact thing the next offline test (A-1) is designed to price.

**ROSTER RESULTS (16 wallets, recent 5m fills, ranked by reconstructed P&L/market):**
- **The ecosystem is ~86% maker.** `taker_fraction ≈ 0.14` for nearly every wallet (a couple of
  pure-taker exceptions are losers). Making, not taking, is the universal mode.
- **Winners exist at BOTH frequencies:** a heavy re-quoter (`0xeebde7a0`, +$14/mkt, **91 fills/mkt**,
  two-sided 73%, BTC) *and* — importantly — a **low-frequency, two-sided, multi-asset winner**
  (`0xae3db1cc`, **+$4.6/mkt at only ~4 fills/mkt**, two-sided 71%, evenly across BTC/ETH/SOL/XRP).
  Below them a long tail of ~$0 wallets (efficient) and small losers.
- **Reconstruction caveat (load-bearing):** w1 (`0x04b6`, +$272k all-time) reconstructs to **−$164/mkt**
  here — a clear **artifact**: my crude `payout − Σcost` cannot handle **155 fills/mkt** with
  intra-window merges/redeems. **So per-market P&L for heavy re-quoters is NOT trustworthy from this
  method** (the Phase-0a fill simulator is the right tool for them). Low-fill wallets (1–4 fills/mkt)
  reconstruct far more reliably — and there the signal is **near-zero to mildly positive**.
- **Takeaway:** the most *accessible-looking* winner is the **low-frequency (~4 fills/mkt) two-sided
  multi-asset maker** — it makes minimal latency demands and is the one pattern my measurement can
  trust. Whether its small edge (+$4.6/mkt) is real or variance is the next offline test.

---

## TRACK B — Low-latency architecture for a small account

The feasibility report set the bar at **~250 ms** detect→cancel (the venue's taker-hold). Here is
a concrete, realistic architecture that meets it — explicitly NOT Railway/Python.

### B.1 Reference architecture
| Layer | Choice | Why |
|---|---|---|
| **Host** | VPS in **eu-west-1 (Dublin)** — Hetzner (~$5–10) or AWS c7g.large (~$53). KYC co-lo in eu-west-2 (London) optional later. | CLOB is AWS **eu-west-2 (London)**; Dublin is <~8 ms away and non-geoblocked. Railway US-West eats ~130 ms. Single fast core > many cores (hot loop is single-threaded). |
| **Market data** | **WebSocket-first**: `wss://…/ws/market` (`book` snapshot + `price_change` deltas + `last_trade_price`) → local book; `…/ws/user` for fills/order status. | REST polling is ~1 s stale and rate-limited. WS p50 ~14 ms. PING every 10 s; **inactivity watchdog ~120 s** (documented silent-freeze bug). |
| **Local order book** | In-memory book per quoted market, applied from snapshot+deltas, validated via the `hash` field. | The substrate for sub-second adverse-move detection. |
| **Quote/cancel loop** | **Event-driven** (not timed): on adverse mid move toward a resting quote → `DELETE /orders` (targeted) then re-quote `POST /orders`. No atomic amend; **never loop on `cancel-all`** (25/s cap). | This is the whole game — must fire inside ~250 ms. |
| **Signer** | **Native/Rust signer — bypass py-clob-client for the hot path.** Options: (1) **NautilusTrader** Polymarket adapter (Rust core, has WS+risk+backtester) — fastest to a robust MVP; (2) Rust CLOB SDK; (3) custom EIP-712 secp256k1 signer. | **Python `py-clob-client-v2` signs ~1 s/order — disqualifying.** EIP-712 ECDSA is sub-ms; the Python overhead is the problem. |
| **Order I/O** | **REST** place/cancel (no WS order entry exists). Pooled keep-alive HTTP; **L2 HMAC auth** (still must sign each order). Batch: 15 post / 1,000-ID cancel. | |
| **Risk engine (minimal)** | Per-side + net inventory caps, max concurrent exposure, per-window/day loss limit, **global kill switch**, and the venue's **10 s heartbeat auto-cancel as a dead-man's switch**. | Small but non-negotiable for live capital. |
| **SDK use** | py-clob-client **only** for one-time auth/key derivation; **bypass it in the hot loop**. Confirm **V2** + sig-type support (POLY_1271 is now the default for new accounts). | |

### B.2 Recommended build path
**NautilusTrader (Rust-cored) as the MVP harness** — it already provides a Polymarket integration,
WS handling, an event-driven engine, a risk layer, and an offline backtester (so we can validate
the *same* logic offline before live). Fall back to a **minimal custom Rust/Go service** only if
NautilusTrader's Polymarket adapter can't do the tight cancel/replace loop we need. Either way the
hot-path signer is native, not Python.

### B.3 Expected latency (Dublin, WS-driven)
- WS detect **~15 ms** + decision **~1–5 ms** + cancel RTT **~25 ms (warm p50)** ≈ **~45 ms p50**,
  **~80–150 ms p95** — **under the 250 ms bar.**
- **But** the venue's Cloudflare-fronted **p99 is 250–650 ms** with occasional >500 ms POST spikes —
  a fraction of fills are unavoidable regardless of stack. So the *achievable* capture is between
  Phase 0a's worst-queue (−EV) and the perfect-cancel ceiling (+$0.2–1.4/market), nearer the
  middle.

### B.4 Cost
- **Minimal:** ~$5–15/mo (Hetzner Dublin + free Polygon RPC + self-hosted monitoring).
- **Serious:** ~$110–365/mo (AWS c7g.large eu-west-1 + paid RPC + overhead).
- **Build effort:** **multi-week** (WS book + watchdog + event loop + Rust signer + risk + ops).

### B.5 The minimal live version (only if the calibration test passes — see below)
**One asset = XRP** (widest spread ~2¢ → most spread to capture; thinnest → least bot competition;
slowest moves ~2 s → most forgiving latency budget). **One market at a time. 5-share orders.
Maker-only, never cross. $100 hard cap, $10/session loss stop, kill switch + heartbeat dead-man.
Shadow mode first.** This is deliberately the *least-competitive, most-latency-forgiving* corner —
the opposite of where the BTC bots fight.

---

## Required deliverables — the roadmap

### 1. Viable paths still worth investigating
- **B-1 (primary):** Dublin + WS + Rust-signer two-sided maker on **XRP/SOL** (forgiving ~1–2 s
  latency budget, least competition). The only path with a *proven* +EV ceiling, and a latency bar
  our architecture can plausibly meet.
- **A-1:** A **selection/timing rule** that avoids trending windows *causally* (pull quotes when
  in-window realized vol spikes) — a "poor-man's cancellation" that needs only ~1–2 s reaction,
  testable offline first. (Lighter-weight cousin of B-1.)
- **A-2:** **Rebate-tilted** quoting — maximize benign two-sided fills for the 20% rebate while
  minimizing adverse, on the highest-volume venue. Marginal alone; only viable layered on B-1/A-1.

### 2. Likely-dead paths (and why)
- **Naive two-sided maker, no cancellation** — Phase 0a: −EV on all 4 assets, robustly. *Dead.*
- **Merge/spread arbitrage** ("buy both legs <$1, merge for free") — disproven; real winners' matched
  legs sum to **≥$1**. *Dead.*
- **Directional / favorite / under-reaction** — efficient market; Phase 0 favorite test was UP-drift
  (0/5 gates). *Dead.*
- **Cheap-side longshot** (the wallets' apparent style) — significantly −EV in our data. *Dead.*
- **BTC as our target market** — deepest, 1¢, sub-second moves, bot-saturated; we lose the queue and
  the latency race. *Dead for a small/slow account* (use it only as the hardest benchmark).
- **Railway/Python hot path** — 2 s polling + ~1 s/order signing + US-West. *Dead as-is.*

### 3. Paths requiring better infrastructure
- **B-1** (Dublin host + WS + Rust signer) — the whole speed strategy. Needs the new stack; the
  current one cannot reach 250 ms.

### 4. Paths testable purely offline (zero risk, do first)
- **A-1 causal-cancellation emulator:** extend the Phase-0a sim with a *causal* (no-foresight) pull
  rule — cancel a quote when in-window vol/΄move exceeds a threshold within a realistic 1–2 s
  reaction — and measure EV per asset. This distinguishes "needs perfect foresight" from "needs only
  ~1–2 s reaction" (which our architecture *can* hit). **Highest-value offline test.**
- **Roster expansion:** pool 20–50 wallets' recent 5m P&L + signature to confirm whether any winner
  is a *low-frequency, simple-rule* profitable trader (accessible) vs all heavy-re-quote makers
  (speed game).
- **Rebate sensitivity:** re-price Phase-0a fills with exact per-fill rebate to bound A-2.

### 5. Paths needing tiny live calibration (only after offline + approval)
- **The latency calibration probe** (from the feasibility report): shadow WS + one tiny
  far-from-mid self-cancelling order, measuring real detect→cancel p50/p95/p99 from **Dublin vs
  Railway**. Gate for B-1.
- **Minimal XRP maker (B.5)** — only if A-1 offline clears AND the latency probe clears 250 ms.

### 6. Ranked next actions by expected value
1. **A-1 offline causal-cancellation emulator** (zero risk, ~hours). *Decides the whole question:*
   if EV is +ve at a **1–2 s** reaction budget, the edge is reachable with a *modest* (non-HFT)
   upgrade and B-1 is worth building; if it needs **<250 ms** foresight, it's true HFT and we stop.
2. **Roster expansion** (zero risk) — confirm winner signature; cheap insurance against missing an
   accessible low-frequency pattern.
3. **Latency calibration probe** (≈$0, one dime order) — only if (1) is promising; measures whether
   *our* achievable Dublin latency meets the budget (1) implies.
4. **Build B-1 MVP on NautilusTrader** (weeks, ~$5–50/mo) — only if (1) and (3) pass.
5. **Minimal XRP shadow → tiny live** (after explicit approval) — the smallest real test.

**Discipline note:** actions 1–3 are free/near-free and *decisive*; do them before any build. The
binding uncertainty is **action 1's threshold** — is the required reaction ~1–2 s (reachable) or
<250 ms with foresight (not)? Everything downstream hinges on it, and it's answerable offline this
week with no capital at risk.
