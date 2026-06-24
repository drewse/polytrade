# Polymarket Copy Lab — paper trading dashboard

Track Polymarket wallets, rank them by historical profitability, detect when
high-ranked wallets take new positions, **paper-copy** those trades, and measure
performance — so you can answer: *"Would copying these wallets have made money?"*

> ⚠️ **Paper trading only.** This project never places real orders, holds no
> private keys, and requires no paid APIs or wallet connection. It simulates
> copied trades against public/mock market data and marks them to market.

---

## What's inside

```
polymarket-copy-lab/
├── backend/                 # Python 3.12 · FastAPI · SQLAlchemy · SQLite
│   ├── app/
│   │   ├── main.py              # FastAPI app + all /api routes
│   │   ├── worker.py            # background polling worker (python -m app.worker)
│   │   ├── db.py                # engine / session + additive auto-migrate
│   │   ├── models.py            # SQLAlchemy tables
│   │   ├── schemas.py           # Pydantic request/response models
│   │   ├── settings.py          # config + default runtime settings
│   │   ├── polymarket_client.py # LIVE public-API client (interface + TODOs)
│   │   ├── mock_provider.py     # deterministic fake-data world (archetypes, regimes…)
│   │   ├── scoring.py           # 0–100 wallet score + classification
│   │   ├── signals.py           # copy-trade signal detection + edge estimate
│   │   ├── paper_trading.py     # slippage / sizing / mark-to-market math
│   │   ├── backtest.py          # historical replay engine (5 strategies)
│   │   ├── attribution.py       # per-wallet PnL/ROI attribution
│   │   ├── signal_quality.py    # post-signal price-move tracking
│   │   └── services.py          # orchestration glue (DB writes live here)
│   └── tests/                   # pytest: scoring, signals, sizing, slippage,
│                                #   exposure caps, backtest replay, attribution
└── frontend/                # React + Vite dashboard
    └── src/
        ├── App.jsx · api.js · styles.css
        ├── components/common.jsx   # tables, badges, multi-line charts
        └── pages/  Overview · Wallets · Signals · Positions · Markets · Backtests · Settings
```

---

## Quick start (mock mode — works fully offline)

You need **Python 3.12** and **Node 18+**. Two terminals.

