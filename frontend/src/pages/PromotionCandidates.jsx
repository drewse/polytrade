import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { Loading, Empty, WalletLink } from '../components/common.jsx'

const num = (n, d = 2) => (n == null ? '—' : Number(n).toFixed(d))
const pct = (n) => (n == null ? '—' : `${(Number(n) * 100).toFixed(1)}%`)
const ago = (s) => {
  if (!s) return '—'
  const d = (Date.now() - new Date(s).getTime()) / 86400000
  if (d < 1) return 'today'
  return `${Math.floor(d)}d ago`
}

const STATUS = {
  strong: { label: '⭐ Strong', kind: 'yes' },
  near: { label: '🟡 Near', kind: 'insufficient_data' },
  watch: { label: '🔵 Watch', kind: 'open' },
}

const FILTERS = [
  { key: 'all', label: 'All' },
  { key: 'strong', label: 'Strong Candidates' },
  { key: 'near', label: 'Near Candidates' },
  { key: 'watch', label: 'Watch' },
]

const SORTS = [
  { key: 'promotion_score', label: 'Promotion Score' },
  { key: 'average_edge', label: 'Avg Edge' },
  { key: 'signals_seen', label: 'Signals' },
  { key: 'roi', label: 'ROI' },
  { key: 'profit_factor', label: 'Profit Factor' },
  { key: 'settled_trades', label: 'Settled Trades' },
  { key: 'last_active', label: 'Last Active' },
]

// Pure, presentational + interactive table — exported for testing (no data fetch).
// Structured so future manual promote/demote/compare actions can hang off each row.
export function PromotionCandidatesTable({ candidates }) {
  const [filter, setFilter] = useState('all')
  const [sort, setSort] = useState('promotion_score')
  const [search, setSearch] = useState('')

  const rows = useMemo(() => {
    let r = candidates || []
    if (filter !== 'all') r = r.filter((c) => c.status === filter)
    if (search.trim()) {
      const q = search.trim().toLowerCase()
      r = r.filter((c) => (c.wallet || '').toLowerCase().includes(q))
    }
    const val = (c) => (sort === 'last_active' ? new Date(c.last_active || 0).getTime() : (c[sort] ?? -Infinity))
    return [...r].sort((a, b) => val(b) - val(a))
  }, [candidates, filter, sort, search])

  return (
    <div>
      <div className="promo-controls">
        <div className="promo-filters" role="group" aria-label="status filter">
          {FILTERS.map((f) => (
            <button key={f.key} className={`chip ${filter === f.key ? 'active' : ''}`}
              onClick={() => setFilter(f.key)}>{f.label}</button>
          ))}
        </div>
        <div className="promo-tools">
          <label className="muted small">Sort&nbsp;
            <select value={sort} onChange={(e) => setSort(e.target.value)} aria-label="sort by">
              {SORTS.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
            </select>
          </label>
          <input className="promo-search" placeholder="Search wallet…" value={search}
            aria-label="search wallet" onChange={(e) => setSearch(e.target.value)} />
        </div>
      </div>

      {!rows.length ? (
        <Empty>No promotion candidates match.</Empty>
      ) : (
        <div className="table-wrap">
          <table data-testid="promo-table">
            <thead>
              <tr>
                <th>Wallet</th><th className="right">Promotion</th><th>Status</th>
                <th className="right">Signals</th><th className="right">Avg Edge</th>
                <th className="right">Avg Conf</th><th className="right">Prod Score</th>
                <th className="right">ROI</th><th className="right">PF</th>
                <th className="right">Settled</th><th className="right">Win%</th>
                <th>Last Active</th><th>Reason Rejected</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((c) => (
                <tr key={c.wallet} data-testid="promo-row">
                  <td className="mono"><WalletLink address={c.wallet} /></td>
                  <td className="right"><b>{num(c.promotion_score, 1)}</b></td>
                  <td><span className={`badge ${STATUS[c.status]?.kind || 'neutral'}`}>{STATUS[c.status]?.label || c.status}</span></td>
                  <td className="right">{c.signals_seen}</td>
                  <td className="right">{num(c.average_edge, 3)}</td>
                  <td className="right">{num(c.average_confidence, 0)}</td>
                  <td className="right">{num(c.average_production_score, 1)}</td>
                  <td className={`right ${c.roi > 0 ? 'pos' : c.roi < 0 ? 'neg' : ''}`}>{c.roi == null ? '—' : pct(c.roi)}</td>
                  <td className="right">{num(c.profit_factor, 2)}</td>
                  <td className="right">{c.settled_trades ?? '—'}</td>
                  <td className="right">{c.win_rate == null ? '—' : pct(c.win_rate)}</td>
                  <td className="small">{ago(c.last_active)}</td>
                  <td className="small" title={c.reason_rejected}>{c.reason_rejected}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// Data-fetching wrapper used by the Live Trading tab.
export default function PromotionCandidates() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    api.livePromotionCandidates(200)
      .then((d) => { if (alive) { setData(d?.detail || d); setError(null) } })
      .catch((e) => alive && setError(e.message))
      .finally(() => alive && setLoading(false))
    return () => { alive = false }
  }, [])

  if (loading) return <Loading />
  if (error) return <Empty>Promotion candidates unavailable: {error}</Empty>
  const sm = data?.summary || {}
  return (
    <div className="panel">
      <h2>Promotion Candidates — "farm system" (read-only analytics)</h2>
      <p className="muted small" style={{ marginTop: -6 }}>
        {sm.total_candidates ?? 0} candidates · ⭐ {sm.strong ?? 0} strong · 🟡 {sm.near ?? 0} near ·
        🔵 {sm.watch ?? 0} watch · {sm.production_wallets_excluded ?? 0} production wallets excluded ·
        thresholds PF&nbsp;{data?.thresholds?.min_profit_factor} / settled&nbsp;{data?.thresholds?.min_settled}.
        Wallets not in production that look promising from real signal history — informational only,
        changes no trading behavior.
      </p>
      <PromotionCandidatesTable candidates={data?.candidates || []} />
    </div>
  )
}
