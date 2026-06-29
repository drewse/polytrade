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

// verdict codes: 1 tradeable edge / 2 predictive-not-tradeable / 3 efficient / 4 insufficient
const VERDICT = {
  1: { kind: 'yes', label: 'Tradeable edge (significant post-cost EV)' },
  2: { kind: 'open', label: 'Predictive signal — not yet tradeable' },
  3: { kind: 'bad', label: 'Efficient market — no durable post-cost edge' },
  4: { kind: 'warn', label: 'Data insufficient' },
}

function ReliabilityStrip({ curve, testid }) {
  if (!curve?.length) return null
  return (
    <div className="small mono" data-testid={testid}>
      {curve.map((c, i) => (
        <span key={i} style={{ marginRight: 10 }} className={Math.abs(c.predicted - c.actual) < 0.1 ? 'pos' : 'neg'}>
          {c.bin}: pred {num(c.predicted, 2)} / act {num(c.actual, 2)} (n{c.n})
        </span>
      ))}
    </div>
  )
}

// Pure presentational report — exported for tests.
export function ResearchReport({ report }) {
  if (!report) return <Empty>No research yet — run the research pipeline.</Empty>
  const v = VERDICT[report.verdict_code] || VERDICT[3]
  const fv = report.fair_value || {}
  const ev = fv.ev || {}
  const ens = report.ensemble || {}
  const ensM = ens.ensemble || {}
  const fd = report.feature_discovery || {}
  const micro = report.microstructure || {}
  const cross = report.cross_market || {}
  const decay = report.decay || {}
  return (
    <div data-testid="research-report">
      <div className={`diag-strip ${['bad', 'warn'].includes(v.kind) ? 'neg' : ''}`} data-testid="research-verdict">
        🧠 Verdict #{report.verdict_code} ({v.label}): <b>{report.headline}</b>
      </div>

      <div className="cards" style={{ marginTop: 8 }}>
        <div className="card"><div className="label">Fair-value AUC</div>
          <div className={`value ${fv.auc > 0.55 ? 'pos' : ''}`} data-testid="fv-auc">{num(fv.auc, 3)}</div>
          <div className="sub">Brier {num(fv.brier, 3)} · cal {num(fv.calibration_score, 3)}</div></div>
        <div className="card"><div className="label">EV after costs / trade</div>
          <div className={`value ${ev.significant ? 'pos' : 'neg'}`} data-testid="fv-ev">{num(ev.ev_after_cost, 4)}</div>
          <div className="sub">t={num(ev.t_stat, 2)} · n={ev.n_trades ?? 0} · {ev.significant ? 'SIGNIFICANT' : 'not significant'}</div></div>
        <div className="card"><div className="label">Ensemble Brier</div>
          <div className={`value ${ensM.calibration_score > 0 ? 'pos' : ''}`}>{num(ensM.brier, 3)}</div>
          <div className="sub">{ens.members?.length ?? 0} perspectives · AUC {num(ensM.auc, 3)}</div></div>
        <div className="card"><div className="label">Model decay</div>
          <div className={`value ${decay.decayed ? 'neg' : 'pos'}`}>{decay.decayed ? 'DECAYED' : 'stable'}</div>
          <div className="sub">Δbrier {num(decay.brier_degradation, 3)}</div></div>
      </div>

      {fv.reliability?.length > 0 && (
        <div className="panel" style={{ marginTop: 8 }}>
          <div className="label">Fair-value calibration (holdout reliability — predicted vs actual P(YES))</div>
          <ReliabilityStrip curve={fv.reliability} testid="fv-reliability" />
        </div>
      )}

      {ens.members?.length > 0 && (
        <div className="panel" style={{ marginTop: 8 }}>
          <div className="label">Ensemble perspectives (combined by calibration reliability, not equally)</div>
          <div className="table-wrap"><table data-testid="ensemble-table">
            <thead><tr><th>Perspective</th><th className="right">Weight</th><th className="right">Brier</th>
              <th className="right">AUC</th><th className="right">EV/trade</th><th className="right">t</th><th className="right">n</th></tr></thead>
            <tbody>{ens.members.map((m) => (
              <tr key={m.perspective} data-testid="ensemble-row">
                <td className="small">{m.perspective}</td>
                <td className="right"><b>{pct(m.weight)}</b></td>
                <td className="right">{num(m.holdout_brier, 3)}</td>
                <td className="right">{num(m.auc, 3)}</td>
                <td className={`right ${m.ev?.significant ? 'pos' : ''}`}>{num(m.ev?.ev_after_cost, 4)}</td>
                <td className="right">{num(m.ev?.t_stat, 2)}</td>
                <td className="right">{m.ev?.n_trades ?? 0}</td></tr>
            ))}</tbody>
          </table></div>
        </div>
      )}

      <div className="cards" style={{ marginTop: 8 }}>
        <div className="card" style={{ flex: 1, minWidth: 260 }}>
          <div className="label">Newly discovered features</div>
          {fd.promoted?.length ? (
            <div className="small mono" data-testid="discovered-features">
              {fd.promoted.slice(0, 8).map((f) => (
                <div key={f.feature}><span className="pos">{f.feature}</span> · corr {num(f.train_corr, 3)}/{num(f.val_corr, 3)}</div>
              ))}
            </div>
          ) : <div className="small muted">none stable this run</div>}
          <div className="sub">{fd.generated ?? 0} generated · {fd.n_stable ?? 0} stable · {fd.eliminated_redundant?.length ?? 0} redundant pruned</div>
        </div>
        <div className="card" style={{ flex: 1, minWidth: 260 }}>
          <div className="label">Microstructure</div>
          <div className="small">large-trade impact ratio <b>{num(micro.large_trade_impact?.impact_ratio, 2)}×</b></div>
          <div className="small">spread expansion {num(micro.spread?.expansion_ratio, 2)}× · clustering {num(micro.trade_clustering_index, 2)}</div>
          <div className="small">price-discovery speed {micro.price_discovery_speed_s == null ? '—' : `${micro.price_discovery_speed_s}s`}</div>
        </div>
        <div className="card" style={{ flex: 1, minWidth: 260 }}>
          <div className="label">Cross-market (BTC spot lead)</div>
          <div className={`small ${cross.btc_spot_lead?.leads ? 'pos' : ''}`}>
            peak lag {cross.btc_spot_lead?.peak_lag_s == null ? '—' : `${cross.btc_spot_lead.peak_lag_s}s`} · corr {num(cross.btc_spot_lead?.peak_corr, 3)}</div>
          {cross.by_duration && Object.entries(cross.by_duration).map(([d, m]) => (
            <div key={d} className="small muted">{d}m: price-info {num(m.price_informativeness, 2)} (n{m.n})</div>
          ))}
        </div>
      </div>

      {report.evolution?.best && (
        <div className="panel" style={{ marginTop: 8 }}>
          <div className="label">Evolutionary search — best survivor ({report.evolution.evaluated} evaluated, {report.evolution.generations} generations)</div>
          <div className="small mono" data-testid="evolution-best">
            {report.evolution.best.family} · score {num(report.evolution.best.score, 1)} · holdout ROI {pct(report.evolution.best.holdout_roi)} · {report.evolution.best.holdout_trades} trades
          </div>
        </div>
      )}
    </div>
  )
}

