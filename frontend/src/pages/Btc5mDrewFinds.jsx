import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { Loading, Empty } from '../components/common.jsx'

const num = (n, d = 2) => (n == null ? '—' : Number(n).toFixed(d))
const usd = (n) => (n == null ? '—' : `${n >= 0 ? '+' : ''}$${Number(n).toLocaleString(undefined, { maximumFractionDigits: 0 })}`)
const short = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : '—')
const ago = (iso) => {
  if (!iso) return 'never'
  const s = (Date.now() - new Date(iso).getTime()) / 1000
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  return `${Math.floor(s / 3600)}h ago`
}
const pmLink = (a) => `https://polymarket.com/profile/${a}`

// Pure presentational — exported for tests.
export function TargetCard({ t }) {
  const isBtc = (t.btc_5m_pct || 0) >= 50
  return (
    <div className="panel" data-testid="target-card" style={{ marginBottom: 8 }}>
      <div className="page-head" style={{ marginBottom: 4 }}>
        <div><b>{t.handle}</b> <span className="small mono muted">{short(t.address)}</span>
          {t.name ? <span className="small"> · {t.name}</span> : null}</div>
        <div className={`value ${(t.all_time_pnl || 0) > 0 ? 'pos' : 'neg'}`} data-testid="target-pnl">{usd(t.all_time_pnl)}</div>
      </div>
      <div className="cards">
        <div className="card"><div className="label">BTC 5m share</div>
          <div className={`value ${isBtc ? 'pos' : ''}`}>{num(t.btc_5m_pct, 0)}%</div></div>
        <div className="card"><div className="label">Trades/day</div><div className="value">{num(t.trades_per_day, 0)}</div>
          <div className="sub">{t.n_trades} sampled</div></div>
        <div className="card"><div className="label">Buy %</div><div className="value">{num(t.buy_pct, 0)}%</div></div>
        <div className="card"><div className="label">Avg entry</div><div className="value">{num(t.avg_price, 3)}</div>
          <div className="sub">size {num(t.avg_size, 0)}</div></div>
      </div>
      <div className="diag-strip" style={{ marginTop: 6 }} data-testid="strategy">🧠 {t.strategy}</div>
      <div className="small muted" style={{ marginTop: 4 }}>mix: {Object.entries(t.category_mix || {}).map(([k, v]) => `${k}:${v}`).join(' · ')}</div>
    </div>
  )
}

export function SimilarTable({ rows }) {
  if (!rows?.length) return <Empty>No similar BTC-5m wallets found yet.</Empty>
  return (
    <div className="table-wrap"><table data-testid="similar-table">
      <thead><tr><th>Wallet</th><th className="right">Similarity</th><th className="right">Shared mkts</th>
        <th className="right">Trades</th><th className="right">Buy%</th><th className="right">Avg px</th>
        <th className="right">All-time P&L</th><th>Link</th></tr></thead>
      <tbody>{rows.map((s) => (
        <tr key={s.wallet} data-testid="similar-row">
          <td className="small mono">{short(s.wallet)}{s.name ? ` · ${s.name}` : ''}</td>
          <td className="right"><b>{num(s.similarity, 2)}</b></td>
          <td className="right">{s.markets_shared}</td>
          <td className="right">{s.trades}</td>
          <td className="right">{num(s.buy_pct, 0)}%</td>
          <td className="right">{num(s.avg_price, 3)}</td>
          <td className={`right ${(s.all_time_pnl || 0) > 0 ? 'pos' : 'neg'}`}>{usd(s.all_time_pnl)}</td>
          <td><a href={pmLink(s.wallet)} target="_blank" rel="noreferrer" className="small">↗</a></td>
        </tr>
      ))}</tbody>
    </table></div>
  )
}

export function OurSpecialists({ rows }) {
  if (!rows?.length) return <Empty>No indexed BTC-5m specialists.</Empty>
  return (
    <div className="table-wrap"><table data-testid="our-table">
      <thead><tr><th>Wallet</th><th className="right">P&L</th><th className="right">ROI</th><th className="right">Win%</th>
        <th className="right">Trades</th><th className="right">PF</th><th>Cluster</th></tr></thead>
      <tbody>{rows.map((s) => (
        <tr key={s.wallet} data-testid="our-row">
          <td className="small mono">{short(s.wallet)}</td>
          <td className={`right ${s.realized_pnl > 0 ? 'pos' : 'neg'}`}>{usd(s.realized_pnl)}</td>
          <td className="right">{num((s.roi || 0) * 100, 1)}%</td>
          <td className="right">{num((s.win_rate || 0) * 100, 0)}%</td>
          <td className="right">{s.trade_count}</td>
          <td className="right">{num(s.profit_factor, 2)}</td>
          <td className="small">{s.cluster}</td>
        </tr>
      ))}</tbody>
    </table></div>
  )
}

// Pure presentational report — exported for tests.
export function DrewFindsReport({ report }) {
  if (!report) return <Empty>No findings yet — run the analysis to reverse-engineer the wallets.</Empty>
  return (
    <div data-testid="drew-report">
      <div className="diag-strip" data-testid="summary">🔎 {report.summary}</div>
      <h3 style={{ margin: '12px 0 4px' }}>Target wallets (reverse-engineered)</h3>
      {(report.targets || []).map((t) => <TargetCard key={t.address} t={t} />)}

      <h3 style={{ margin: '12px 0 4px' }}>Similar wallets on the BTC 5m graph (seed {report.seed_wallet || '—'}'s co-traders)</h3>
      <SimilarTable rows={report.similar_btc5m_wallets} />

      <h3 style={{ margin: '14px 0 4px' }}>Our indexed BTC-5m profitable specialists (cross-reference)</h3>
      <OurSpecialists rows={report.our_indexed_specialists} />
    </div>
  )
}

export default function Btc5mDrewFinds() {
  const [status, setStatus] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState(null)

  const load = useCallback(async () => {
    try { setStatus(await api.btc5mDrewFindsStatus().then((r) => r?.detail || r)) }
    catch (e) { setToast(e.message) } finally { setLoading(false) }
  }, [])
  useEffect(() => { load() }, [load])
  useEffect(() => { if (!toast) return; const t = setTimeout(() => setToast(null), 6000); return () => clearTimeout(t) }, [toast])

  const run = async () => {
    setBusy(true)
    try { await api.btc5mDrewFindsRun(); setToast('Analysis complete'); await load() }
    catch (e) { setToast(e.message) } finally { setBusy(false) }
  }

  if (loading) return <Loading />
  const s = status || {}

  return (
    <div>
      <div className="diag-strip" style={{ marginBottom: 10 }}>🧪 {s.safety}</div>
      <div className="page-head" style={{ marginBottom: 8 }}>
        <div>
          <h2 style={{ margin: 0 }}>DREW FINDS</h2>
          <p className="muted small" style={{ margin: '2px 0 0' }}>
            Reverse-engineered wallet strategies + similar BTC 5m traders · read-only · last run {ago(s.built_at)}
          </p>
        </div>
        <div className="toolbar" style={{ gap: 6 }}>
          <button onClick={run} disabled={busy} data-testid="run-btn">{busy ? 'Analyzing…' : 'Run analysis'}</button>
          <button className="secondary" onClick={load} disabled={busy}>↻ Refresh</button>
        </div>
      </div>

      <DrewFindsReport report={s.report} />

      {toast && <div className="toast">{toast}</div>}
    </div>
  )
}
