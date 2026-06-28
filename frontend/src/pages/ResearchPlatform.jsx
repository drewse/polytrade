import { useCallback, useEffect, useState } from 'react'
import { api, fmt } from '../api'
import { Loading, Empty, WalletLink, Stat, Sparkline } from '../components/common.jsx'

const num = (n, d = 2) => (n == null ? '—' : Number(n).toFixed(d))
const pct = (n, d = 1) => (n == null ? '—' : `${(Number(n) * 100).toFixed(d)}%`)

const STATUS_KIND = {
  Research: 'neutral', 'Paper Trading': 'open', Candidate: 'sharp', Champion: 'yes',
  Retired: 'bad', Archived: 'neutral',
}

const SECTIONS = [
  'Overview', 'Strategy Library', 'Tournament', 'Champion Board', 'Evolution',
  'Ensembles', 'Hypotheses', 'Nightly Reviews', 'Experiments',
]

// ---- pure components (exported for tests) --------------------------------
export function StrategyTable({ rows, onSelect }) {
  if (!rows?.length) return <Empty>No strategies yet — run a research cycle.</Empty>
  return (
    <div className="table-wrap">
      <table data-testid="strategy-table">
        <thead><tr>
          <th>Strategy</th><th>Archetype</th><th>Status</th><th className="right">Robust</th>
          <th className="right">ROI</th><th className="right">PF</th><th className="right">Win%</th>
          <th className="right">MaxDD</th><th className="right">Sharpe</th><th className="right">Trades</th>
        </tr></thead>
        <tbody>
          {rows.map((s) => (
            <tr key={s.id} data-testid="strategy-row" className={s.is_champion ? 'highlight' : ''}
              style={{ cursor: onSelect ? 'pointer' : 'default' }} onClick={() => onSelect?.(s.id)}>
              <td><b>{s.is_champion ? '★ ' : ''}{s.name}</b>{s.is_ensemble ? <span className="badge sharp" style={{ marginLeft: 4 }}>ens</span> : ''}</td>
              <td className="small">{s.archetype}</td>
              <td><span className={`badge ${STATUS_KIND[s.status] || 'neutral'}`}>{s.status}</span></td>
              <td className="right"><b>{num(s.robust_score, 1)}</b></td>
              <td className="right">{pct(s.metrics?.roi)}</td>
              <td className="right">{num(s.metrics?.profit_factor)}</td>
              <td className="right">{pct(s.metrics?.win_rate)}</td>
              <td className="right">{pct(s.metrics?.max_drawdown)}</td>
              <td className="right">{num(s.metrics?.sharpe)}</td>
              <td className="right">{s.trades}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function ChampionCard({ champion }) {
  if (!champion) return <Empty>No champion strategy yet.</Empty>
  const m = champion.metrics || {}
  const eq = champion.equity_curve || []
  return (
    <div className="card" data-testid="champion-card">
      <div className="page-head" style={{ marginBottom: 6 }}>
        <h3 style={{ margin: 0 }}>★ {champion.name}</h3>
        <span className="badge yes">Champion · robust {num(champion.robust_score, 1)}</span>
      </div>
      <p className="small muted">{champion.description}</p>
      <div className="cards">
        <Stat label="ROI" value={pct(m.roi)} tone={m.roi >= 0 ? 'pos' : 'neg'} />
        <Stat label="Profit factor" value={num(m.profit_factor)} />
        <Stat label="Win rate" value={pct(m.win_rate)} />
        <Stat label="Expected value" value={num(m.expected_value, 3)} />
        <Stat label="Max drawdown" value={pct(m.max_drawdown)} tone="neg" />
        <Stat label="Sharpe / Calmar" value={`${num(m.sharpe)} / ${num(m.calmar)}`} />
        <Stat label="Consistency" value={pct(m.consistency)} />
        <Stat label="Paper trades" value={m.trades ?? champion.trades} />
      </div>
      {eq.length > 1 && <div style={{ marginTop: 8 }}><div className="label">Equity curve</div><Sparkline points={eq} /></div>}
    </div>
  )
}

export function HypothesisList({ rows }) {
  if (!rows?.length) return <Empty>No hypotheses yet.</Empty>
  return (
    <div data-testid="hypothesis-list">
      {rows.map((h) => (
        <div key={h.id} className="card" data-testid="hypothesis-row" style={{ marginBottom: 6 }}>
          <div className="page-head" style={{ marginBottom: 2 }}>
            <b>{h.text}</b>
            <span className={`badge ${h.status === 'Confirmed' ? 'yes' : h.status === 'Rejected' ? 'bad' : h.status === 'Testing' ? 'open' : 'neutral'}`}>{h.status}</span>
          </div>
          <div className="small muted">{Object.entries(h.evidence || {}).map(([k, v]) => `${k}: ${typeof v === 'number' ? num(v, 3) : v}`).join(' · ')}</div>
        </div>
      ))}
    </div>
  )
}

export function NightlyReviewCard({ review }) {
  if (!review) return <Empty>No nightly reviews yet.</Empty>
  const r = review.report || {}
  const rows = Object.entries(r).filter(([k]) => !k.startsWith('_'))
  return (
    <div className="card" data-testid="nightly-review">
      <div className="page-head" style={{ marginBottom: 4 }}>
        <b>Nightly Review</b><span className="muted small">{fmt.ago(review.created_at)}</span>
      </div>
      <p className="small">{review.summary}</p>
      <div className="risk-grid">
        {rows.map(([k, v]) => (
          <div key={k} className="risk-cell"><span>{k.replace(/^\d+_/, '').replace(/_/g, ' ')}</span>
            <b>{Array.isArray(v) ? (v.length ? v.map((x) => (typeof x === 'object' ? (x.name || x.feature || JSON.stringify(x)) : x)).join(', ') : '—') : String(v)}</b></div>
        ))}
      </div>
    </div>
  )
}

export function StrategyDrilldown({ data, onClose }) {
  if (!data) return null
  const s = data.strategy
  const m = s.metrics || {}
  const eq = s.equity_curve || []
  return (
    <div className="panel" data-testid="strategy-drilldown" style={{ borderLeft: '3px solid #4ea1ff' }}>
      <div className="page-head" style={{ marginBottom: 6 }}>
        <h3 style={{ margin: 0 }}>{s.is_champion ? '★ ' : ''}{s.name} <span className="muted small">v{s.version} · {s.archetype} · {s.status}</span></h3>
        <button className="secondary" onClick={onClose}>✕ Close</button>
      </div>
      <p className="small muted">{s.description}</p>
      <div className="cards">
        <Stat label="Robust score" value={num(s.robust_score, 1)} />
        <Stat label="ROI" value={pct(m.roi)} tone={m.roi >= 0 ? 'pos' : 'neg'} />
        <Stat label="PF" value={num(m.profit_factor)} />
        <Stat label="Win rate" value={pct(m.win_rate)} />
        <Stat label="Max DD" value={pct(m.max_drawdown)} tone="neg" />
        <Stat label="Paper bankroll" value={fmt.usd2(s.paper_bankroll)} />
        <Stat label="Rolling 7/30/90d" value={`${num(m.rolling_7d, 0)} / ${num(m.rolling_30d, 0)} / ${num(m.rolling_90d, 0)}`} />
      </div>
      {eq.length > 1 && <div style={{ marginTop: 8 }}><div className="label">Equity curve</div><Sparkline points={eq} /></div>}
      {s.origin_wallets?.length > 0 && (
        <div className="small" style={{ marginTop: 6 }}>Origin wallets: {s.origin_wallets.slice(0, 6).map((w) => <span key={w} style={{ marginRight: 6 }}><WalletLink address={w} /></span>)}</div>
      )}
      {data.lineage?.children?.length > 0 && (
        <div className="small muted">Children: {data.lineage.children.map((c) => `${c.name} (${num(c.robust_score, 0)})`).join(', ')}</div>
      )}
      <h4 style={{ margin: '12px 0 4px' }}>Paper Trade Explorer (with explanations)</h4>
      {!data.paper_trades?.length ? <Empty>No paper trades.</Empty> : (
        <div className="table-wrap"><table data-testid="drilldown-trades">
          <thead><tr><th>Market</th><th>Action</th><th className="right">Conf</th><th className="right">Edge</th>
            <th className="right">P/L</th><th>Result</th><th>Why</th></tr></thead>
          <tbody>{data.paper_trades.slice(0, 40).map((t) => (
            <tr key={t.id}><td className="small">{(t.market || t.market_id).slice(0, 30)}</td>
              <td><span className={`badge ${t.action === 'NO_TRADE' ? 'neutral' : 'open'}`}>{t.action}</span></td>
              <td className="right">{pct(t.confidence)}</td><td className="right">{num(t.edge, 3)}</td>
              <td className={`right ${t.realized_pnl >= 0 ? 'pos' : 'neg'}`}>{fmt.usd2(t.realized_pnl)}</td>
              <td>{t.won == null ? '—' : t.won ? <span className="pos">✓</span> : <span className="neg">✗</span>}</td>
              <td className="small muted" title={(t.explanation?.reasons || []).join('; ')}>{(t.explanation?.reasons || []).slice(0, 2).join('; ')}</td></tr>
          ))}</tbody>
        </table></div>
      )}
    </div>
  )
}

// ---- main component ------------------------------------------------------
export default function ResearchPlatform() {
  const [sub, setSub] = useState('Overview')
  const [dash, setDash] = useState(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)
  const [data, setData] = useState({})
  const [loading, setLoading] = useState(false)
  const [drill, setDrill] = useState(null)

  const loadDash = useCallback(() => api.researchDashboard()
    .then((r) => setDash(r?.detail || r)).catch((e) => setMsg(e.message)), [])
  useEffect(() => { loadDash() }, [loadDash])

  const loaders = {
    'Strategy Library': () => api.researchStrategies(),
    Tournament: () => api.researchTournament(),
    'Champion Board': () => api.researchChampion(),
    Evolution: () => api.researchExperiments(),
    Ensembles: () => api.researchStrategies(),
    Hypotheses: () => api.researchHypotheses(),
    'Nightly Reviews': () => api.researchNightlyReviews(),
    Experiments: () => api.researchExperiments(),
  }

  useEffect(() => {
    const fn = loaders[sub]
    if (!fn || data[sub]) return
    setLoading(true)
    fn().then((r) => setData((p) => ({ ...p, [sub]: r?.detail || r })))
      .catch((e) => setMsg(e.message)).finally(() => setLoading(false))
  }, [sub]) // eslint-disable-line react-hooks/exhaustive-deps

  const openStrategy = (id) => {
    setDrill('loading')
    api.researchStrategy(id).then((r) => setDrill(r?.detail || r)).catch((e) => { setMsg(e.message); setDrill(null) })
  }

  const runCycle = async () => {
    setBusy(true); setMsg('Running research cycle… (refresh → seed → mutate → replay → tournament → review)')
    try {
      const r = await api.researchCycle(120)
      const d = r?.detail || {}
      setMsg(`Cycle done: champion ${d.champion || 'none'}${d.champion_changed ? ' (changed)' : ''}, ${d.mutations?.mutations_created ?? 0} mutations, ${d.replay?.strategies_replayed ?? 0} strategies replayed. ${d.nightly_summary || ''}`)
      setData({}); await loadDash()
    } catch (e) { setMsg(e.message) } finally { setBusy(false) }
  }

  const d = data[sub]
  return (
    <div>
      <div className="page-head" style={{ marginBottom: 8 }}>
        <div>
          <h2 style={{ margin: 0 }}>Research Platform <span className="badge sharp">V1</span></h2>
          <p className="muted small" style={{ margin: '2px 0 0' }}>Self-improving paper-research — discover · paper-trade · compare · mutate · promote (research only, never live)</p>
        </div>
        <button data-testid="run-cycle" onClick={runCycle} disabled={busy}>{busy ? 'Running…' : '▶ Run Research Cycle'}</button>
      </div>
      {msg && <div className="diag-strip">{msg}</div>}

      <div className="live-tabs" style={{ flexWrap: 'wrap' }}>
        {SECTIONS.map((s) => (
          <button key={s} className={`tab ${sub === s ? 'active' : ''}`} onClick={() => { setSub(s); setDrill(null) }}>{s}</button>
        ))}
      </div>

      {drill && drill !== 'loading' && (
        <div style={{ marginTop: 12 }}><StrategyDrilldown data={drill} onClose={() => setDrill(null)} /></div>
      )}
      {drill === 'loading' && <Loading />}

      <div style={{ marginTop: 12 }}>
        {sub === 'Overview' && (!dash ? <Empty>Run a research cycle to populate the platform.</Empty> : (
          <div>
            <div className="cards">
              <Stat label="Total strategies" value={dash.total_strategies ?? 0} sub={`${dash.ensembles ?? 0} ensembles`} />
              <Stat label="Paper trades" value={dash.paper_trades ?? 0} />
              <Stat label="Champion" value={dash.champion?.name || '—'} sub={dash.champion ? `robust ${num(dash.champion.robust_score, 1)}` : ''} />
              <Stat label="Hypotheses" value={dash.hypotheses_total ?? 0} sub={`${dash.hypotheses_confirmed ?? 0} confirmed`} />
              {Object.entries(dash.by_status || {}).map(([k, v]) => <Stat key={k} label={k} value={v} />)}
            </div>
            <h3 style={{ marginTop: 12 }}>Champion</h3>
            <ChampionCard champion={dash.champion} />
            <h3 style={{ marginTop: 12 }}>Top strategies</h3>
            <StrategyTable rows={dash.top_strategies} onSelect={openStrategy} />
            <p className="muted small" style={{ marginTop: 8 }}>🔬 {dash.safety}</p>
          </div>
        ))}
        {sub !== 'Overview' && loading && !d && <Loading />}
        {sub === 'Strategy Library' && d && <StrategyTable rows={d.strategies} onSelect={openStrategy} />}
        {sub === 'Ensembles' && d && <StrategyTable rows={(d.strategies || []).filter((s) => s.is_ensemble)} onSelect={openStrategy} />}
        {sub === 'Tournament' && d && <StrategyTable rows={d.leaderboard?.map((s) => ({ ...s, metrics: s }))} onSelect={openStrategy} />}
        {sub === 'Champion Board' && d && (
          <div>
            <ChampionCard champion={d.champion} />
            <h3 style={{ marginTop: 12 }}>Champion history</h3>
            {!d.history?.length ? <Empty>No champion changes recorded yet.</Empty> : d.history.map((h, i) => (
              <div key={i} className="diag-strip" style={{ marginBottom: 6 }}><b>{h.title}</b> <span className="muted small">{fmt.ago(h.created_at)}</span></div>
            ))}
          </div>
        )}
        {(sub === 'Evolution' || sub === 'Experiments') && d && (
          !d.experiments?.length ? <Empty>No experiments logged yet.</Empty> : (
            <div className="table-wrap"><table data-testid="experiment-table">
              <thead><tr><th>When</th><th>Kind</th><th>Title</th><th>Detail</th></tr></thead>
              <tbody>{d.experiments.map((e) => (
                <tr key={e.id}><td className="small">{fmt.ago(e.created_at)}</td>
                  <td><span className="badge neutral">{e.kind}</span></td>
                  <td className="small">{e.title}</td>
                  <td className="small muted">{JSON.stringify(e.detail).slice(0, 80)}</td></tr>
              ))}</tbody>
            </table></div>
          )
        )}
        {sub === 'Hypotheses' && d && <HypothesisList rows={d.hypotheses} />}
        {sub === 'Nightly Reviews' && d && (
          !d.reviews?.length ? <Empty>No nightly reviews yet.</Empty> :
            <div>{d.reviews.map((r) => <div key={r.id} style={{ marginBottom: 12 }}><NightlyReviewCard review={r} /></div>)}</div>
        )}
      </div>
    </div>
  )
}
