import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, fmt } from '../api'
import { Loading, Empty, WalletLink } from '../components/common.jsx'

const num = (n, d = 2) => (n == null ? '—' : Number(n).toFixed(d))
const pct = (n, d = 1) => (n == null ? '—' : `${(Number(n) * 100).toFixed(d)}%`)
const usd = (n) => (n == null ? '—' : (Number(n)).toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }))
const hasWarn = (w, code) => (w.warnings || []).some((x) => x.code === code)

const FILTERS = [
  { key: 'all', label: 'All', fn: () => true },
  { key: 'neg_public', label: 'Negative public all-time', fn: (w) => (w.public?.pnl_all ?? 0) < 0 },
  { key: 'low_coverage', label: 'Low coverage', fn: (w) => w.internal?.backfill_coverage?.level === 'low' },
  { key: 'conflict', label: 'Conflicting stats', fn: (w) => hasWarn(w, 'internal_public_conflict') },
  { key: 'recent_good', label: 'Strong recent / bad lifetime', fn: (w) => hasWarn(w, 'recent_good_lifetime_bad') },
  { key: 'drawdown', label: 'High drawdown', fn: (w) => hasWarn(w, 'high_drawdown') },
  { key: 'whale', label: 'Likely market maker / whale', fn: (w) => hasWarn(w, 'likely_market_maker_whale') },
]
const SORTS = [
  { key: 'rank', label: 'Rank', val: (w) => w.rank, dir: 1 },
  { key: 'score', label: 'Production score', val: (w) => w.production_rank_score },
  { key: 'roi', label: 'Internal ROI', val: (w) => w.internal?.roi },
  { key: 'pf', label: 'Internal PF', val: (w) => w.internal?.profit_factor },
  { key: 'public_all', label: 'Public all-time P/L', val: (w) => w.public?.pnl_all },
  { key: 'r7', label: '7D P/L', val: (w) => w.rolling?.['7d']?.pnl },
  { key: 'r30', label: '30D P/L', val: (w) => w.rolling?.['30d']?.pnl },
  { key: 'settled', label: 'Settled trades', val: (w) => w.internal?.num_settled },
  { key: 'warnings', label: 'Warning count', val: (w) => w.warning_count },
]
const SEV_TONE = { high: 'bad', medium: 'warn', low: 'neutral' }

export function WarningChips({ warnings }) {
  if (!warnings?.length) return <span className="pos small">none</span>
  return (
    <span data-testid="warning-chips">
      {warnings.map((w, i) => (
        <span key={i} className={`badge ${SEV_TONE[w.severity] || 'neutral'}`} title={w.message} style={{ marginRight: 3 }}>
          {w.code.replace(/_/g, ' ')}
        </span>
      ))}
    </span>
  )
}

