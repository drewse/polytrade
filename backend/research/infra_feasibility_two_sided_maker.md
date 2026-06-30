# Infrastructure Feasibility — Competing for the Two-Sided Maker Edge

**Context:** Phase 0a showed the two-sided maker edge is real but **execution-speed-gated** —
positive only if you can cancel stale quotes before adverse fills land. This report asks what
it would take to actually compete, and whether it's worth it for a small account.

**Bottom line:** The required reaction latency is **~250 ms** (a documented, venue-gifted
budget), and it is **technically reachable** with a competent low-latency stack (~$110–365/mo,
Dublin-hosted, websocket-driven) — but our **current Railway/REST/2-second-polling stack misses
it by ~40×**, and **the economics do not justify the rebuild for a small account.** The edge
belongs to scaled, elite operators; a marginal new entrant most likely loses after competition.
Recommend: **do not pursue as a profit venture.** Run the zero-risk latency calibration (below)
only to confirm the numbers.

*(All factual claims are sourced; CONFIRMED = official Polymarket docs / direct measurement,
ESTIMATED = third-party/inferred. Key sources listed at the end.)*

---

## 1. How to obtain / approximate the maker latency advantage

**It is not a program you register for — it is an automatic server-side rule, and it defines
the latency bar.** On selected crypto/finance up-down markets (identifiable via the `itode:true`
flag on `GET /clob-markets/{condition_id}`), Polymarket **holds a taker (marketable) order for
250 ms before matching** (CONFIRMED — docs.polymarket.com/concepts/order-lifecycle,
/trading/orders/create). On sports it's 1 s. A *resting maker quote is not marketable, so it can
still be cancelled normally* during that window — that's the maker's protection.

**Watch the trend, though — the venue is actively closing this loophole.** The older **500 ms**
bump (the "~500 ms maker advantage" in stale writeups) was **removed Feb 18 2026**; the current
**250 ms** delay was further tightened **June 5 2026** so a taker order, once in the delay window,
is **locked and uncancellable** — explicitly to "reduce HFT gaming." So the window you'd be
building to exploit has already been halved and hardened twice in 2026. You would be engineering
against a **hostile, shrinking target the operator is deliberately narrowing.** (CONFIRMED — X
@PolymarketDevs Jun 5 2026; protos.com; theblockbeats.)

The practical effect: when a taker comes to hit your resting quote, you get a **~250 ms window**
to cancel/reprice that quote before the match commits. That is the "maker advantage" — it's
**granted to every resting order automatically**, no KYC or whitelist needed. **Exploiting it
requires a detect→decide→cancel loop faster than ~250 ms.** That single number is the
engineering target.

The *latency* play on top of it is physical proximity: the CLOB matching engine runs in **AWS
eu-west-2 (London)** (CONFIRMED — docs.polymarket.com/developers/CLOB/introduction), with
official **co-location offered in eu-west-2 to KYC/KYB-verified users**. London (UK) is itself
geoblocked, so the non-KYC fallback is **eu-west-1 (Dublin), <~8 ms away**.

## 2. Maker programs / rewards / lower-latency order paths

- **Maker rebate (automatic):** makers pay 0 fees and earn **20% of the crypto taker fee**
  (`size·0.07·p·(1−p)`); no enrollment. (CONFIRMED — docs.polymarket.com/trading/fees.) Phase 0a
  already includes this; it's ~10–15× too small alone.
- **Liquidity-reward pools:** vary per market; the candle markets **do not appear to carry
  meaningful classic LP pools** (only the rebate). (ESTIMATED.)
