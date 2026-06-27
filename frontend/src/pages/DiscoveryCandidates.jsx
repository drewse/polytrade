import { useEffect, useMemo, useState } from 'react'
import { api, fmt } from '../api'
import { Loading, Empty, WalletLink } from '../components/common.jsx'

const num = (n, d = 2) => (n == null ? '—' : Number(n).toFixed(d))
const pct = (n) => (n == null ? '—' : `${(Number(n) * 100).toFixed(1)}%`)

const SOURCE_LABEL = {
  profit_leaderboard: 'Profit LB', volume_leaderboard: 'Volume LB',
  top_holders: 'Top Holders', recent_trades: 'Recent',
}

const FILTERS = [
  { key: 'all', label: 'All' },
  { key: 'leaderboard', label: 'Leaderboard' },
  { key: 'holders', label: 'Top Holders' },
  { key: 'recent', label: 'Recent Trades' },
  { key: 'needs_backfill', label: 'Needs Backfill' },
  { key: 'backfilled', label: 'Already Backfilled' },
]
const SORTS = [
  { key: 'discovery_score', label: 'Discovery Score' },
  { key: 'backfill_priority', label: 'Backfill Priority' },
  { key: 'source_rank', label: 'Source Rank' },
  { key: 'first_seen', label: 'First Seen' },
  { key: 'last_seen', label: 'Last Seen' },
  { key: 'roi', label: 'ROI' },
  { key: 'profit_factor', label: 'Profit Factor' },
]

function matches(c, f) {
  switch (f) {
    case 'leaderboard': return c.discovery_sources.some((s) => s.includes('leaderboard'))
    case 'holders': return c.discovery_sources.includes('top_holders')
    case 'recent': return c.discovery_sources.includes('recent_trades')
    case 'needs_backfill': return c.needs_backfill
    case 'backfilled': return !c.needs_backfill
    default: return true
  }
}

// Pure table — exported for testing (no data fetch).
export function DiscoveryCandidatesTable({ candidates }) {
  const [filter, setFilter] = useState('all')
  const [sort, setSort] = useState('backfill_priority')

  const rows = useMemo(() => {
    let r = (candidates || []).filter((c) => matches(c, filter))
    const dateKeys = new Set(['first_seen', 'last_seen'])
    r = [...r].sort((a, b) => {
      if (sort === 'source_rank') return (a.source_rank ?? 1e9) - (b.source_rank ?? 1e9)  // lower rank first
      if (dateKeys.has(sort)) return new Date(b[sort] || 0) - new Date(a[sort] || 0)       // newest first
      return (b[sort] ?? -Infinity) - (a[sort] ?? -Infinity)
    })
    return r
  }, [candidates, filter, sort])

  return (
    <div>
      <div className="promo-controls">
        <div className="promo-filters" role="group" aria-label="source filter">
          {FILTERS.map((f) => (
            <button key={f.key} className={`chip ${filter === f.key ? 'active' : ''}`}
              onClick={() => setFilter(f.key)}>{f.label}</button>
          ))}
        </div>
        <label className="muted small">Sort&nbsp;
          <select value={sort} onChange={(e) => setSort(e.target.value)} aria-label="sort by">
            {SORTS.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
          </select>
        </label>
      </div>

      {!rows.length ? (
        <Empty>No discovery candidates match.</Empty>
      ) : (
        <div className="table-wrap">
          <table data-testid="discovery-table">
            <thead>
              <tr>
                <th>Wallet</th><th className="right">Disc Score</th><th>Sources</th>
                <th className="right">Rank</th><th>Detail</th><th className="right">Backfill Pri</th>
                <th>Backfill</th><th className="right">ROI</th><th className="right">PF</th>
                <th>Production</th><th>Reason</th><th>First Seen</th><th>Last Seen</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((c) => (
                <tr key={c.wallet} data-testid="discovery-row">
                  <td className="mono"><WalletLink address={c.wallet} /></td>
                  <td className="right"><b>{num(c.discovery_score, 1)}</b></td>
                  <td className="small">{c.discovery_sources.map((s) => SOURCE_LABEL[s] || s).join(', ')}</td>
                  <td className="right">{c.source_rank ?? '—'}</td>
                  <td className="small" title={c.source_details.join(', ')}>{c.source_details.slice(0, 2).join(', ')}</td>
                  <td className="right">{c.backfill_priority}</td>
                  <td>{c.needs_backfill
                    ? <span className="badge insufficient_data">needs backfill</span>
                    : <span className="badge yes">backfilled</span>}</td>
                  <td className={`right ${c.roi > 0 ? 'pos' : c.roi < 0 ? 'neg' : ''}`}>{c.roi == null ? '—' : pct(c.roi)}</td>
                  <td className="right">{num(c.profit_factor, 2)}</td>
                  <td>{c.production_eligible
                    ? <span className="badge yes">eligible</span>
                    : <span className="badge neutral">no</span>}</td>
                  <td className="small" title={c.reason_not_eligible}>{c.reason_not_eligible}</td>
                  <td className="small">{fmt.ago(c.first_seen)}</td>
                  <td className="small">{fmt.ago(c.last_seen)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

export default function DiscoveryCandidates() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)

  const load = () => api.liveDiscoveryCandidates(300)
    .then((d) => { setData(d?.detail || d); setError(null) })
    .catch((e) => setError(e.message))
    .finally(() => setLoading(false))

  useEffect(() => { load() }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const refresh = async () => {
    setBusy(true)
    try {
      const r = await api.liveDiscoveryRefresh()
      const d = r?.detail || {}
      setMsg(`Discovered ${d.discovered ?? 0} (${d.new_discovery_rows ?? 0} new, ${d.needs_backfill ?? 0} need backfill) — ${d.note || ''}`)
      await load()
    } catch (e) { setMsg(e.message) } finally { setBusy(false) }
  }

  if (loading) return <Loading />
  if (error) return <Empty>Discovery candidates unavailable: {error}</Empty>
  const sm = data?.summary || {}
  const bs = sm.by_source || {}

  return (
    <div className="panel">
      <div className="page-head" style={{ marginBottom: 8 }}>
        <h2 style={{ margin: 0 }}>Discovery Candidates — leaderboard & top-holder sourcing</h2>
        <button onClick={refresh} disabled={busy}>{busy ? 'Fetching…' : '↻ Refresh sources'}</button>
      </div>
      <p className="muted small">
        {sm.total ?? 0} discovered · {sm.needs_backfill ?? 0} need backfill · {sm.already_backfilled ?? 0} backfilled ·
        {sm.production_eligible ?? 0} production eligible · sources: {bs.profit_leaderboard ?? 0} profit-LB,
        {bs.volume_leaderboard ?? 0} volume-LB, {bs.top_holders ?? 0} holders, {bs.recent_trades ?? 0} recent.
        Discovering a wallet never makes it tradable — normal backfill + ranking + eligibility still apply.
      </p>
      {msg && <div className="diag-strip">{msg}</div>}
      <DiscoveryCandidatesTable candidates={data?.candidates || []} />
    </div>
  )
}
