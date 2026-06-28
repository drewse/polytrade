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
  // live real-money executor (monitoring + safe controls; NO enable-trading control)
  liveStatus: () => request('/api/live/status'),
  liveExecutions: (limit = 50) => request(`/api/live/executions?limit=${limit}`),
  liveDecisions: (limit = 100) => request(`/api/live/decisions?limit=${limit}`),
  liveRanking: (limit = 20) => request(`/api/live/wallet-ranking?limit=${limit}`),
  livePromotionCandidates: (limit = 200) => request(`/api/live/promotion-candidates?limit=${limit}`),
  liveShadowPortfolio: (limit = 200) => request(`/api/live/shadow-portfolio?limit=${limit}`),
  liveDiscoveryCandidates: (limit = 300) => request(`/api/live/discovery-candidates?limit=${limit}`),
  liveDiscoveryRefresh: () => request('/api/live/discovery/refresh', { method: 'POST' }),
  liveDiscoveryBackfillStatus: () => request('/api/live/discovery-backfill/status'),
  liveDiscoveryBackfillRunOnce: (batch = 5) =>
    request(`/api/live/discovery-backfill/run-once?batch=${batch}`, { method: 'POST' }),
  liveHalt: (reason = 'manual') =>
    request(`/api/live/halt?reason=${encodeURIComponent(reason)}`, { method: 'POST' }),
  livePause: () => request('/api/live/pause', { method: 'POST' }),
  liveResume: () => request('/api/live/resume', { method: 'POST' }),
  liveRunOnce: () => request('/api/live/run-once', { method: 'POST' }),
  liveReconcile: (balance) => request(`/api/live/reconcile?balance=${balance}`, { method: 'POST' }),
  liveReconcileAccount: () => request('/api/live/reconcile-account', { method: 'POST' }),
  liveSizingSimulation: (limit = 1000) => request(`/api/live/sizing-simulation?limit=${limit}`),
  liveReconcileFills: (limit = 300) => request(`/api/live/reconcile-fills?limit=${limit}`, { method: 'POST' }),
  liveReconcilerStatus: () => request('/api/live/reconciler-status'),
  // BTC 5M Reversal Lab — isolated read-only research
  btc5mDashboard: () => request('/api/btc5m/dashboard'),
  btc5mRefresh: (limitMarkets = 50) => request(`/api/btc5m/refresh?limit_markets=${limitMarkets}`, { method: 'POST' }),
  btc5mDataset: () => request('/api/btc5m/dataset'),
  btc5mWalletIq: (limit = 50) => request(`/api/btc5m/wallet-iq?limit=${limit}`),
  btc5mWalletProfiles: (limit = 200) => request(`/api/btc5m/wallet-profiles?limit=${limit}`),
  btc5mClusters: () => request('/api/btc5m/clusters'),
  btc5mStrategyLab: (scope = 'global') => request(`/api/btc5m/strategy-lab?scope=${encodeURIComponent(scope)}`),
  btc5mConsensus: () => request('/api/btc5m/consensus'),
  btc5mFeatureImportance: (scope = 'global') => request(`/api/btc5m/feature-importance?scope=${encodeURIComponent(scope)}`),
  btc5mShadow: (limit = 50) => request(`/api/btc5m/shadow?limit=${limit}`),
  btc5mModels: (scope = 'global') => request(`/api/btc5m/models?scope=${encodeURIComponent(scope)}`),
  btc5mResearchNotes: (limit = 40) => request('/api/btc5m/research-notes'),
  // Research Platform V1 — isolated paper research
  researchDashboard: () => request('/api/research/dashboard'),
  researchCycle: (limitMarkets = 120) => request(`/api/research/cycle?limit_markets=${limitMarkets}`, { method: 'POST' }),
  researchReplay: () => request('/api/research/replay', { method: 'POST' }),
  researchStrategies: (status) => request(`/api/research/strategies${status ? `?status=${encodeURIComponent(status)}` : ''}`),
  researchStrategy: (id) => request(`/api/research/strategies/${id}`),
  researchTournament: () => request('/api/research/tournament'),
  researchChampion: () => request('/api/research/champion'),
  researchHypotheses: () => request('/api/research/hypotheses'),
  researchNightlyReviews: () => request('/api/research/nightly-reviews'),
  researchExperiments: () => request('/api/research/experiments'),
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
