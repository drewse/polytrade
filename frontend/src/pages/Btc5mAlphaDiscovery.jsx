import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { Loading, Empty } from '../components/common.jsx'

const num = (n, d = 3) => (n == null ? '—' : Number(n).toFixed(d))
const pct = (n, d = 1) => (n == null ? '—' : `${(Number(n) * 100).toFixed(d)}%`)
const ago = (iso) => {
  if (!iso) return 'never'
  const s = (Date.now() - new Date(iso).getTime()) / 1000
  if (s < 60) return `${Math.floor(s)}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  return `${Math.floor(s / 3600)}h ago`
}

const VERDICT = {
  1: { kind: 'yes', label: 'Alpha promoted to paper' },
  2: { kind: 'open', label: 'Predictive alpha — not yet tradeable' },
  3: { kind: 'bad', label: 'No tradeable alpha this generation' },
}
const LIFECYCLE = { paper: 'pos', candidate: '', demoted: 'neg', retired: 'neg' }

// Pure presentational — exported for tests.
export function DiscoveryReport({ report }) {
  if (!report || report.ok === false) return <Empty>No discovery run yet — run a generation.</Empty>
  const v = VERDICT[report.verdict_code] || VERDICT[3]
  const mi = report.mining || {}
  const model = report.model || {}
  const m = model.metrics || {}
  const cross = report.cross_market || {}
  return (
    <div data-testid="discovery-report">
      <div className={`diag-strip ${['bad'].includes(v.kind) ? 'neg' : ''}`} data-testid="discovery-verdict">
        🔬 Generation {report.generation} · #{report.verdict_code} ({v.label}): <b>{report.headline}</b>
      </div>

      <div className="cards" style={{ marginTop: 8 }}>
        <div className="card"><div className="label">Features mined</div>
          <div className="value" data-testid="mined-count">{mi.survived ?? 0}<span className="sub"> / {mi.generated ?? 0}</span></div>
          <div className="sub">{mi.evaluated ?? 0} passed IC/MI · survived after stability+pruning</div></div>
        <div className="card"><div className="label">New alpha this gen</div>
          <div className={`value ${report.new_alpha?.length ? 'pos' : ''}`}>{report.new_alpha?.length ?? 0}</div>
          <div className="sub">{report.gained_power?.length ?? 0} gained · {report.lost_power?.length ?? 0} lost power</div></div>
        <div className="card"><div className="label">Retrained model</div>
          <div className={`value ${LIFECYCLE[model.lifecycle_state] || ''}`} data-testid="model-lifecycle">{model.lifecycle_state || '—'}</div>
          <div className="sub">AUC {num(m.auc, 3)} · EV {num(m.ev_after_cost, 4)} (t {num(m.ev_t_stat, 2)}) · {model.vs_prev || 'new'}</div></div>
        <div className="card"><div className="label">External leads</div>
          <div className={`value ${report.external_leads?.length ? 'pos' : ''}`}>{report.external_leads?.length ? report.external_leads.join(', ') : 'none'}</div>
          <div className="sub">ETH/SOL spot → Polymarket lead</div></div>
      </div>

      {report.top_features?.length > 0 && (
        <div className="panel" style={{ marginTop: 8 }}>
          <div className="label">Top mined features (by SHAP-style importance · IC · stability)</div>
          <div className="table-wrap"><table data-testid="top-features">
            <thead><tr><th>Feature</th><th>Category</th><th className="right">IC</th><th className="right">MI</th>
              <th className="right">SHAP</th><th className="right">Stab(split/reg/mo)</th><th className="right">Decay</th></tr></thead>
            <tbody>{report.top_features.map((f) => (
              <tr key={f.name} data-testid="feature-row">
                <td className="small mono">{f.name}</td>
                <td className="small">{f.category}</td>
                <td className={`right ${Math.abs(f.ic) > 0.1 ? 'pos' : ''}`}>{num(f.ic, 3)}</td>
                <td className="right">{num(f.mutual_info, 3)}</td>
                <td className="right">{num(f.shap, 3)}</td>
                <td className="right small">{num(f.stability_splits, 2)}/{num(f.stability_regime, 2)}/{num(f.stability_month, 2)}</td>
                <td className={`right ${f.decay > 0.5 ? 'neg' : ''}`}>{num(f.decay, 2)}</td></tr>
            ))}</tbody>
          </table></div>
        </div>
      )}

      <div className="cards" style={{ marginTop: 8 }}>
        <div className="card" style={{ flex: 1, minWidth: 240 }}>
          <div className="label">Newly discovered</div>
          {report.new_alpha?.length ? (
            <div className="small mono" data-testid="new-alpha">{report.new_alpha.slice(0, 10).map((n) => <div key={n} className="pos">{n}</div>)}</div>
          ) : <div className="small muted">none this generation</div>}
        </div>
        <div className="card" style={{ flex: 1, minWidth: 240 }}>
          <div className="label">Lost predictive power</div>
          {report.lost_power?.length ? (
            <div className="small mono">{report.lost_power.slice(0, 10).map((f) => <div key={f.name} className="neg">{f.name} ({num(f.ic_change, 3)})</div>)}</div>
          ) : <div className="small muted">none decayed</div>}
        </div>
        <div className="card" style={{ flex: 1, minWidth: 240 }}>
          <div className="label">Cross-asset lead test</div>
          {cross.assets ? Object.entries(cross.assets).map(([a, d]) => (
            <div key={a} className={`small ${d.leads ? 'pos' : 'muted'}`}>{a}: {d.n_markets ? `lag ${d.avg_peak_lag_s}s corr ${num(d.avg_peak_corr, 3)} (${pct(d.leads_fraction)})` : 'no data'}</div>
          )) : <div className="small muted">not run</div>}
        </div>
      </div>

      <div className="panel" style={{ marginTop: 8 }}>
        <div className="label">Promotion rule</div>
        <div className="small muted">{report.promotion_rules}</div>
        {model.promotion_reason && <div className="small" style={{ marginTop: 4 }}>Model verdict: <b>{model.promotion_reason}</b></div>}
      </div>

      <div className="panel" style={{ marginTop: 8 }}>
        <div className="label">Data gaps (categories needing raw tick / book / derivatives ingestion)</div>
        <div className="small muted">{(report.data_gaps || []).join(' · ')}</div>
      </div>
    </div>
  )
}

export function ModelGenerations({ generations }) {
  if (!generations?.length) return <Empty>No model generations yet.</Empty>
  return (
    <div className="table-wrap">
      <table data-testid="model-gens">
        <thead><tr>
          <th>Gen</th><th>Model</th><th className="right">#Feat</th><th className="right">AUC</th>
          <th className="right">EV/trade</th><th className="right">t</th><th className="right">Regime-stab</th>
          <th className="right">Decay</th><th>Lifecycle</th><th>vs prev</th>
        </tr></thead>
        <tbody>
          {generations.map((g) => (
            <tr key={g.generation} data-testid="gen-row">
              <td>{g.generation}</td>
              <td className="small mono">{g.name}</td>
              <td className="right">{g.n_features}</td>
              <td className="right">{num(g.auc, 3)}</td>
              <td className={`right ${g.ev_after_cost > 0 ? 'pos' : 'neg'}`}>{num(g.ev_after_cost, 4)}</td>
              <td className="right">{num(g.ev_t_stat, 2)}</td>
              <td className="right">{num(g.regime_stability, 2)}</td>
              <td className="right">{num(g.decay, 3)}</td>
              <td className={`small ${LIFECYCLE[g.lifecycle_state] || ''}`}>{g.lifecycle_state}</td>
              <td className="small">{g.vs_prev}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function Btc5mAlphaDiscovery() {
  const [status, setStatus] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [toast, setToast] = useState(null)

  const load = useCallback(async () => {
    try { setStatus(await api.btc5mDiscoveryStatus().then((r) => r?.detail || r)) }
    catch (e) { setToast(e.message) } finally { setLoading(false) }
  }, [])
  useEffect(() => { load() }, [load])
  useEffect(() => { if (!toast) return; const t = setTimeout(() => setToast(null), 6000); return () => clearTimeout(t) }, [toast])

  const act = async (key, fn) => {
    setBusy(key)
    try { const r = await fn().then((x) => x?.detail || x); setToast(JSON.stringify(r).slice(0, 160)); await load() }
    catch (e) { setToast(e.message) } finally { setBusy('') }
  }

  if (loading) return <Loading />
  const s = status || {}
  const report = s.alpha_research
  const gens = s.model_generations?.generations || []
  const reg = s.feature_registry || {}

  return (
    <div>
      <div className="diag-strip" style={{ marginBottom: 10 }}>🧪 {s.safety}</div>
      <div className="page-head" style={{ marginBottom: 8 }}>
        <div>
          <h2 style={{ margin: 0 }}>BTC 5M Alpha Discovery Engine</h2>
          <p className="muted small" style={{ margin: '2px 0 0' }}>
            Generation {s.generation ?? 0} · {reg.n_active ?? 0} active features / {reg.n_total_tracked ?? 0} tracked ·
            continuously mines new predictive features the market may not price · last run {ago(s.alpha_built_at)}
          </p>
        </div>
        <div className="toolbar" style={{ gap: 6 }}>
          <button onClick={() => act('run', () => api.btc5mDiscoveryRun(false))} disabled={busy} data-testid="run-btn">
            {busy === 'run' ? 'Mining…' : 'Run generation'}</button>
          <button className="secondary" onClick={() => act('cross', () => api.btc5mDiscoveryRun(true))} disabled={busy} data-testid="cross-btn">
            {busy === 'cross' ? 'Running…' : '+ Cross-asset'}</button>
          <button className="secondary" onClick={load} disabled={busy}>↻ Refresh</button>
        </div>
      </div>

      <DiscoveryReport report={report} />

      <h3 style={{ margin: '14px 0 4px' }}>Model generations (meta-learning lifecycle)</h3>
      <ModelGenerations generations={gens} />

      {toast && <div className="toast">{toast}</div>}
    </div>
  )
}