### 1. Backend API

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# start the API (creates the SQLite DB + default settings on first boot)
uvicorn app.main:app --reload --port 8000
```

Seed the mock world (30 wallets, 100 markets, 5,000 historical + 50 recent
trades, signals, and open/closed positions):

```bash
curl -X POST http://127.0.0.1:8000/api/mock/seed
```

### 2. Background worker (optional but recommended)

In a second terminal — keeps generating new trades, signals, positions, and
equity snapshots on an interval. It auto-seeds if the DB is empty.

```bash
cd backend
source .venv/bin/activate
python -m app.worker
```

### 3. Frontend dashboard

```bash
cd frontend
npm install
npm run dev          # opens http://localhost:5173 (proxies /api to :8000)
```

Open **http://localhost:5173** → you'll land on the Overview page. Click
**Run ingest** a few times (or let the worker run) to watch positions resolve
and the equity curve move.

---

## How it works

1. **Ingestion** pulls markets + recent trades from a `DataProvider` (mock or
   live) and upserts them.
2. **Scoring** aggregates each wallet's trade history into a 0–100 score
   (realized ROI, win rate, consistency, recency, sample size — with Bayesian
   shrinkage so tiny samples can't look elite) and a class:
   `sharp` / `neutral` / `bad` / `insufficient_data`.
3. **Signal detection** emits a copy signal when a new trade clears every gate
   (wallet score, trade count, trade size, market liquidity, price freshness).
   Confidence rises when multiple sharp wallets pile into the same outcome.
4. **Paper simulator** opens a position for qualifying signals: position size =
   `max_position_pct`% of bankroll, capped by per-market exposure, filled with
   simulated slippage. Positions mark-to-market each cycle and auto-close when
   their market resolves (manual close also available).
5. **Dashboard** shows bankroll, PnL, ROI, win rate, wallets, signals,
   positions, markets, and editable settings.

In mock mode, markets resolve gradually in favor of the score-weighted
consensus of the sharp wallets ~62% of the time — which is what lets the
dashboard demonstrate that copying the sharp cohort is profitable.

---

## API endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/api/overview` | bankroll, PnL, ROI, win rate, top wallets, equity curve |
| GET  | `/api/wallets` | tracked wallets + stats |
| POST | `/api/wallets` | start tracking a wallet `{address, label?}` |
| PATCH| `/api/wallets/{id}` | enable/disable copying, relabel |
| GET  | `/api/signals` | recent copy-trade signals |
| GET  | `/api/positions?status=open\|closed` | paper positions |
| POST | `/api/positions/{id}/close` | manually close a position |
| GET  | `/api/markets?category=` | tracked markets |
| GET  | `/api/settings` | strategy/risk settings |
| PATCH| `/api/settings` | update settings |
| POST | `/api/ingest/run` | run one ingest cycle now |
| POST | `/api/mock/seed` | (re)seed the mock world |
| POST | `/api/backtests/run` | run a backtest `{name, train_fraction, category?, min_wallet_score?, strategies?}` |
| GET  | `/api/backtests` | list past backtests |
| GET  | `/api/backtests/{id}` | one backtest + per-strategy results |
| GET  | `/api/backtests/{id}/trades?strategy=` | individual simulated trades |
| GET  | `/api/attribution/wallets` | per-wallet copy PnL/ROI/win-rate |
| GET  | `/api/signals/quality` | signals + post-signal price moves (5m/30m/2h/close, MFE/MAE) |
| GET  | `/api/status` | data-source health (mode, ok, stale, partial-wallet count) for badges |
| POST | `/api/wallets/backfill` | pull a wallet's recent **live** history & score it `{address, limit?}` |
| POST | `/api/discovery/run` | discover + rank copy candidates `{max_backfill?}` |
| GET  | `/api/discovery/candidates?classification=&state=` | ranked candidates |
| GET  | `/api/discovery/candidates/{address}` | candidate detail (trades, categories, profit curve) |
| POST | `/api/discovery/candidates/{address}/track` | track + enable copying |
| POST | `/api/discovery/candidates/{address}/ignore` | ignore + disable copying |

Interactive docs at **http://127.0.0.1:8000/docs**.

---

## Wallet discovery & copyability

The **Discovery** tab automatically surfaces and ranks wallets worth copying.

Pipeline ([`discovery.py`](backend/app/discovery.py)):
1. **scan** recent trades → 2. **extract** unique wallets → 3. **filter** out
dust/inactive traders → 4. **backfill** the top movers (live, read-only) →
5. **score copyability** → 6. **rank** & persist as candidates you can Track/Ignore.

**Copyability** ([`copyability.py`](backend/app/copyability.py)) is scored
*separately* from raw profitability — a wallet can be profitable yet a bad copy
target. It blends ROI, win rate, trade count, average notional, recency,
win-rate **consistency over time**, market **diversity**, and category
**specialization**, then actively penalizes the ways copy strategies blow up:

