// Thin API client.
//
// Base URL resolution:
//   - Production (Vercel): set VITE_API_BASE_URL to the deployed backend origin
//     (e.g. https://polytrade-api.onrender.com). Requests become absolute and
//     hit that backend directly (CORS must allow the Vercel domain).
//   - Local dev: leave VITE_API_BASE_URL unset. BASE falls back to '' so requests
//     stay same-origin (relative /api/...) and Vite's dev proxy forwards them to
//     the FastAPI backend on http://127.0.0.1:8000 (see vite.config.js).
function normalizeBase(raw) {
  if (!raw) return ''
  // A value without a scheme (e.g. "api.example.com") would be treated as a
  // relative path and glued onto the current origin, so requests would hit the
  // frontend instead of the backend. Default missing schemes to https://.
  const withScheme = /^https?:\/\//i.test(raw) ? raw : `https://${raw}`
  return withScheme.replace(/\/+$/, '') // drop trailing slash(es)
}
const BASE = normalizeBase(import.meta.env.VITE_API_BASE_URL)

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    let detail
    try {
      detail = (await res.json()).detail
    } catch {
      // HTTP/2 responses carry no reason phrase, so res.statusText is often ''.
      detail = res.statusText || null
    }
    // Always throw a non-empty message — an empty string is falsy and would
    // slip past `if (error)` guards downstream, crashing on null data.
    const msg = typeof detail === 'string' && detail ? detail : `Request failed (${res.status})`
    throw new Error(msg)
  }
  if (res.status === 204) return null
  return res.json()
}

export const api = {
  overview: () => request('/api/overview'),
  wallets: () => request('/api/wallets'),
  addWallet: (body) => request('/api/wallets', { method: 'POST', body: JSON.stringify(body) }),
  updateWallet: (id, body) =>
    request(`/api/wallets/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  signals: () => request('/api/signals'),
  positions: (status) => request(`/api/positions${status ? `?status=${status}` : ''}`),
  closePosition: (id) => request(`/api/positions/${id}/close`, { method: 'POST' }),
  markets: () => request('/api/markets'),
  settings: () => request('/api/settings'),
  updateSettings: (body) =>
    request('/api/settings', { method: 'PATCH', body: JSON.stringify(body) }),
  runIngest: () => request('/api/ingest/run', { method: 'POST' }),
  seed: () => request('/api/mock/seed', { method: 'POST' }),
  // backtests
  runBacktest: (body) =>
    request('/api/backtests/run', { method: 'POST', body: JSON.stringify(body) }),
  backtests: () => request('/api/backtests'),
  backtest: (id) => request(`/api/backtests/${id}`),
  backtestTrades: (id, strategy) =>
    request(`/api/backtests/${id}/trades${strategy ? `?strategy=${strategy}` : ''}`),
  // attribution + signal quality
  attribution: () => request('/api/attribution/wallets'),
  signalQuality: () => request('/api/signals/quality'),
  // data-source status + live wallet backfill
  status: () => request('/api/status'),
  backfillWallet: (address, limit = 200) =>
    request('/api/wallets/backfill', { method: 'POST', body: JSON.stringify({ address, limit }) }),
  // discovery
  runDiscovery: (maxBackfill) =>
    request('/api/discovery/run', { method: 'POST', body: JSON.stringify({ max_backfill: maxBackfill ?? null }) }),
  candidates: (classification, state) => {
    const q = new URLSearchParams()
    if (classification) q.set('classification', classification)
    if (state) q.set('state', state)
    const qs = q.toString()
    return request(`/api/discovery/candidates${qs ? `?${qs}` : ''}`)
  },
  candidate: (address) => request(`/api/discovery/candidates/${address}`),
  // TOP 20 paper-strategy lab
  top20Strategies: () => request('/api/top-20/strategies'),
  top20Strategy: (id) => request(`/api/top-20/strategies/${id}`),
  top20Trades: (strategyId, limit = 100) =>
    request(`/api/top-20/trades?${strategyId ? `strategy_id=${strategyId}&` : ''}limit=${limit}`),
  top20Recompute: () => request('/api/top-20/recompute', { method: 'POST' }),
  top20Reset: () => request('/api/top-20/reset-paper', { method: 'POST' }),
  top20Leaderboard: () => request('/api/top-20/leaderboard'),
  top20Portfolio: () => request('/api/top-20/portfolio'),
  top20Explain: (signalId) => request(`/api/top-20/explain/${signalId}`),
  top20ForwardTest: () => request('/api/top-20/forward-test'),
  top20Report: () => request('/api/top-20/report'),
  top20Ensembles: () => request('/api/top-20/ensembles'),
  top20MarketIntel: () => request('/api/top-20/market-intel'),
  top20Retirement: () => request('/api/top-20/retirement'),
  top20MonteCarlo: (id) => request(`/api/top-20/montecarlo/${id}`),
  top20Optimize: (param) => request(`/api/top-20/optimize/${param}`),
  top20WalkForward: (param) => request(`/api/top-20/walk-forward/${param}`),
  top20Dataset: () => request('/api/top-20/dataset?settled_only=false&limit=50'),
  walletProfile: (address) => request(`/api/wallets/${address}/profile`),
  // historical replay + research analytics
  replayStatus: () => request('/api/replay/status'),
  replayBackfillMarkets: (pages = 3) => request(`/api/replay/backfill-markets?pages=${pages}`, { method: 'POST' }),
  replayBackfillWallets: (n = 5) => request(`/api/replay/backfill-wallets?max_wallets=${n}`, { method: 'POST' }),
  replayRun: (maxTrades = 400) => request(`/api/replay/run?max_trades=${maxTrades}`, { method: 'POST' }),
  replayReset: () => request('/api/replay/reset', { method: 'POST' }),
  researchBenchmark: () => request('/api/research/benchmark'),
  researchDrift: () => request('/api/research/drift'),
  researchRegimes: () => request('/api/research/regimes'),
  trackCandidate: (address) => request(`/api/discovery/candidates/${address}/track`, { method: 'POST' }),
  ignoreCandidate: (address) => request(`/api/discovery/candidates/${address}/ignore`, { method: 'POST' }),
}

export const COPY_CLASS = {
  elite_candidate: { label: 'elite', kind: 'sharp' },
  good_candidate: { label: 'good', kind: 'yes' },
  watchlist: { label: 'watchlist', kind: 'open' },
  ignore: { label: 'ignore', kind: 'bad' },
  insufficient_data: { label: 'insufficient', kind: 'insufficient_data' },
}

export const STRATEGIES = [
  'copy_sharp_wallets',
  'fade_losing_wallets',
  'whale_shock_reversion',
  'random_baseline',
  'no_trade_baseline',
]

// formatting helpers shared across pages
export const fmt = {
  usd: (n) =>
    (n ?? 0).toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }),
  usd2: (n) =>
    (n ?? 0).toLocaleString('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2 }),
  pct: (n) => `${(n ?? 0).toFixed(1)}%`,
  price: (n) => `${(n ?? 0).toFixed(3)}`,
  date: (s) => (s ? new Date(s).toLocaleString() : '—'),
  ago: (s) => {
    if (!s) return '—'
    const d = (Date.now() - new Date(s).getTime()) / 1000
    if (d < 60) return 'just now'
    if (d < 3600) return `${Math.floor(d / 60)}m ago`
    if (d < 86400) return `${Math.floor(d / 3600)}h ago`
    return `${Math.floor(d / 86400)}d ago`
  },
}
