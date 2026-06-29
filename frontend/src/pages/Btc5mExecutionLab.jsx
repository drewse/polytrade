import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { Loading, Empty } from '../components/common.jsx'

const num = (n, d = 4) => (n == null ? '—' : Number(n).toFixed(d))
const pct = (n, d = 1) => (n == null ? '—' : `${(Number(n) * 100).toFixed(d)}%`)
const ago = (iso) => {
  if (!iso) return 'never'
  const s = (Date.now() - new Date(iso).getTime()) / 1000
  if (s < 60) return `${Math.floor(s)}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  return `${Math.floor(s / 3600)}h ago`
}

const VERDICT = {
  1: { kind: 'yes', label: 'Execution creates a tradeable edge' },
  2: { kind: 'open', label: 'Execution helps but not enough' },
  3: { kind: 'bad', label: 'Execution is not the bottleneck' },
}

function FrontierTable({ rows, best }) {
  if (!rows?.length) return null
  return (
    <div className="table-wrap"><table data-testid="exec-frontier">
      <thead><tr><th>Policy</th><th className="right">Fill%</th><th className="right">Avg fill px</th>
        <th className="right">Spread cap</th><th className="right">EV/trade</th><th className="right">ROI</th>
        <th className="right">t</th><th className="right">Sharpe</th><th className="right">Max DD</th><th>Sig?</th></tr></thead>
      <tbody>{rows.map((r) => (
        <tr key={r.policy} data-testid="frontier-row" className={best && r.policy === best.policy ? 'pos' : ''}>
          <td className="small mono">{r.policy}{best && r.policy === best.policy ? ' ★' : ''}</td>
          <td className="right">{pct(r.fill_rate)}</td>
          <td className="right">{num(r.avg_fill_price, 3)}</td>
          <td className={`right ${r.avg_spread_captured > 0 ? 'pos' : 'neg'}`}>{num(r.avg_spread_captured, 3)}</td>
          <td className={`right ${r.ev_after_cost > 0 ? 'pos' : 'neg'}`}>{num(r.ev_after_cost, 4)}</td>
          <td className="right">{pct(r.roi)}</td>
          <td className="right">{num(r.t_stat, 2)}</td>
          <td className="right">{num(r.sharpe, 2)}</td>
          <td className="right">{num(r.max_drawdown, 2)}</td>
          <td className="small">{r.significant ? <span className="pos">yes</span> : '—'}</td>
        </tr>
      ))}</tbody>
    </table></div>
  )
}

// Pure presentational — exported for tests.
export function ExecutionReport({ report }) {
  if (!report || report.ok === false) return <Empty>No execution research yet — run the simulation.</Empty>
  const v = VERDICT[report.verdict_code] || VERDICT[3]
  const fm = report.fill_probability || {}
  const promo = report.promotion_experiment || {}
  const bd = report.breakdowns || {}
  const best = report.best_policy || {}
  return (
    <div data-testid="execution-report">
      <div className={`diag-strip ${['bad'].includes(v.kind) ? 'neg' : ''}`} data-testid="execution-verdict">
        ⚙️ #{report.verdict_code} ({v.label}): <b>{report.headline}</b>
      </div>

      <div className="cards" style={{ marginTop: 8 }}>
        <div className="card"><div className="label">Best execution policy</div>
          <div className="value" data-testid="best-policy">{best.policy || '—'}</div>
          <div className="sub">EV {num(best.ev_after_cost, 4)} · fill {pct(best.fill_rate)} · {best.significant ? 'significant' : 'not significant'}</div></div>
        <div className="card"><div className="label">Models flipped → paper</div>
          <div className={`value ${promo.models_flipped_to_paper > 0 ? 'pos' : 'neg'}`} data-testid="flips">
            {promo.models_flipped_to_paper ?? 0}<span className="sub"> / {promo.models_tested ?? 0}</span></div>
          <div className="sub">execution-only, same gates</div></div>
        <div className="card"><div className="label">5s passive fill rate</div>
          <div className="value">{pct(fm.overall_5s_fill_rate)}</div>
          <div className="sub">λ {num(fm.hazard_lambda_per_s, 3)}/s</div></div>
        <div className="card"><div className="label">Spread captured (best)</div>
          <div className={`value ${best.avg_spread_captured > 0 ? 'pos' : ''}`}>{num(best.avg_spread_captured, 3)}</div></div>
      </div>

      <div className="panel" style={{ marginTop: 8 }}>
        <h3 style={{ marginTop: 0 }}>Execution frontier (holdout)</h3>
        <FrontierTable rows={report.execution_frontier} best={best} />
      </div>

      <div className="panel" style={{ marginTop: 8 }}>
        <div className="label">Fill-probability by timeout (passive bid)</div>
        <div className="small mono" data-testid="fill-curve">
          {Object.entries(fm.modelled_fill_rate || {}).map(([t, p]) => {
            const empirical = (fm.empirical_fill_rate || {})[t]
            return <span key={t} style={{ marginRight: 12 }}>{t}s: {pct(p)}{empirical != null ? ` (emp ${pct(empirical)})` : ' *'}</span>
          })}
        </div>
        <div className="small muted" style={{ marginTop: 4 }}>{fm.note}</div>
      </div>

      {promo.results?.length > 0 && (
        <div className="panel" style={{ marginTop: 8 }}>
          <div className="label">Promotion experiment — market vs best passive (no retraining, same gates)</div>
          <div className="table-wrap"><table data-testid="promo-table">
            <thead><tr><th>Model</th><th>Market state</th><th className="right">Mkt EV (t)</th>
              <th>Passive state</th><th className="right">Pass EV (t)</th><th className="right">Pass fills</th><th>Flipped?</th></tr></thead>
            <tbody>{promo.results.filter((r) => !r.skipped).map((r) => (
              <tr key={r.model} data-testid="promo-row">
                <td className="small mono">{r.model}</td>
                <td className="small">{r.market?.state}</td>
                <td className="right">{num(r.market?.ev, 4)} ({num(r.market?.t, 1)})</td>
                <td className={`small ${r.passive?.state === 'paper' ? 'pos' : ''}`}>{r.passive?.state}</td>
                <td className="right">{num(r.passive?.ev, 4)} ({num(r.passive?.t, 1)})</td>
                <td className="right">{r.passive?.fills} ({pct(r.passive?.fill_rate)})</td>
                <td className="small">{r.flipped_to_paper ? <span className="pos">✓ flipped</span> : '—'}</td></tr>
            ))}</tbody>
          </table></div>
        </div>
      )}

      {bd.by_regime && (
        <div className="panel" style={{ marginTop: 8 }}>
          <div className="label">Best-passive breakdown by regime (does passive dominate anywhere?)</div>
          <div className="small mono" data-testid="regime-breakdown">
            {Object.entries(bd.by_regime).map(([rg, d]) => (
              <span key={rg} style={{ marginRight: 12 }} className={d.significant ? 'pos' : ''}>
                {rg}: EV {num(d.ev_after_cost, 4)} fill {pct(d.fill_rate)} (n{d.signals}){d.significant ? ' ✓' : ''}</span>
            ))}
          </div>
        </div>
      )}

      <div className="panel" style={{ marginTop: 8 }}>
        <div className="label">Research answers</div>
        <ol className="small" style={{ margin: '4px 0 0 16px' }} data-testid="research-answers">
          {(report.research_answers || []).map((a, i) => (
            <li key={i} style={{ marginBottom: 2 }}><b>{a.q}</b> → <span className="pos">{a.a}</span> <span className="muted">({a.detail})</span></li>
          ))}
        </ol>
      </div>

      <div className="panel" style={{ marginTop: 8 }}>
        <div className="label">Modelling approximations (honest)</div>
        <div className="small muted">{(report.approximations || []).join(' · ')}</div>
      </div>
    </div>
  )
}

export default function Btc5mExecutionLab() {
  const [status, setStatus] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState(null)

  const load = useCallback(async () => {
    try { setStatus(await api.btc5mExecutionStatus().then((r) => r?.detail || r)) }
    catch (e) { setToast(e.message) } finally { setLoading(false) }
  }, [])
  useEffect(() => { load() }, [load])
  useEffect(() => { if (!toast) return; const t = setTimeout(() => setToast(null), 6000); return () => clearTimeout(t) }, [toast])

  const run = async () => {
    setBusy(true)
    try { const r = await api.btc5mExecutionRun().then((x) => x?.detail || x); setToast(JSON.stringify(r?.headline || r).slice(0, 160)); await load() }
    catch (e) { setToast(e.message) } finally { setBusy(false) }
  }

  if (loading) return <Loading />
  const s = status || {}

  return (
    <div>
      <div className="diag-strip" style={{ marginBottom: 10 }}>🧪 {s.safety}</div>
      <div className="page-head" style={{ marginBottom: 8 }}>
        <div>
          <h2 style={{ margin: 0 }}>BTC 5M Execution Research Lab</h2>
          <p className="muted small" style={{ margin: '2px 0 0' }}>
            Can passive liquidity provision (capturing spread instead of paying it) convert predictive-but-untradeable
            models into significant +EV? · last run {ago(s.execution_built_at)}
          </p>
        </div>
        <div className="toolbar" style={{ gap: 6 }}>
          <button onClick={run} disabled={busy} data-testid="run-btn">{busy ? 'Simulating…' : 'Run execution research'}</button>
          <button className="secondary" onClick={load} disabled={busy}>↻ Refresh</button>
        </div>
      </div>

      <ExecutionReport report={s.execution} />

      {toast && <div className="toast">{toast}</div>}
    </div>
  )
}