- **tiny samples** → shrunk toward neutral (can't trust the edge yet)
- **too-good-to-be-true** (very high win rate on few trades) → capped + flagged
- **spoof/noise** (micro-notional spam, erratic returns, huge volume in 1–2
  markets) → forced toward *ignore*

Classifications: `elite_candidate` · `good_candidate` · `watchlist` · `ignore` ·
`insufficient_data`. The candidate detail view shows recent trades, best/worst
categories, a cumulative profit curve, copied paper PnL (if tracked), and a
weak-sample warning.

**Auto-discovery** (Settings): with `auto_discovery_enabled` on, the worker runs
discovery every `discovery_interval_minutes`, backfilling at most
`max_wallets_to_backfill_per_cycle` wallets per run.

Mock seed produces a realistic spread (≈ 5 elite / 8 good / ~14 watchlist /
~19 ignore+insufficient). In live mode, freshly-discovered wallets often score
`insufficient_data` at first because their recent trades are on **unresolved**
markets (no realized PnL yet) — backfill more history or wait for resolutions.

---

## Backtesting (the research engine)

The **Backtests** tab replays historical trades in timestamp order and compares
five strategies side by side on the same data:

| Strategy | Rule |
|----------|------|
| `copy_sharp_wallets` | copy trades from wallets classed **sharp** (score ≥ threshold) |
| `fade_losing_wallets` | take the **opposite** side of wallets classed **bad** |
| `whale_shock_reversion` | when a **whale** (≥ $6k) hits, bet **reversion** (opposite side) |
| `random_baseline` | copy a random outcome on ~15% of trades |
| `no_trade_baseline` | never trade (flat bankroll control) |

Each strategy reports: starting/ending bankroll, total PnL, ROI, max drawdown,
win rate, # trades, average trade return, best/worst trade, and an equity curve.
The dashboard draws equity-curve and drawdown charts and a per-trade table.

### What the backtest assumes (read this before trusting numbers)

1. **Walk-forward, no lookahead.** Wallet classifications come from a *training*
   window (the first `train_fraction` of history). Only later *test*-window
   trades are traded, so a wallet is judged "sharp" using only earlier data.
2. **Flat unit staking.** Every bet risks a fixed `max_position_pct` of the
   *starting* bankroll — no compounding, no leverage cap, **no ruin stop**.
   A terrible strategy can therefore print a drawdown > 100% (account goes
   negative); that's a signal, not a bug.
3. **Hold to resolution.** Entry at the trade's price (plus liquidity-scaled
   slippage); exit at resolution worth $1 (win) or $0 (lose), booked at the
   market's `resolved_at`.
4. **Only resolved markets are scored.** Trades on still-open markets have no
   known outcome and are skipped (not counted as wins/losses).
5. **Mock resolutions are skill-correlated.** In mock mode a market resolves in
   favor of the score-weighted consensus of the wallets in it ~62% of the time.
   This is *by construction* so the copy thesis is demonstrable — it is not
   evidence about the real Polymarket.

### Signal quality & attribution

* **Signal quality** (`signal_quality.py`): the worker snapshots open-market
  prices each cycle (`market_price_snapshots`) and fills in how each signal's
  price moved at +5m/+30m/+2h/close, plus max favorable/adverse excursion.
  Immediately after a seed there's no price history, so quality is *synthesized*
  once for display; real snapshot-based values overwrite it as time passes.
* **Attribution** (`attribution.py`): rolls every paper position back to its
  source wallet — realized/unrealized PnL, ROI, win rate, copied signals, and
  average copied entry price. Shown on the **Wallets** page.

---

## Tests

```bash
cd backend && source .venv/bin/activate
python -m pytest          # 32 tests: scoring, signals, sizing, slippage,
                          #   exposure caps, backtest replay, attribution
```

---

## Settings (editable in the dashboard)

| Setting | Default | Meaning |
|---------|---------|---------|
| `bankroll` | $10,000 | starting paper bankroll |
| `min_wallet_score` | 65 | only copy wallets at/above this score |
| `min_trade_count` | 20 | wallet must have ≥ this many trades |
| `min_trade_size` | $50 | ignore smaller signals |
| `max_position_pct` | 1% | per-position cap (% of bankroll) |
| `max_market_exposure_pct` | 5% | total exposure cap per market |
| `slippage_cents` | 1.5¢ | simulated fill slippage |
| `min_market_liquidity` | $1,000 | skip illiquid markets |
| `max_price_staleness_min` | 120 | ignore stale trades |
| `min_confidence` | 50 | min signal confidence to open a position |
| `min_volume` | 0 | skip low-volume markets |
| `min_edge` | 0 | min estimated edge (P(win) − price) to copy |
| `polling_interval_seconds` | 30 | worker cadence |
| `data_mode` | `mock` | `mock` or `live` |
| `max_daily_loss` | $500 | stop opening once today's realized loss exceeds this |
| `max_open_positions` | 50 | hard cap on concurrent open positions |
| `max_correlated_exposure_pct` | 15% | max open exposure per **category** |
| `cooldown_losses` | 3 | consecutive losing closes that trigger a cooldown |
| `cooldown_minutes` | 30 | pause new entries this long after the streak |
| `auto_discovery_enabled` | 0 | worker auto-discovers/backfills wallets when on |
| `discovery_interval_minutes` | 15 | min minutes between auto-discovery runs |
| `max_wallets_to_backfill_per_cycle` | 5 | cap on live backfills per discovery run |
| `min_candidate_trade_count` | 15 | below this a wallet is `insufficient_data` |
| `min_candidate_notional` | $25 | ignore dust traders below this avg notional |

