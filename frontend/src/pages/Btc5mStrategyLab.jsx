import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { Loading, Empty } from '../components/common.jsx'

const num = (n, d = 2) => (n == null ? '—' : Number(n).toFixed(d))
const pct = (n, d = 1) => (n == null ? '—' : `${(Number(n) * 100).toFixed(d)}%`)
const ago = (iso) => {
  if (!iso) return 'never'
  const s = (Date.now() - new Date(iso).getTime()) / 1000
  if (s < 60) return `${Math.floor(s)}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  return `${Math.floor(s / 3600)}h ago`
}

const VERDICT = {
  1: { kind: 'yes', label: 'BTC leads Polymarket repricing' },
  2: { kind: 'yes', label: 'Order flow predicts resolution' },
  3: { kind: 'open', label: 'Large trades predict movement' },
  4: { kind: 'open', label: 'Mean reversion after overreaction' },
  5: { kind: 'bad', label: 'No durable edge found' },
}

// Pure presentational report — exported for tests.
export function LabReport({ report }) {
  if (!report) return <Empty>No report yet — build the dataset and run the search.</Empty>
  const v = VERDICT[report.verdict_code] || VERDICT[5]
  const b = report.best_strategy
  return (
    <div data-testid="lab-report">
      <div className={`diag-strip ${v.kind === 'bad' ? 'neg' : ''}`} data-testid="verdict-banner">
        🏁 Verdict #{report.verdict_code}: <b>{report.headline}</b>
      </div>
      <div className="cards">
        <div className="card"><div className="label">Best independent strategy</div>
          <div className="value">{b ? b.name : '—'}</div>
          <div className="sub">{b ? `${b.family} · holdout ROI ${pct(b.holdout_roi)} · ${b.holdout_trades} trades` : 'none survived holdout'}</div></div>
        <div className="card"><div className="label">Accepted strategies</div><div className="value">{report.n_accepted ?? 0}</div></div>
        <div className="card"><div className="label">BTC→PM lag corr</div>
          <div className={`value ${report.lag_analysis?.lag_vs_resolution_corr > 0.1 ? 'pos' : ''}`}>{num(report.lag_analysis?.lag_vs_resolution_corr, 3)}</div>
          <div className="sub">BTC leads when &gt; 0.1</div></div>
        <div className="card"><div className="label">Flow→resolution corr</div>
          <div className={`value ${report.flow_imbalance_analysis?.flow_vs_resolution_corr > 0.1 ? 'pos' : ''}`}>{num(report.flow_imbalance_analysis?.flow_vs_resolution_corr, 3)}</div></div>
        <div className="card"><div className="label">Large-trade hit vs base</div>
          <div className="value">{pct(report.large_trade_analysis?.large_trade_dir_hit_rate)} / {pct(report.large_trade_analysis?.baseline_dir_hit_rate)}</div></div>
      </div>
      <div className="cards" style={{ marginTop: 8 }}>
        {Object.entries(report.family_best_scores || {}).map(([f, s]) => (
          <div key={f} className="card"><div className="label">{f}</div><div className="value">{num(s, 1)}</div><div className="sub">best robust score</div></div>
        ))}
      </div>
    </div>
  )
}

export function LeaderboardTable({ rows, testid, empty }) {
  if (!rows?.length) return <Empty>{empty}</Empty>
  return (
    <div className="table-wrap">
      <table data-testid={testid}>
        <thead><tr>
          <th>Strategy</th><th>Family</th><th className="right">Robust</th><th className="right">Holdout ROI</th>
          <th className="right">Win%</th><th className="right">PF</th><th className="right">Trades</th>
          <th className="right">Max DD</th><th className="right">Avg edge</th><th>Note</th>
        </tr></thead>
        <tbody>
          {rows.map((s) => (
            <tr key={s.name} data-testid="lab-row">
              <td className="small mono">{s.name}</td>
              <td className="small">{s.family}</td>
              <td className="right"><b>{num(s.robust_score, 1)}</b></td>
              <td className={`right ${s.roi > 0 ? 'pos' : 'neg'}`}>{pct(s.roi)}</td>
              <td className="right">{pct(s.win_rate)}</td>
              <td className="right">{num(s.profit_factor, 2)}</td>
              <td className="right">{s.trades}</td>
              <td className="right">{num(s.max_drawdown, 2)}</td>
              <td className="right">{num(s.avg_edge, 3)}</td>
              <td className="small neg" title={s.rejected_reason || ''}>{s.overfit ? `overfit: ${s.rejected_reason || ''}` : ''}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function Btc5mStrategyLab() {
  const [status, setStatus] = useState(null)
  const [board, setBoard] = useState(null)
  const [analyses, setAnalyses] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [toast, setToast] = useState(null)

  const load = useCallback(async () => {
    try {
      const [st, lb, an] = await Promise.all([
        api.btc5mLabStatus().then((r) => r?.detail || r),
        api.btc5mLabLeaderboard(40).then((r) => r?.detail || r).catch(() => null),
        api.btc5mLabAnalyses().then((r) => r?.detail || r).catch(() => null),
      ])
      setStatus(st); setBoard(lb); setAnalyses(an)
    } catch (e) { setToast(e.message) } finally { setLoading(false) }
  }, [])
  useEffect(() => { load() }, [load])
  useEffect(() => { if (!toast) return; const t = setTimeout(() => setToast(null), 5000); return () => clearTimeout(t) }, [toast])

  const act = async (key, fn) => {
    setBusy(key)
    try { const r = await fn().then((x) => x?.detail || x); setToast(JSON.stringify(r).slice(0, 120)); await load() }
    catch (e) { setToast(e.message) } finally { setBusy('') }
  }

  if (loading) return <Loading />
  const s = status || {}
  const ed = analyses?.edge_decay

  return (
    <div>
      <div className="diag-strip" style={{ marginBottom: 10 }}>🧪 {s.safety}</div>
      <div className="page-head" style={{ marginBottom: 8 }}>
        <div>
          <h2 style={{ margin: 0 }}>BTC 5M Independent Strategy Lab</h2>
          <p className="muted small" style={{ margin: '2px 0 0' }}>
            {s.points_built ?? 0} decision points from {s.markets_built ?? 0} markets · BTC source {s.btc_price_source || '—'} ·
            splits {JSON.stringify(s.by_split || {})} · {s.strategies_tested ?? 0} strategies tested · dataset {ago(s.dataset_built_at)}
          </p>
        </div>
        <div className="toolbar" style={{ gap: 6 }}>
          <button className="secondary" onClick={() => act('build', () => api.btc5mLabBuild(80))} disabled={busy} data-testid="build-btn">
            {busy === 'build' ? 'Building…' : 'Build dataset'}</button>
          <button onClick={() => act('search', () => api.btc5mLabSearch())} disabled={busy} data-testid="search-btn">
            {busy === 'search' ? 'Searching…' : 'Run strategy search'}</button>
          <button className="secondary" onClick={load} disabled={busy}>↻ Refresh</button>
        </div>
      </div>

      <LabReport report={s.report} />

      <div className="cards" style={{ marginTop: 12 }}>
        <div className="card" style={{ flex: 1, minWidth: 260 }}>
          <div className="label">BTC ↔ Polymarket lag</div>
          <div className="small">lag→resolution corr <b>{num(analyses?.lag?.lag_vs_resolution_corr, 3)}</b> · n {analyses?.lag?.n ?? 0}</div>
          <div className="small muted">{analyses?.lag?.interpretation}</div>
        </div>
        <div className="card" style={{ flex: 1, minWidth: 260 }}>
          <div className="label">Order-flow imbalance</div>
          <div className="small">flow→resolution corr <b>{num(analyses?.flow_imbalance?.flow_vs_resolution_corr, 3)}</b></div>
          <div className="small muted">{analyses?.flow_imbalance?.interpretation}</div>
        </div>
        <div className="card" style={{ flex: 1, minWidth: 260 }}>
          <div className="label">Large-trade impact</div>
          <div className="small">hit {pct(analyses?.large_trade?.large_trade_dir_hit_rate)} vs base {pct(analyses?.large_trade?.baseline_dir_hit_rate)} · n {analyses?.large_trade?.n_large ?? 0}</div>
          <div className="small muted">{analyses?.large_trade?.interpretation}</div>
        </div>
      </div>

      {ed?.buckets?.length > 0 && (
        <div className="panel" style={{ marginTop: 12 }}>
          <h3 style={{ marginTop: 0 }}>Edge decay by entry window — {ed.strategy}</h3>
          <div className="table-wrap"><table data-testid="edge-decay">
            <thead><tr><th>Entry window (s)</th><th className="right">Trades</th><th className="right">ROI</th><th className="right">Avg edge</th></tr></thead>
            <tbody>{ed.buckets.map((b) => (
              <tr key={b.entry_window_s}><td>{b.entry_window_s}</td><td className="right">{b.trades}</td>
                <td className={`right ${b.roi > 0 ? 'pos' : 'neg'}`}>{pct(b.roi)}</td><td className="right">{num(b.avg_edge, 3)}</td></tr>
            ))}</tbody>
          </table></div>
        </div>
      )}

      <h3 style={{ margin: '14px 0 4px' }}>Best independent strategies (accepted, out-of-sample)</h3>
      <LeaderboardTable rows={board?.accepted} testid="accepted-table" empty="No accepted strategies yet — run the search." />

      <h3 style={{ margin: '14px 0 4px' }}>Rejected / overfit</h3>
      <LeaderboardTable rows={board?.rejected} testid="rejected-table" empty="No rejected strategies." />

      {toast && <div className="toast">{toast}</div>}
    </div>
  )
}
