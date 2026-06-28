import { useCallback, useEffect, useState } from 'react'
import { api, fmt } from '../api'
import { Loading, Empty, Stat, Sparkline } from '../components/common.jsx'

const num = (n, d = 2) => (n == null ? '—' : Number(n).toFixed(d))
const pct = (n, d = 1) => (n == null ? '—' : `${(Number(n) * 100).toFixed(d)}%`)

const SECTIONS = [
  'Overview', 'Experiment Feed', 'Challengers', 'Timing', 'Sizing', 'Confidence',
  'Consensus', 'Regime Performance', 'Recommendations', 'Nightly Reviews',
]
const SIG_TONE = { Significant: 'yes', Promising: 'open', Regressing: 'bad', Rejected: 'neutral', 'Insufficient Data': 'neutral' }

// ---- pure components (exported for tests) --------------------------------
export function ChallengerTable({ rows, onSelect }) {
  if (!rows?.length) return <Empty>No challengers yet — run a challenger cycle.</Empty>
  return (
    <div className="table-wrap">
      <table data-testid="challenger-table">
        <thead><tr>
          <th>Challenger</th><th>Kind</th><th className="right">Trades</th><th className="right">ROI</th>
          <th className="right">PF</th><th className="right">Win%</th><th className="right">vs Prod</th>
          <th>Significance</th><th className="right">Robust</th>
        </tr></thead>
        <tbody>
          {rows.map((c) => (
            <tr key={c.key} data-testid="challenger-row" className={c.is_champion ? 'highlight' : ''}
              style={{ cursor: onSelect ? 'pointer' : 'default' }} onClick={() => onSelect?.(c.key)}>
              <td><b>{c.is_champion ? '★ ' : ''}{c.name}</b>{c.is_production ? <span className="badge neutral" style={{ marginLeft: 4 }}>prod</span> : ''}</td>
              <td className="small">{c.kind}</td>
              <td className="right">{c.trades}</td>
              <td className="right">{pct(c.metrics?.roi)}</td>
              <td className="right">{num(c.metrics?.profit_factor)}</td>
              <td className="right">{pct(c.metrics?.win_rate)}</td>
              <td className={`right ${(c.vs_production?.mean_improvement ?? 0) > 0 ? 'pos' : (c.vs_production?.mean_improvement ?? 0) < 0 ? 'neg' : ''}`}>
                {c.is_production ? '—' : num(c.vs_production?.mean_improvement, 3)}</td>
              <td>{c.is_production ? '' : <span className={`badge ${SIG_TONE[c.vs_production?.significance] || 'neutral'}`}>{c.vs_production?.significance}</span>}</td>
              <td className="right">{num(c.robust_score, 1)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function ComparisonTable({ rows }) {
  if (!rows?.length) return <Empty>No data for this category yet.</Empty>
  return (
    <div className="table-wrap">
      <table data-testid="comparison-table">
        <thead><tr><th>Variant</th><th className="right">Trades</th><th className="right">ROI</th><th className="right">PF</th>
          <th className="right">Sharpe</th><th className="right">MaxDD</th><th className="right">vs Prod</th><th>Significance</th></tr></thead>
        <tbody>
          {rows.map((c) => (
            <tr key={c.key} data-testid="comparison-row">
              <td><b>{c.name}</b></td><td className="right">{c.trades}</td>
              <td className="right">{pct(c.roi)}</td><td className="right">{num(c.profit_factor)}</td>
              <td className="right">{num(c.sharpe)}</td><td className="right">{pct(c.max_drawdown)}</td>
              <td className={`right ${(c.vs_production?.mean_improvement ?? 0) > 0 ? 'pos' : 'neg'}`}>{num(c.vs_production?.mean_improvement, 3)}</td>
              <td><span className={`badge ${SIG_TONE[c.vs_production?.significance] || 'neutral'}`}>{c.vs_production?.significance}</span></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function ExperimentFeed({ rows, onSelect }) {
  if (!rows?.length) return <Empty>No experiments yet.</Empty>
  return (
    <div className="table-wrap">
      <table data-testid="experiment-table">
        <thead><tr><th>When</th><th>Market</th><th>Regime</th><th>Outcome</th><th>Winner</th><th className="right">Improvement</th></tr></thead>
        <tbody>
          {rows.map((e) => (
            <tr key={e.id} data-testid="experiment-row" style={{ cursor: onSelect ? 'pointer' : 'default' }} onClick={() => onSelect?.(e.id)}>
              <td className="small muted">{fmt.ago(e.created_at)}</td>
              <td className="small">{(e.market || e.market_id).slice(0, 30)}</td>
              <td><span className="badge neutral">{e.regime}</span></td>
              <td className="small">{e.outcome}</td>
              <td className="small"><b>{e.winner}</b></td>
              <td className={`right ${e.improvement > 0 ? 'pos' : ''}`}>{num(e.improvement, 3)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function ExperimentDrilldown({ data, onClose }) {
  if (!data) return null
  const decisions = Object.entries(data.challenger_decisions || {})
  return (
    <div className="panel" data-testid="experiment-drilldown" style={{ borderLeft: '3px solid #4ea1ff' }}>
      <div className="page-head" style={{ marginBottom: 6 }}>
        <h3 style={{ margin: 0 }}>Experiment #{data.id} <span className="muted small">· {data.regime} · outcome {data.outcome} · winner {data.winner}</span></h3>
        <button className="secondary" onClick={onClose}>✕ Close</button>
      </div>
      <p className="small muted">{(data.market || data.market_id)}</p>
      <div className="table-wrap"><table data-testid="decisions-table">
        <thead><tr><th>Challenger</th><th>Action</th><th className="right">Entry</th><th className="right">Size</th><th className="right">P/L</th><th>Result</th></tr></thead>
        <tbody>{decisions.sort((a, b) => (b[1].pnl ?? 0) - (a[1].pnl ?? 0)).map(([key, d]) => (
          <tr key={key} className={key === data.winner ? 'highlight' : ''}>
            <td className="small">{key === data.winner ? '★ ' : ''}{key}</td>
            <td><span className={`badge ${d.action === 'NO_TRADE' ? 'neutral' : 'open'}`}>{d.action}</span></td>
            <td className="right">{num(d.entry_price, 3)}</td><td className="right">{fmt.usd2(d.size)}</td>
            <td className={`right ${d.pnl > 0 ? 'pos' : d.pnl < 0 ? 'neg' : ''}`}>{num(d.pnl, 3)}</td>
            <td>{d.won == null ? '—' : d.won ? <span className="pos">✓</span> : <span className="neg">✗</span>}</td>
          </tr>
        ))}</tbody>
      </table></div>
    </div>
  )
}

export function ChallengerDrilldown({ data, onClose }) {
  if (!data) return null
  const c = data.challenger
  const eq = c.equity_curve || []
  return (
    <div className="panel" data-testid="challenger-drilldown" style={{ borderLeft: '3px solid #4ea1ff' }}>
      <div className="page-head" style={{ marginBottom: 6 }}>
        <h3 style={{ margin: 0 }}>{c.is_champion ? '★ ' : ''}{c.name} <span className="muted small">· {c.kind}</span></h3>
        <button className="secondary" onClick={onClose}>✕ Close</button>
      </div>
      <div className="cards">
        <Stat label="Paper bankroll" value={fmt.usd2(c.paper_bankroll)} />
        <Stat label="ROI" value={pct(c.metrics?.roi)} tone={c.metrics?.roi >= 0 ? 'pos' : 'neg'} />
        <Stat label="PF / Sharpe" value={`${num(c.metrics?.profit_factor)} / ${num(c.metrics?.sharpe)}`} />
        <Stat label="Max DD" value={pct(c.metrics?.max_drawdown)} tone="neg" />
        <Stat label="vs Production" value={num(c.vs_production?.mean_improvement, 3)} sub={c.vs_production?.significance} />
        <Stat label="Rolling 7/30/90d" value={`${num(c.decay?.['7d']?.roi, 2)} / ${num(c.decay?.['30d']?.roi, 2)} / ${num(c.decay?.['90d']?.roi, 2)}`} />
      </div>
      {eq.length > 1 && <div style={{ marginTop: 8 }}><div className="label">Equity curve</div><Sparkline points={eq} /></div>}
      {c.by_regime && <p className="small muted" style={{ marginTop: 6 }}>By regime: {Object.entries(c.by_regime).map(([rg, v]) => `${rg}: ${v.improvement_pct}%`).join(' · ')}</p>}
    </div>
  )
}

export function RegimeHeatmap({ data }) {
  if (!data?.challengers?.length) return <Empty>No regime performance yet.</Empty>
  const cell = (v) => {
    if (v == null) return <td className="right muted">·</td>
    const tone = v >= 3 ? '#1f6f43' : v >= 0 ? '#33415c' : '#6f2330'
    return <td className="right" style={{ background: tone }}>{num(v, 1)}%</td>
  }
  return (
    <div className="table-wrap">
      <table data-testid="regime-heatmap">
        <thead><tr><th>Challenger</th>{data.regimes.map((r) => <th key={r} className="right small">{r.slice(0, 8)}</th>)}</tr></thead>
        <tbody>
          {data.challengers.slice(0, 24).map((c) => (
            <tr key={c.key} data-testid="regime-row"><td className="small">{c.name}</td>
              {data.regimes.map((r) => <CellWrap key={r} v={c.by_regime[r]} render={cell} />)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
function CellWrap({ v, render }) { return render(v) }

export function RecommendationList({ rows }) {
  if (!rows?.length) return <Empty>No recommendations yet — challengers need more trades to reach significance.</Empty>
  return (
    <div data-testid="rec-list">
      {rows.map((r, i) => (
        <div key={i} className="card" data-testid="rec-row" style={{ marginBottom: 6 }}>
          <div className="page-head" style={{ marginBottom: 2 }}>
            <span className="badge sharp">{r.category}</span>
            <span className={`badge ${SIG_TONE[r.significance] || 'neutral'}`}>{r.significance}</span>
          </div>
          <div className="small">{r.text}</div>
        </div>
      ))}
    </div>
  )
}

// ---- main component ------------------------------------------------------
export default function ChallengerLab() {
  const [sub, setSub] = useState('Overview')
  const [dash, setDash] = useState(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)
  const [data, setData] = useState({})
  const [loading, setLoading] = useState(false)
  const [drill, setDrill] = useState(null)       // {type:'exp'|'ch', data}

  const loadDash = useCallback(() => api.pcDashboard().then((r) => setDash(r?.detail || r)).catch((e) => setMsg(e.message)), [])
  useEffect(() => { loadDash() }, [loadDash])

  const loaders = {
    'Experiment Feed': () => api.pcExperiments(),
    Challengers: () => api.pcChallengers(),
    Timing: () => api.pcComparison('timing'),
    Sizing: () => api.pcComparison('sizing'),
    Confidence: () => api.pcComparison('confidence'),
    Consensus: () => api.pcComparison('consensus'),
    'Regime Performance': () => api.pcRegimePerformance(),
    Recommendations: () => api.pcRecommendations(),
    'Nightly Reviews': () => api.pcNightlyReviews(),
  }
  useEffect(() => {
    const fn = loaders[sub]
    if (!fn || data[sub]) return
    setLoading(true)
    fn().then((r) => setData((p) => ({ ...p, [sub]: r?.detail || r }))).catch((e) => setMsg(e.message)).finally(() => setLoading(false))
  }, [sub]) // eslint-disable-line react-hooks/exhaustive-deps

  const openExperiment = (id) => { setDrill('loading'); api.pcExperiment(id).then((r) => setDrill({ type: 'exp', data: r?.detail || r })).catch((e) => { setMsg(e.message); setDrill(null) }) }
  const openChallenger = (key) => { setDrill('loading'); api.pcChallenger(key).then((r) => setDrill({ type: 'ch', data: r?.detail || r })).catch((e) => { setMsg(e.message); setDrill(null) }) }

  const runBatch = async () => {
    setBusy(true); setMsg('Running paper-challenger cycle…')
    try {
      const r = await api.pcRun(150); const d = r?.detail || {}
      setMsg(`Cycle done: ${d.experiments?.total_experiments ?? 0} experiments (+${d.experiments?.new_experiments ?? 0}), champion ${d.champion}. ${d.nightly_summary || ''}`)
      setData({}); await loadDash()
    } catch (e) { setMsg(e.message) } finally { setBusy(false) }
  }

  const d = data[sub]
  return (
    <div>
      <div className="page-head" style={{ marginBottom: 8 }}>
        <div>
          <h2 style={{ margin: 0 }}>Paper Challenger Lab <span className="badge sharp">V1</span></h2>
          <p className="muted small" style={{ margin: '2px 0 0' }}>Every production decision becomes an A/B experiment — paper challengers compete on timing, sizing, confidence, consensus & strategy · never live</p>
        </div>
        <button data-testid="run-challenger" onClick={runBatch} disabled={busy}>{busy ? 'Running…' : '▶ Run Challenger Cycle'}</button>
      </div>
      {msg && <div className="diag-strip">{msg}</div>}

      <div className="live-tabs" style={{ flexWrap: 'wrap' }}>
        {SECTIONS.map((s) => <button key={s} className={`tab ${sub === s ? 'active' : ''}`} onClick={() => { setSub(s); setDrill(null) }}>{s}</button>)}
      </div>

      {drill && drill !== 'loading' && (
        <div style={{ marginTop: 12 }}>
          {drill.type === 'exp' ? <ExperimentDrilldown data={drill.data} onClose={() => setDrill(null)} />
            : <ChallengerDrilldown data={drill.data} onClose={() => setDrill(null)} />}
        </div>
      )}
      {drill === 'loading' && <Loading />}

      <div style={{ marginTop: 12 }}>
        {sub === 'Overview' && (!dash ? <Empty>Run a challenger cycle to populate.</Empty> : (
          <div>
            <div className="cards">
              <Stat label="Experiments" value={dash.experiments ?? 0} />
              <Stat label="Paper portfolios" value={dash.paper_portfolios ?? 0} sub={`${dash.paper_trades ?? 0} paper trades`} />
              <Stat label="Champion" value={dash.champion?.name || '—'} sub={dash.champion?.is_production ? 'production (status quo)' : `+${num(dash.champion?.vs_production?.mean_improvement, 3)} vs prod`} />
              <Stat label="Significant challengers" value={dash.significant ?? 0} />
            </div>
            <h3 style={{ marginTop: 12 }}>Leading challengers</h3>
            <ChallengerTable rows={dash.leading_challengers} onSelect={openChallenger} />
            {dash.last_review && <p className="muted small" style={{ marginTop: 8 }}>Last review {fmt.ago(dash.last_review.created_at)}: {dash.last_review.summary}</p>}
            <p className="muted small">🔬 {dash.safety}</p>
          </div>
        ))}
        {sub !== 'Overview' && loading && !d && <Loading />}
        {sub === 'Experiment Feed' && d && <ExperimentFeed rows={d.experiments} onSelect={openExperiment} />}
        {sub === 'Challengers' && d && <ChallengerTable rows={d.challengers} onSelect={openChallenger} />}
        {['Timing', 'Sizing', 'Confidence', 'Consensus'].includes(sub) && d && <ComparisonTable rows={d.rows} />}
        {sub === 'Regime Performance' && d && <RegimeHeatmap data={d} />}
        {sub === 'Recommendations' && d && <RecommendationList rows={d.recommendations} />}
        {sub === 'Nightly Reviews' && d && (
          !d.reviews?.length ? <Empty>No nightly reviews yet.</Empty> : (
            <div>{d.reviews.map((rv) => (
              <div key={rv.id} className="card" data-testid="pc-review" style={{ marginBottom: 10 }}>
                <div className="page-head" style={{ marginBottom: 4 }}><b>Challenger Review</b><span className="muted small">{fmt.ago(rv.created_at)}</span></div>
                <p className="small">{rv.summary}</p>
                <div className="risk-grid">
                  {['timing_improvement', 'sizing_improvement', 'confidence_improvement', 'consensus_improvement', 'best_strategy_challenger', 'overall_champion'].map((k) => (
                    <div key={k} className="risk-cell"><span>{k.replace(/_/g, ' ')}</span>
                      <b>{typeof rv.report?.[k] === 'object' && rv.report?.[k] ? `${rv.report[k].key} (${rv.report[k].improvement_pct}%)` : (rv.report?.[k] || '—')}</b></div>
                  ))}
                </div>
              </div>
            ))}</div>
          )
        )}
      </div>
    </div>
  )
}