---

## Live read-only mode (verified)

Set `data_mode = live` in **Settings** (or `PATCH /api/settings`). The worker
then pulls **real, public, read-only** data from Polymarket's no-auth hosts —
endpoints verified against live responses in June 2026:

| Host | Endpoint | Used for | Status |
|------|----------|----------|--------|
| `gamma-api.polymarket.com` | `GET /markets?closed=false&order=volume` | active markets, metadata, outcomes/prices, `clobTokenIds`, liquidity/volume, resolution | ✅ verified |
| `gamma-api.polymarket.com` | `GET /markets?closed=true` | resolved markets (winner = outcome priced ≈ 1.0) | ✅ verified |
| `data-api.polymarket.com` | `GET /trades?limit=&takerOnly=false` | recent trades (wallet, side, size-in-shares, price, ts) | ✅ verified |
| `data-api.polymarket.com` | `GET /trades?user=<addr>` | per-wallet activity (recent window) | ✅ verified |
| `clob.polymarket.com` | `GET /midpoint?token_id=` · `GET /price?token_id=&side=` · `GET /book?token_id=` | live mid / bid-ask / order book | ✅ verified |

Parsing is hardened with schema guards in
[`backend/app/polymarket_client.py`](backend/app/polymarket_client.py):
JSON-string-encoded arrays (`outcomes`, `outcomePrices`, `clobTokenIds`) and real
arrays are both handled; missing **optional** fields fall back to defaults; a
malformed **response** (wrong top-level type) or a record missing **required**
fields raises a clear `LiveDataError` / `LiveParseError` instead of silently
producing garbage. A real `User-Agent` is sent (the API 403s the default Python one).

Recorded JSON **fixtures** (`backend/tests/fixtures/`) captured from the real APIs
back 21 parser tests — no network needed to run the suite.

**Known live caveats / partial data**
- **Wallet history is a recent window, not full lifetime.** `data-api` returns
  recent activity, so live-derived wallet scores are flagged **PARTIAL WALLET
  HISTORY** in the UI and `partial_history=true` in the API. Use **Backfill (live)**
  on the Wallets page (`POST /api/wallets/backfill`) to pull more history per wallet.
- **Trade `size` is in shares**, so USD notional is computed as `size × price`.
- **`category` is often null** on Gamma markets (it lives on `events[0].category`).
- Fresh live wallets usually score `insufficient_data` until enough history is
  ingested/backfilled, so live signals are sparse at first — expected, not a bug.

Dashboard status badges (sidebar): **MOCK DATA**, **LIVE READ-ONLY DATA**,
**PARTIAL WALLET HISTORY**, **API ERROR**, **STALE DATA** (from `GET /api/status`).

---

## 🔒 Safety — this is research / paper trading ONLY

This project is structurally incapable of touching real money:

- **No private keys.** None are read, stored, requested, or referenced anywhere.
- **No orders are ever submitted.** There is no order-placement code path, no
  signing, no wallet connection, no approval flow. The live client only issues
  HTTP `GET` requests to public endpoints.
- **Read-only live data.** Live mode *reads* public markets/trades/prices. It
  never writes to any exchange.
- **All "trades" are simulated** in the local SQLite DB (paper positions marked
  to market). "Bankroll", "PnL", and "fills" are bookkeeping only.
- **No paid APIs, no auth, no API keys.**

If you want to be certain: grep the codebase — there is no `POST`/`order`/`sign`/
`privateKey` against any exchange. The only writes are to your local DB.

No API keys. No private keys. No paid plans. Nothing is ever traded for real.

---

## Resetting

Delete `backend/polymarket_copy_lab.db` (or POST `/api/mock/seed`) to start over.