export function AuditTable({ rows, onSelect }) {
  if (!rows?.length) return <Empty>No audited wallets.</Empty>
  return (
    <div className="table-wrap">
      <table data-testid="audit-table">
        <thead><tr>
          <th>#</th><th>Wallet</th><th>Name</th><th className="right">Score</th>
          <th className="right">Int ROI</th><th className="right">PF</th><th className="right">Win%</th><th className="right">Settled</th>
          <th className="right">Int P/L</th><th className="right">Public all-time</th><th className="right">Pos value</th><th className="right">Preds</th>
          <th className="right">1D</th><th className="right">7D</th><th className="right">30D</th><th className="right">90D</th>
          <th>Coverage</th><th>Warnings</th>
        </tr></thead>
        <tbody>
          {rows.map((w) => (
            <tr key={w.address} data-testid="audit-row" style={{ cursor: onSelect ? 'pointer' : 'default' }} onClick={() => onSelect?.(w.address)}>
              <td>{w.rank}</td>
              <td className="mono" onClick={(e) => e.stopPropagation()}><WalletLink address={w.address} /></td>
              <td className="small">{w.display_name || '—'}</td>
              <td className="right"><b>{num(w.production_rank_score, 1)}</b></td>
              <td className="right">{pct(w.internal?.roi)}</td>
              <td className="right">{num(w.internal?.profit_factor)}</td>
              <td className="right">{pct(w.internal?.win_rate)}</td>
              <td className="right">{w.internal?.num_settled ?? '—'}</td>
              <td className={`right ${(w.internal?.realized_pnl ?? 0) >= 0 ? 'pos' : 'neg'}`}>{usd(w.internal?.realized_pnl)}</td>
              <td className={`right ${(w.public?.pnl_all ?? 0) >= 0 ? 'pos' : 'neg'}`}><b>{w.public?.pnl_all == null ? '—' : usd(w.public.pnl_all)}</b></td>
              <td className="right">{usd(w.public?.position_value)}</td>
              <td className="right">{w.public?.predictions ?? '—'}</td>
              {['1d', '7d', '30d', '90d'].map((k) => (
                <td key={k} className={`right small ${(w.rolling?.[k]?.pnl ?? 0) >= 0 ? 'pos' : 'neg'}`}>{w.rolling?.[k] ? num(w.rolling[k].pnl, 0) : '—'}</td>
              ))}
              <td><span className={`badge ${w.internal?.backfill_coverage?.level === 'low' ? 'bad' : w.internal?.backfill_coverage?.level === 'high' ? 'yes' : 'neutral'}`}>{w.internal?.backfill_coverage?.level || '—'}</span></td>
              <td><WarningChips warnings={w.warnings} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function AuditDrilldown({ data, onClose }) {
  if (!data) return null
  const i = data.internal || {}
  const p = data.public || {}
  const r = data.rolling || {}
  return (
    <div className="panel" data-testid="audit-drilldown" style={{ borderLeft: '3px solid #4ea1ff' }}>
      <div className="page-head" style={{ marginBottom: 6 }}>
        <h3 style={{ margin: 0 }}>{data.display_name || data.address.slice(0, 10)} <span className="muted small">· score {num(data.production_rank_score, 1)}</span></h3>
        <button className="secondary" onClick={onClose}>✕ Close</button>
      </div>
      <p className="mono small"><WalletLink address={data.address} /></p>
      <p className="small">{data.copy_rationale}</p>

      <h4 style={{ margin: '10px 0 4px' }}>Internal vs Public (side by side)</h4>
      <div className="table-wrap"><table data-testid="side-by-side"><thead><tr><th>Metric</th><th className="right">Internal (ours)</th><th className="right">Public (Polymarket)</th></tr></thead>
        <tbody>
          <tr><td>All-time / realized P/L</td><td className="right">{usd(i.realized_pnl)}</td><td className={`right ${(p.pnl_all ?? 0) >= 0 ? 'pos' : 'neg'}`}><b>{usd(p.pnl_all)}</b></td></tr>
          <tr><td>ROI</td><td className="right">{pct(i.roi)}</td><td className="right muted">n/a</td></tr>
          <tr><td>Profit factor</td><td className="right">{num(i.profit_factor)}</td><td className="right muted">n/a</td></tr>
          <tr><td>Volume</td><td className="right">{usd(i.volume)}</td><td className="right">{usd(p.volume_all)}</td></tr>
          <tr><td>Settled / Predictions</td><td className="right">{i.num_settled}</td><td className="right">{p.predictions ?? '—'}</td></tr>
          <tr><td>Position value</td><td className="right muted">n/a</td><td className="right">{usd(p.position_value)}</td></tr>
          <tr><td>Largest position</td><td className="right muted">n/a</td><td className="right">{usd(p.largest_position_size)}</td></tr>
          <tr><td>Coverage estimate</td><td className="right" colSpan={2}>{i.backfill_coverage?.level} (vol ratio {num(i.backfill_coverage?.volume_ratio, 4)})</td></tr>
        </tbody></table></div>

      <h4 style={{ margin: '10px 0 4px' }}>Rolling P/L (internal captured slice)</h4>
      <div className="table-wrap"><table><thead><tr><th>Window</th><th className="right">P/L</th><th className="right">ROI</th><th className="right">PF</th><th className="right">Trades</th></tr></thead>
        <tbody>{['1d', '7d', '30d', '90d'].map((k) => (
          <tr key={k}><td>{k}</td><td className={`right ${(r[k]?.pnl ?? 0) >= 0 ? 'pos' : 'neg'}`}>{usd(r[k]?.pnl)}</td><td className="right">{pct(r[k]?.roi)}</td><td className="right">{num(r[k]?.pf)}</td><td className="right">{r[k]?.trades ?? 0}</td></tr>
        ))}</tbody></table></div>
      {(p.pnl_1d != null || p.pnl_30d != null) && (
        <p className="small muted">Public rolling P/L — 1D {usd(p.pnl_1d)} · 7D {usd(p.pnl_7d)} · 30D {usd(p.pnl_30d)}</p>
      )}

      <h4 style={{ margin: '10px 0 4px' }}>Ranking score breakdown</h4>
      <div className="risk-grid">
        {Object.entries(data.score_breakdown?.components || {}).map(([k, c]) => (
          <div key={k} className="risk-cell"><span>{k.replace(/_/g, ' ')} ({pct(c.weight, 0)})</span><b>{num(c.points, 1)} pts</b></div>
        ))}
        <div className="risk-cell"><span>total</span><b>{num(data.score_breakdown?.total, 1)}</b></div>
      </div>

      <h4 style={{ margin: '10px 0 4px' }}>Eligibility rules</h4>
      {(data.eligibility_rules || []).map((rl, idx) => (
        <div key={idx} className="small"><span className={rl.pass ? 'pos' : 'neg'}>{rl.pass ? '✓' : '✗'}</span> {rl.rule} <span className="muted">({rl.detail})</span></div>
      ))}

      {(data.largest_losses?.length > 0) && (
        <>
          <h4 style={{ margin: '10px 0 4px' }}>Largest internal wins / losses</h4>
          <p className="small">Wins: {(data.largest_wins || []).map((x) => `${usd(x.pnl)}`).join(', ') || '—'}</p>
          <p className="small">Losses: {(data.largest_losses || []).map((x) => `${usd(x.pnl)}`).join(', ') || '—'}</p>
        </>
      )}
      {p.top_positions?.length > 0 && (
        <p className="small muted">Public top positions: {p.top_positions.slice(0, 4).map((t) => `${(t.title || '').slice(0, 22)} (${usd(t.size)}, P/L ${usd(t.cashPnl)})`).join(' · ')}</p>
      )}

      <h4 style={{ margin: '10px 0 4px' }}>Warnings</h4>
      {!data.warnings?.length ? <span className="pos small">none</span> : data.warnings.map((w, idx) => (
        <div key={idx} className="small"><span className={`badge ${SEV_TONE[w.severity]}`}>{w.severity}</span> {w.message}</div>
      ))}
    </div>
  )
}

export default function TopWalletsAudit() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)
  const [filter, setFilter] = useState('all')
  const [sort, setSort] = useState('rank')
  const [drill, setDrill] = useState(null)

  const load = useCallback((refreshPublic = false) => {
    if (refreshPublic) setBusy(true); else setLoading(true)
    return api.liveTopWalletsAudit(refreshPublic)
      .then((r) => { setData(r?.detail || r); setMsg(null) })
      .catch((e) => setMsg(e.message))
      .finally(() => { setLoading(false); setBusy(false) })
  }, [])
  useEffect(() => { load(false) }, [load])

  const openWallet = (addr) => { setDrill('loading'); api.liveWalletAuditDetail(addr).then((r) => setDrill(r?.detail || r)).catch((e) => { setMsg(e.message); setDrill(null) }) }

  const rows = useMemo(() => {
    const f = FILTERS.find((x) => x.key === filter) || FILTERS[0]
    const s = SORTS.find((x) => x.key === sort) || SORTS[0]
    const list = (data?.wallets || []).filter(f.fn)
    return [...list].sort((a, b) => {
      const av = s.val(a), bv = s.val(b)
      if (s.dir === 1) return (av ?? 1e9) - (bv ?? 1e9)            // rank ascending
      return (bv ?? -1e9) - (av ?? -1e9)                          // everything else descending
    })
  }, [data, filter, sort])

  if (loading) return <Loading />

  return (
    <div>
      <div className="page-head" style={{ marginBottom: 8 }}>
        <div>
          <h2 style={{ margin: 0 }}>Top 20 Audit <span className="badge sharp">read-only</span></h2>
          <p className="muted small" style={{ margin: '2px 0 0' }}>
            Internal ranking vs PUBLIC Polymarket lifetime stats for the {data?.top_n ?? 20} wallets the executor may copy.
            Public stats never alter ranking. {data?.public_refresh ? `Refreshed ${data.public_refresh.fetched}.` : ''}
          </p>
        </div>
        <button data-testid="refresh-public" onClick={() => load(true)} disabled={busy}>
          {busy ? 'Fetching public…' : '⟳ Refresh public stats'}
        </button>
      </div>
      {msg && <div className="diag-strip neg">{msg}</div>}
      <div className="diag-strip">{data?.safety}</div>

      <div className="promo-controls">
        <div className="promo-filters" role="group" aria-label="audit filter">
          {FILTERS.map((f) => (
            <button key={f.key} className={`chip ${filter === f.key ? 'active' : ''}`} onClick={() => setFilter(f.key)}>{f.label}</button>
          ))}
        </div>
        <label className="muted small">Sort&nbsp;
          <select value={sort} onChange={(e) => setSort(e.target.value)} aria-label="sort by">
            {SORTS.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
          </select>
        </label>
      </div>

      {drill && drill !== 'loading' && <div style={{ margin: '12px 0' }}><AuditDrilldown data={drill} onClose={() => setDrill(null)} /></div>}
      {drill === 'loading' && <Loading />}

      <p className="muted small">{rows.length} of {data?.wallets?.length ?? 0} wallets · click a row for the full drilldown.</p>
      <AuditTable rows={rows} onSelect={openWallet} />
    </div>
  )
}
