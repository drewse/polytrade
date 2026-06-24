// Thin API client. Uses same-origin /api (proxied to the backend in dev).
const BASE = import.meta.env.VITE_API_BASE || ''

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
      detail = res.statusText
    }
    throw new Error(typeof detail === 'string' ? detail : `Request failed (${res.status})`)
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