- **Lower-latency data path = the WebSocket**, not REST: `wss://ws-subscriptions-clob.polymarket.com/ws/market`
  (book / price_change / trades) and `…/ws/user` (your fills/order status). The Python
  `py-clob-client` is **REST-only — no native WS**; you wire the socket yourself. (CONFIRMED —
  docs.polymarket.com/market-data/websocket/*.)
- **Auth/throughput levers:** **L2 HMAC-SHA256 auth** for repeated requests (avoids re-signing
  overhead); **batch cancel up to 1,000 order IDs** in one `DELETE /orders`; **batch post up to
  15**; `cancel_all` / `cancel_market_orders`. Rate limits are generous (≈5,000 `/order` per 10 s
  burst). **CLOB V2 SDK is mandatory** (V1 archived/rejected) — *we already use v2.* (CONFIRMED.)
- **Co-location** in eu-west-2 via KYC/KYB is the only "pro path," and even one documented
  practitioner judged it a marginal micro-optimization over a Dublin VPS. (ESTIMATED.)

## 3. What latency is realistically required for profitable cancellation

Two independent anchors agree on **~250 ms** as the practical threshold:
- **Venue rule:** the 250 ms taker-hold *is* your reaction budget — beat it and you pull stale
  quotes before they're hit. (CONFIRMED.)
- **Our market-speed data** (115–119 real markets/asset): a 1-tick move takes **<1 s on BTC**
  (median 5, up to 119 trades/active-second), **~1 s on ETH/SOL**, **~2 s on XRP**. So 250 ms is
  ample for the thin alts and tight-but-workable for BTC. Phase 0a's perfect-cancellation ceiling
  (the upper bound at ~0 ms) was **+$0.21 BTC / +$1.19 ETH / +$1.33 SOL / +$1.42 XRP per market**;
  a real ~50–250 ms loop captures a large fraction of that, the venue p99 tail (below) the rest.

**Reachable loop budget (Dublin, websocket-driven):** WS event detect **~15 ms** (measured p50)
+ decision **~1–5 ms** + cancel round-trip **~25 ms** (measured warm p50) ≈ **~50 ms** — well
under 250 ms *in principle*. **Two big caveats:** (1) the venue has a **Cloudflare-fronted p99
tail of 250–650 ms** you cannot co-locate past, plus occasional **>500–700 ms POST spikes** — a
fraction of fills slip through regardless. (2) **The Python `py-clob-client-v2` reportedly takes
~1 SECOND per order to sign/submit** (NautilusTrader docs) — ~4× over the entire budget. EIP-712
ECDSA signing is intrinsically sub-ms, so this is SDK overhead, but it means **our Python path is
disqualifying on its own**; a serious build needs a **native/Rust signer** (e.g. the NautilusTrader
Rust adapter or a custom secp256k1 path). (Measured/CONFIRMED — TradoxVPS Dublin benchmark,
NautilusTrader Polymarket docs.)

## 4. Is sub-second cancel/replace achievable from our current stack? — **No, not as-is.**

| Stage | Our current stack | Required |
|---|---|---|
| Detection | **REST polling every 2,000 ms** (live-maker reconcile loop) | event-driven WS, ~15 ms |
| Region | **Railway default US-West (GCP)** → ~130–150 ms RTT to London | Dublin eu-west-1, ~25 ms |
| Order submit (measured) | ~154 ms; occasional 600 ms cold | ~25 ms warm |
| Cancel call (measured) | ~30 ms *(local return; true venue-confirmed cancel unmeasured)* | <~50 ms confirmed |
| **End-to-end detect→cancel** | **~2,150 ms+** (poll-bound) | **<250 ms** |

We miss the threshold by **~8× on detection alone** and add ~130 ms of avoidable region RTT. The
fast cancel-call number (~30 ms) is almost certainly the local SDK return, not a venue-confirmed
cancel — **this is exactly what the calibration test must measure.** Nothing about our current
architecture (poll-based, US-West, REST) is on the right path; it would need replacing, not tuning.

## 5. Architecture that would be required

| Component | Requirement |
|---|---|
| **Hosting** | VPS/EC2 in **eu-west-1 (Dublin)** (or eu-west-2 co-lo w/ KYC). Single fast core > many cores (hot loop is single-threaded). c7g.large or a Hetzner box. NOT Railway US-West. |
| **Event feed** | Persistent **WS** to `/ws/market` (book/price/trades) + `/ws/user` (fills/order status). **Data-inactivity watchdog (~120 s)** — documented silent-freeze bug where the socket stays "healthy" but stops sending. REST `/midpoint` spot-checks. |
| **Local order book** | In-memory book per quoted market, updated from WS `book`/`price_change` deltas; reconcile against `/book` snapshots. This is the detection substrate for adverse moves. |
| **Cancel/replace loop** | Event-driven (not timed): on adverse mid move toward a resting quote, fire `DELETE /orders` (batch up to 1,000) within the 250 ms budget; re-quote via `POST /orders` (≤15). No atomic amend exists — it's cancel-then-place. |
| **Risk engine** | Inventory caps (per-side + net), max concurrent exposure, per-window/day loss limits, global kill switch, and the venue's **10 s heartbeat auto-cancel** as a dead-man's switch (orders auto-cancel if no heartbeat). |
| **Wallet / signing** | **CLOB V2 SDK** (mandatory), **L2 HMAC auth** for the HTTP request (but L2 does **not** remove per-order EIP-712 signing). **The Python SDK's ~1 s/order signing is disqualifying for a hot loop — needs a native/Rust signer.** Proxy-wallet (Gnosis-safe sig type 2, as we use); note sig type 3 / POLY_1271 is now the default for *new* accounts and breaks older SDKs. |
| **CLOB client limits to design around** | py-clob-client is REST-only (add WS yourself); 15-order post batch / 1,000-ID cancel batch; **`cancel-all` is tightly limited (25/s burst) — loop on targeted `DELETE /order` / `cancel-market-orders` instead**; 10-s rate-limit windows (token-bucket); Cloudflare throttles rather than 429s; WS PING every 10 s or dropped; V1 silently rejected (use v2). |

This is essentially **a from-scratch low-latency MM service**, separate from the current
paper/research stack — not an extension of the existing executor.

## 6. Engineering complexity — **Large (multi-week)**

Not exotic (no FPGA/kernel-bypass; Python at ~50 ms is "fast enough" per practitioners), but a
real production build: WS book maintenance + reconnection/watchdog, event-driven quoting/cancel
engine, inventory/risk engine, V2 signing + L2 auth + pooled HTTP, monitoring, and **careful ops**
(WS freezes, p99 spikes, partial fills, reconciliation, capital at risk in real time). Realistic:
**several focused weeks to a robust MVP**, plus ongoing operational burden. T-shirt: **L.**

## 7. Estimated monthly infrastructure cost

| Tier | Cost/mo | Note |
|---|---|---|
| **Minimal hobby** | **~$5–15** | Hetzner/small VPS + free Polygon RPC (Alchemy 30M CU) + self-hosted Grafana. *But cheap ≠ low-latency unless region-pinned to Dublin.* |
| **Serious low-latency** | **~$110–365** | c7g.large/c6i.large in eu-west-1 (~$53–62) + paid RPC (QuickNode $49 / dedicated $40–100) + transfer/overhead. |
| Co-lo (eu-west-2, KYC) | + premium | Marginal gain over Dublin per the one documented practitioner. |

The dominant latency lever is **region (Dublin), not money** — co-locating saves ~130 ms vs our
US-West Railway, far more than any instance upgrade. Infra cost is **not** the obstacle.

## 8. Is this realistically worth pursuing for a small account? — **No.**

**Feasibility: yes.** The 250 ms bar is reachable with a ~$110–365/mo Dublin websocket bot; it's
competent engineering, not frontier HFT.

**Worth it: no, for us, at small size.** Honest economics:
- **Absolute upside is small.** The Phase-0a *ceiling* is +$0.2–1.4 per market at 5-share
  ($2.50) size. A small account can only safely quote a few markets concurrently with limited
  capital → order-of-magnitude **$10s/day gross at the optimistic ceiling**, before the realistic
  discounts below.
- **The ceiling is the *winner's* outcome, not a new entrant's.** On-chain studies: **~0.55% of
  profitable maker wallets capture ~50% of all maker profit; ~84% of traders lose; bots are
  55–62% of volume** (6,000+ bot addresses, 56M trades). You'd be the marginal entrant competing
  with w1 (+$272k, 614 trades/day) and thousands of bots for the *benign* fills and queue
  position. New entrants most plausibly land **near breakeven-to-negative** after competition,
  not at the ceiling.
- **The venue tail caps the edge.** p99 250–650 ms Cloudflare latency + >500 ms POST spikes mean
  a fraction of adverse fills are unavoidable regardless of your stack — eating into a thin edge.
- **Effort/risk mismatch.** Multi-week build (now including a **native/Rust signer** to dodge the
  Python SDK's ~1 s/order signing) + ongoing real-money ops (WS freezes, reconciliation, live
  capital) for $10s/day expected value, with material risk of being net-negative, is a poor trade
  for a small account. It only makes sense at **scale** (large capital, many concurrent markets,
  elite execution, full-time ops) — precisely the cohort already capturing the edge.
- **The operator is actively closing the loophole.** In 2026 alone Polymarket **removed the 500 ms
  maker window (Feb) and locked taker orders mid-delay (Jun) to "reduce HFT gaming."** You'd invest
  weeks to compete for an edge the venue is deliberately and repeatedly narrowing — your build
  could be obsoleted by the next rule tweak. This is a structural reason to stay out, independent
  of the latency math.

**Recommendation:** Do **not** build the low-latency maker as a profit venture. Bank the research
conclusion: *the edge is real, structural, and speed-gated; capturing it economically requires
scale we don't have.* If you ever want to revisit, the gate is the calibration test below — and
even passing it, §8's economics, not the technology, are the binding constraint.

---

## Zero-risk latency calibration test (proposed — no trading)

**Goal:** measure our **real** detect→decide→cancel latency and compare it to the 250 ms
threshold, before committing any engineering. **No resting quotes that can fill** (so zero market
risk).

**Design (shadow, read-mostly):**
1. **Subscribe** to the CLOB market WS (`/ws/market`) for one BTC + one XRP 5m market; maintain a
   local mid. Pure read — no orders. Log per-event **WS-receive timestamp** vs the trade's own
   timestamp to measure **feed latency**.
2. **Decision latency:** on each adverse mid move past a hypothetical resting quote, run the full
   quoting/cancel decision logic and timestamp it. No order is sent — measure compute only.
3. **Cancel round-trip (the one real-order part, still zero-risk):** post **one tiny far-from-mid
   maker order that cannot fill** (e.g. bid at 0.02), then immediately `DELETE` it, timestamping
   submit→ack and cancel→venue-confirmed (via the `/ws/user` order-status message, not the local
   return). Repeat to get **p50/p95/p99 true cancel latency**. Far-from-touch ⇒ no fill risk;
   capital at risk ≈ one $0.10 order that we cancel.
4. **Run it from two locations:** our current **Railway (US-West)** and a throwaway **Dublin VPS
   (~$5)**, to quantify the region penalty empirically (resolves the eu-west-2 question for *our*
   path rather than trusting vendor blogs).

**Decision rule:**
- If even a **Dublin** stack can't get end-to-end detect→cancel **under ~250 ms p95** → the edge
  is unreachable for us; stop.
- If Dublin clears 250 ms (likely, ~50–100 ms expected) but **Railway/US-West does not** (almost
  certain) → confirms the rebuild *could* reach the bar, but **§8's economics still say don't** —
  so this becomes a documented "feasible-but-not-worth-it" close-out, not a green light.

This is implementable in the existing research harness (WS listener + a single self-cancelling
probe order), needs no executor change, and risks effectively nothing. **Awaiting your approval
before building even this** — per your instruction, nothing live yet.

---

### Key sources
CONFIRMED (official): 250 ms taker hold & order lifecycle (docs.polymarket.com/concepts/order-lifecycle,
/trading/orders/create); CLOB region eu-west-2/eu-west-1 + co-lo (docs…/developers/CLOB/introduction);
maker 0-fee + 20% rebate (docs…/trading/fees); WS channels (docs…/market-data/websocket/*); batch
limits & rate limits (docs…/api-reference/*); V2-mandatory (changelog). MEASURED: Dublin warm HTTP
p50 23 ms / p95 31 ms / p99 67 ms, WS p50 14 ms, venue p99 250–650 ms, >500 ms POST spikes
(tradoxvps.com benchmark). Our own: submit ~154 ms, poll 2 s (live-maker telemetry). ESTIMATED:
AWS provider (region naming + job reqs), cross-region RTT (~75 ms NY↔London, ~130 ms Railway
US-West↔London), candle LP-pool absence, cost ranges (Vantage/Hetzner/QuickNode pricing).