export function ModelLeaderboard({ models }) {
  if (!models?.length) return <Empty>No models trained yet.</Empty>
  return (
    <div className="table-wrap">
      <table data-testid="model-leaderboard">
        <thead><tr>
          <th>Model</th><th>Kind</th><th className="right">Brier</th><th className="right">Cal</th>
          <th className="right">AUC</th><th className="right">EV/trade</th><th className="right">t</th>
          <th className="right">n</th><th>Status</th>
        </tr></thead>
        <tbody>
          {models.map((m) => (
            <tr key={m.name} data-testid="model-row">
              <td className="small mono">{m.name}</td>
              <td className="small">{m.kind}</td>
              <td className="right">{num(m.brier, 3)}</td>
              <td className={`right ${m.calibration_score > 0 ? 'pos' : ''}`}>{num(m.calibration_score, 3)}</td>
              <td className="right">{num(m.auc, 3)}</td>
              <td className={`right ${m.ev_after_cost > 0 ? 'pos' : 'neg'}`}>{num(m.ev_after_cost, 4)}</td>
              <td className="right">{num(m.ev_t_stat, 2)}</td>
              <td className="right">{m.n_trades}</td>
              <td className="small">{m.promoted ? <span className="pos">promoted</span> : (m.significant ? 'significant' : '—')}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function Btc5mAlphaResearch() {
  const [status, setStatus] = useState(null)
  const [worker, setWorker] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [toast, setToast] = useState(null)

  const load = useCallback(async () => {
    try {
      const [st, wk] = await Promise.all([
        api.btc5mResearchStatus().then((r) => r?.detail || r),
        api.btc5mResearchWorker().then((r) => r?.detail || r).catch(() => null),
      ])
      setStatus(st); setWorker(wk)
    } catch (e) { setToast(e.message) } finally { setLoading(false) }
  }, [])
  useEffect(() => { load() }, [load])
  useEffect(() => { if (!toast) return; const t = setTimeout(() => setToast(null), 6000); return () => clearTimeout(t) }, [toast])

  const act = async (key, fn) => {
    setBusy(key)
    try { const r = await fn().then((x) => x?.detail || x); setToast(JSON.stringify(r).slice(0, 140)); await load() }
    catch (e) { setToast(e.message) } finally { setBusy('') }
  }

  if (loading) return <Loading />
  const s = status || {}
  const report = s.research
  const models = s.model_leaderboard?.models || []

  return (
    <div>
      <div className="diag-strip" style={{ marginBottom: 10 }}>🧪 {s.safety}</div>
      <div className="page-head" style={{ marginBottom: 8 }}>
        <div>
          <h2 style={{ margin: 0 }}>BTC 5M Alpha Research Platform</h2>
          <p className="muted small" style={{ margin: '2px 0 0' }}>
            Estimates the true P(YES) (fair value), gates on statistically-significant EV after spread/slippage,
            and promotes only validated signals · last run {ago(s.research_built_at)} ·
            nightly worker {worker?.worker_running ? 'running' : (worker?.worker_enabled ? 'enabled' : 'off')}
          </p>
        </div>
        <div className="toolbar" style={{ gap: 6 }}>
          <button onClick={() => act('run', () => api.btc5mResearchRun(false))} disabled={busy} data-testid="run-btn">
            {busy === 'run' ? 'Running…' : 'Run research'}</button>
          <button className="secondary" onClick={() => act('rebuild', () => api.btc5mResearchRun(true, 80))} disabled={busy} data-testid="rebuild-btn">
            {busy === 'rebuild' ? 'Rebuilding…' : 'Rebuild + research'}</button>
          <button className="secondary" onClick={load} disabled={busy}>↻ Refresh</button>
        </div>
      </div>

      <ResearchReport report={report} />

      <h3 style={{ margin: '14px 0 4px' }}>Trained model leaderboard (calibration + EV-after-cost)</h3>
      <ModelLeaderboard models={models} />

      {toast && <div className="toast">{toast}</div>}
    </div>
  )
}
