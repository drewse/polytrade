import { useEffect, useMemo, useState } from 'react'
import { api, fmt } from '../api'
import { Loading, Empty, WalletLink } from '../components/common.jsx'

const usd = (n) => (n == null ? '—' : `${n >= 0 ? '+' : ''}${Number(n).toFixed(2)}`)
const pct = (n) => (n == null ? '—' : `${(Number(n) * 100).toFixed(1)}%`)
const num = (n, d = 2) => (n == null ? '—' : Number(n).toFixed(d))

const STATUS = {
  strong: { label: '⭐ Strong', kind: 'yes' },
  near: { label: '🟡 Near', kind: 'insufficient_data' },
  watch: { label: '🔵 Watch', kind: 'open' },
}
const FILTERS = [
  { key: 'all', label: 'All' },
  { key: 'strong', label: 'Strong' },
  { key: 'near', label: 'Near' },
  { key: 'watch', label: 'Watch' },
]
const SORTS = [
  { key: 'total_pl', label: 'P/L' },
  { key: 'return_pct', label: 'Return %' },
  { key: 'win_rate', label: 'Win Rate' },
  { key: 'max_drawdown', label: 'Drawdown' },
  { key: 'shadow_trades', label: 'Trades' },
  { key: 'promotion_score', label: 'Promotion Score' },
]

const PL = ({ v }) => <span className={v > 0 ? 'pos' : v < 0 ? 'neg' : 'muted'}>{usd(v)}</span>

// Pure, presentational + interactive table — exported for testing (no data fetch).
export function ShadowPortfolioTable({ wallets }) {
  const [filter, setFilter] = useState('all')
  const [sort, setSort] = useState('total_pl')

  const rows = useMemo(() => {
    let r = wallets || []
    if (filter !== 'all') r = r.filter((w) => w.status === filter)
    return [...r].sort((a, b) => (b[sort] ?? -Infinity) - (a[sort] ?? -Infinity))
  }, [wallets, filter, sort])

  return (
    <div>
      <div className="promo-controls">
        <div className="promo-filters" role="group" aria-label="status filter">
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
        <Empty>No shadow wallets match.</Empty>
      ) : (
        <div className="table-wrap">
          <table data-testid="shadow-table">
            <thead>
              <tr>
                <th>Wallet</th><th>Status</th><th className="right">Shadow P/L*</th>
                <th className="right">Return%*</th><th className="right">Trades</th>
                <th className="right">Win%</th><th className="right">Drawdown*</th>
                <th className="right">Avg Edge</th><th>Last Sim Trade</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((w) => (
                <tr key={w.wallet} data-testid="shadow-row">
                  <td className="mono"><WalletLink address={w.wallet} /></td>
                  <td><span className={`badge ${STATUS[w.status]?.kind || 'neutral'}`}>{STATUS[w.status]?.label || w.status}</span></td>
                  <td className="right"><b><PL v={w.total_pl} /></b></td>
                  <td className="right"><PL v={w.return_pct} /></td>
                  <td className="right">{w.shadow_trades}</td>
                  <td className="right">{w.win_rate == null ? '—' : pct(w.win_rate)}</td>
                  <td className="right neg">{num(w.max_drawdown, 2)}</td>
                  <td className="right">{num(w.avg_edge, 3)}</td>
                  <td className="small">{fmt.ago(w.last_simulated_trade)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function SumCard({ label, value, tone, sub }) {
  return <div className="card"><div className="label">{label}</div>
    <div className={`value ${tone || ''}`}>{value}</div>{sub != null && <div className="sub">{sub}</div>}</div>
}

export default function ShadowPortfolio() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    api.liveShadowPortfolio(200)
      .then((d) => { if (alive) { setData(d?.detail || d); setError(null) } })
      .catch((e) => alive && setError(e.message))
      .finally(() => alive && setLoading(false))
    return () => { alive = false }
  }, [])

  if (loading) return <Loading />
  if (error) return <Empty>Shadow portfolio unavailable: {error}</Empty>
  const all = data?.aggregates?.all_candidates || {}
  const best = data?.best_candidate
  const worst = data?.worst_candidate

  return (
    <div className="panel">
      <h2>Shadow Portfolio — simulated copies of promotion candidates</h2>
      <p className="muted small" style={{ marginTop: -6 }}>
        ⚠ <b>SIMULATED ONLY</b> — {data?.note} Each shadow copy stakes a fixed
        ${data?.stake_unit} unit at the signal's observed price; no real orders, executions, or
        positions are affected.
      </p>

      <div className="cards">
        <SumCard label="Sim total P/L*" value={<PL v={all.total_pl} />} sub={`realized ${usd(all.realized_pl)} · unreal ${usd(all.unrealized_pl)}`} />
        <SumCard label="Sim return %*" value={<PL v={all.return_pct} />} sub={`on $${all.staked} staked`} />
        <SumCard label="Sim trades" value={all.shadow_trades ?? 0} sub={`${all.open_positions ?? 0} open`} />
        <SumCard label="Win rate*" value={all.win_rate == null ? '—' : pct(all.win_rate)} />
        <SumCard label="Max drawdown*" value={num(all.max_drawdown, 2)} tone="neg" />
        <SumCard label="Best candidate*" value={best ? <WalletLink address={best.wallet} /> : '—'} sub={best ? usd(best.total_pl) : ''} />
        <SumCard label="Worst candidate*" value={worst ? <WalletLink address={worst.wallet} /> : '—'} sub={worst ? usd(worst.total_pl) : ''} />
        <SumCard label="Production baseline*" value={<PL v={data?.aggregates?.production_baseline?.total_pl} />}
          sub={`${data?.aggregates?.production_baseline?.shadow_trades ?? 0} sim trades`} />
      </div>

      <ShadowPortfolioTable wallets={data?.wallets || []} />
      <p className="muted small" style={{ marginTop: 8 }}>* simulated — analytics only, never executed.</p>
    </div>
  )
}
