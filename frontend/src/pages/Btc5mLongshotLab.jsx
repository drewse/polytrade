import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { Loading, Empty } from '../components/common.jsx'

const num = (n, d = 4) => (n == null ? '—' : Number(n).toFixed(d))
const pct = (n, d = 1) => (n == null ? '—' : `${(Number(n) * 100).toFixed(d)}%`)
const ago = (iso) => {
  if (!iso) return 'never'
  const s = (Date.now() - new Date(iso).getTime()) / 1000
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  return `${Math.floor(s / 3600)}h ago`
}

const VERDICT = {
  1: { kind: 'yes', label: 'Tradeable — cheap-side making +EV as a maker' },
  2: { kind: 'open', label: 'Real mispricing (significant at mid), execution needs work' },
  3: { kind: 'warn', label: 'Suggestive mispricing, not yet significant' },
  4: { kind: 'bad', label: 'No cheap-side mispricing — not longshot bias' },
}

export function CalibrationTable({ calib }) {
  if (!calib?.bins?.length) return null
  return (
    <div className="panel" style={{ marginTop: 8 }}>
      <div className="label">Calibration — implied vs ACTUAL up-rate by price bin (slope {calib.calibration_slope},
        cheap-side edge at mid <b className={calib.cheap_side_edge_at_mid > 0 ? 'pos' : 'neg'}>{num(calib.cheap_side_edge_at_mid, 4)}</b>/share)</div>
      <div className="table-wrap"><table data-testid="calib-table">
        <thead><tr><th>Price bin</th><th className="right">n</th><th className="right">Implied</th>
          <th className="right">Actual</th><th className="right">Mispricing</th></tr></thead>
        <tbody>{calib.bins.map((b) => (
          <tr key={b.bin} data-testid="calib-row">
            <td className="small">{b.bin}</td><td className="right">{b.n}</td>
            <td className="right">{num(b.implied_up, 3)}</td><td className="right">{num(b.actual_up, 3)}</td>
            <td className={`right ${Math.abs(b.mispricing) > 0.02 ? (b.mispricing > 0 ? 'pos' : 'neg') : ''}`}>{num(b.mispricing, 3)}</td>
          </tr>
        ))}</tbody>
      </table></div>
      <div className="small muted" style={{ marginTop: 4 }}>{calib.interpretation}</div>
    </div>
  )
}

export function GridTable({ grid }) {
  if (!grid?.length) return null
  return (
    <div className="table-wrap"><table data-testid="grid-table">
      <thead><tr><th>Execution</th><th className="right">Max entry</th><th className="right">N</th>
        <th className="right">EV/trade</th><th className="right">Win%</th><th className="right">ROI</th>
        <th className="right">t</th><th className="right">P(EV&gt;0)</th><th>Sig?</th></tr></thead>
      <tbody>{grid.map((g, i) => (
        <tr key={i} data-testid="grid-row" className={g.execution === 'maker' ? 'pos' : (g.execution === 'taker' ? 'neg' : '')}>
          <td className="small">{g.execution}</td>
          <td className="right">{num(g.max_entry, 2)}</td>
          <td className="right">{g.n}</td>
          <td className={`right ${g.ev_per_trade > 0 ? 'pos' : 'neg'}`}>{num(g.ev_per_trade, 4)}</td>
          <td className="right">{pct(g.win_rate)}</td>
          <td className="right">{pct(g.roi)}</td>
          <td className="right">{num(g.t_stat, 2)}</td>
          <td className="right">{num(g.prob_ev_positive, 2)}</td>
          <td className="small">{g.significant ? <span className="pos">✓</span> : '—'}</td>
        </tr>
      ))}</tbody>
    </table></div>
  )
}

// Pure presentational — exported for tests.
export function LongshotReport({ report }) {
  if (!report || report.ok === false) return <Empty>No result yet — run the cheap-side test.</Empty>
  const v = VERDICT[report.verdict_code] || VERDICT[4]
  const cells = report.headline_cells || {}
  return (
    <div data-testid="longshot-report">
      <div className={`diag-strip ${['bad', 'warn'].includes(v.kind) ? 'neg' : ''}`} data-testid="longshot-verdict">
        🎯 #{report.verdict_code} ({v.label})
      </div>
      <div className="small muted" style={{ margin: '4px 0' }} data-testid="longshot-headline">{report.headline}</div>

      <div className="cards" style={{ marginTop: 8 }}>
        <div className="card"><div className="label">Cheap-side edge @ mid</div>
          <div className={`value ${(report.calibration?.cheap_side_edge_at_mid || 0) > 0 ? 'pos' : 'neg'}`} data-testid="edge">
            {num(report.calibration?.cheap_side_edge_at_mid, 4)}</div><div className="sub">per $1, full sample</div></div>
        <div className="card"><div className="label">Calibration slope</div>
          <div className={`value ${(report.calibration?.calibration_slope || 1) < 0.95 ? 'pos' : ''}`}>{num(report.calibration?.calibration_slope, 3)}</div>
          <div className="sub">&lt;1 ⇒ overreaction/reversion</div></div>
        <div className="card"><div className="label">Maker EV (entry&lt;0.45)</div>
          <div className={`value ${(cells.maker_cheap?.ev_per_trade || 0) > 0 ? 'pos' : 'neg'}`}>{num(cells.maker_cheap?.ev_per_trade, 4)}</div>
          <div className="sub">n {cells.maker_cheap?.n ?? 0} · P {num(cells.maker_cheap?.prob_ev_positive, 2)}</div></div>
        <div className="card"><div className="label">Taker EV (control)</div>
          <div className={`value ${(cells.taker_all?.ev_per_trade || 0) > 0 ? 'pos' : 'neg'}`}>{num(cells.taker_all?.ev_per_trade, 4)}</div>
          <div className="sub">should be negative</div></div>
      </div>

      <CalibrationTable calib={report.calibration} />

      <h3 style={{ margin: '12px 0 4px' }}>Execution × entry-threshold grid</h3>
      <GridTable grid={report.grid} />

      {report.wallet_benchmark && (
        <div className="panel" style={{ marginTop: 8 }}>
          <div className="small">📌 Benchmark: the {report.wallet_benchmark.profitable_wallets} profitable DREW FINDS wallets
            buy at avg entry <b>{report.wallet_benchmark.avg_entry}</b> — compare to the ~0.45 rows above.</div>
        </div>
      )}
    </div>
  )
}

export default function Btc5mLongshotLab() {
  const [status, setStatus] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState(null)

  const load = useCallback(async () => {
    try { setStatus(await api.btc5mLongshotStatus().then((r) => r?.detail || r)) }
    catch (e) { setToast(e.message) } finally { setLoading(false) }
  }, [])
  useEffect(() => { load() }, [load])
  useEffect(() => { if (!toast) return; const t = setTimeout(() => setToast(null), 6000); return () => clearTimeout(t) }, [toast])

  const run = async () => {
    setBusy(true)
    try { await api.btc5mLongshotRun(); setToast('Done'); await load() }
    catch (e) { setToast(e.message) } finally { setBusy(false) }
  }

  if (loading) return <Loading />
  const s = status || {}

  return (
    <div>
      <div className="diag-strip" style={{ marginBottom: 10 }}>🧪 {s.safety}</div>
      <div className="page-head" style={{ marginBottom: 8 }}>
        <div>
          <h2 style={{ margin: 0 }}>Longshot / Value Lab</h2>
          <p className="muted small" style={{ margin: '2px 0 0' }}>
            Does buying the CHEAP side (what the 12 profitable wallets do) have +EV in our own data? · last run {ago(s.built_at)}
          </p>
        </div>
        <div className="toolbar" style={{ gap: 6 }}>
          <button onClick={run} disabled={busy} data-testid="run-btn">{busy ? 'Testing…' : 'Run cheap-side test'}</button>
          <button className="secondary" onClick={load} disabled={busy}>↻ Refresh</button>
        </div>
      </div>

      <LongshotReport report={s.report} />

      {toast && <div className="toast">{toast}</div>}
    </div>
  )
}
