import { useCallback, useEffect, useState } from 'react'
import { api, fmt } from '../api'
import { Loading, Empty, WalletLink, Stat } from '../components/common.jsx'

const num = (n, d = 2) => (n == null ? '—' : Number(n).toFixed(d))
const pct = (n, d = 1) => (n == null ? '—' : `${(Number(n) * 100).toFixed(d)}%`)

const SECTIONS = [
  'Overview', 'Market Explorer', 'Regime Explorer', 'Heatmaps', 'Wallet Specialization',
  'Strategy Specialization', 'Strategy Decay', 'Originality Network',
  'Counterfactual Simulator', 'Market Recommendations', 'Nightly Reviews',
]

const TREND_ICON = { improving: '▲', stable: '▬', decaying: '▼', broken: '✖' }
const TREND_TONE = { improving: 'pos', stable: 'muted', decaying: 'warn', broken: 'neg' }
const REGIME_TONE = (r) => (/Trend|Breakout|Momentum/.test(r) ? 'pos' : /Volatility|Whipsaw|News/.test(r) ? 'warn' : 'neutral')

// ---- pure components (exported for tests) --------------------------------
export function RegimeBars({ distribution }) {
  const entries = Object.entries(distribution || {})
  if (!entries.length) return <Empty>No regimes classified yet.</Empty>
  const max = Math.max(...entries.map(([, v]) => v), 1)
  return (
    <div data-testid="regime-bars">
      {entries.sort((a, b) => b[1] - a[1]).map(([rg, n]) => (
        <div key={rg} className="risk-cell" style={{ alignItems: 'center', gap: 8 }}>
          <span style={{ width: 150, display: 'inline-block' }}><span className={`badge ${REGIME_TONE(rg)}`}>{rg}</span></span>
          <span style={{ flex: 1, background: '#1b2433', borderRadius: 4, height: 12 }}>
            <span style={{ display: 'block', height: 12, borderRadius: 4, background: '#4ea1ff', width: `${Math.max(3, (n / max) * 100)}%` }} />
          </span>
          <b style={{ width: 36, textAlign: 'right' }}>{n}</b>
        </div>
      ))}
    </div>
  )
}

export function MarketTable({ rows, onSelect }) {
  if (!rows?.length) return <Empty>No markets classified yet — run an intelligence batch.</Empty>
  return (
    <div className="table-wrap">
      <table data-testid="market-table">
        <thead><tr>
          <th>Market</th><th>Regime</th><th>2nd</th><th className="right">Conf</th>
          <th className="right">Net move</th><th className="right">Vol</th><th className="right">Volume</th><th>Outcome</th>
        </tr></thead>
        <tbody>
          {rows.map((m) => (
            <tr key={m.market_id} data-testid="market-row" style={{ cursor: onSelect ? 'pointer' : 'default' }}
              onClick={() => onSelect?.(m.market_id)}>
              <td className="small">{(m.question || m.market_id).slice(0, 40)}</td>
              <td><span className={`badge ${REGIME_TONE(m.regime)}`}>{m.regime}</span></td>
              <td className="small muted">{m.secondary_regime || '—'}</td>
              <td className="right">{pct(m.regime_confidence, 0)}</td>
              <td className="right">{num(m.net_move, 3)}</td>
              <td className="right">{num(m.prob_volatility, 3)}</td>
              <td className="right">{fmt.usd(m.total_volume)}</td>
              <td className="small">{m.final_outcome || (m.resolved ? '—' : 'open')}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function WalletSpecTable({ rows, onWallet }) {
  if (!rows?.length) return <Empty>No wallet specialization yet.</Empty>
  return (
    <div className="table-wrap">
      <table data-testid="wallet-spec-table">
        <thead><tr>
          <th>Wallet</th><th>Cluster</th><th>Best regime</th><th className="right">Spec</th>
          <th>Originality</th><th className="right">Orig score</th><th className="right">Avg stake</th><th>Decay</th>
        </tr></thead>
        <tbody>
          {rows.map((w) => (
            <tr key={w.wallet} data-testid="wallet-spec-row">
              <td className="mono"><WalletLink address={w.wallet} /></td>
              <td className="small">{w.cluster}</td>
              <td><span className={`badge ${REGIME_TONE(w.best_regime)}`}>{w.best_regime || '—'}</span></td>
              <td className="right">{num(w.specialization_score, 2)}</td>
              <td className="small">{w.originality?.role || '—'}</td>
              <td className="right">{num(w.originality_score, 2)}</td>
              <td className="right">{fmt.usd2(w.position_size?.avg_stake)}</td>
              <td className={TREND_TONE[w.decay?.trend]}>{TREND_ICON[w.decay?.trend] || '—'} {w.decay?.trend || ''}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function StrategyHeatmap({ rows, regimes }) {
  if (!rows?.length) return <Empty>No strategy heatmaps yet.</Empty>
  const cols = regimes || [...new Set(rows.flatMap((r) => Object.keys(r.by_regime || {})))].slice(0, 10)
  const cell = (v) => {
    if (!v) return <td className="right muted">·</td>
    const wr = v.win_rate ?? 0
    const tone = wr >= 0.6 ? '#1f6f43' : wr >= 0.5 ? '#33415c' : '#6f2330'
    return <td className="right" style={{ background: tone }} title={`win ${pct(wr)} · roi ${pct(v.roi)} · n${v.trades}`}>{pct(wr, 0)}</td>
  }
  return (
    <div className="table-wrap">
      <table data-testid="heatmap-table">
        <thead><tr><th>Strategy</th>{cols.map((c) => <th key={c} className="right small">{c.slice(0, 8)}</th>)}</tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.strategy_id} data-testid="heatmap-row">
              <td className="small"><b>{r.name}</b></td>
              {cols.map((c) => <Cell key={c} v={(r.by_regime || {})[c]} render={cell} />)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
function Cell({ v, render }) { return render(v) }

export function OriginalityTable({ rows }) {
  if (!rows?.length) return <Empty>No originality graph yet.</Empty>
  return (
    <div className="table-wrap">
      <table data-testid="originality-table">
        <thead><tr><th>Wallet</th><th>Role</th><th className="right">Orig score</th><th className="right">Leads</th>
          <th className="right">Follows</th><th className="right">Reaction delay</th><th className="right">Repeated follow</th></tr></thead>
        <tbody>
          {rows.map((w) => (
            <tr key={w.wallet} data-testid="originality-row">
              <td className="mono"><WalletLink address={w.wallet} /></td>
              <td><span className={`badge ${w.role === 'leader' ? 'yes' : w.role === 'follower' ? 'bad' : 'neutral'}`}>{w.role}</span></td>
              <td className="right">{num(w.originality_score, 2)}</td>
              <td className="right">{w.leads}</td><td className="right">{w.follows}</td>
              <td className="right">{w.avg_reaction_delay_s == null ? '—' : `${num(w.avg_reaction_delay_s, 0)}s`}</td>
              <td className="right">{pct(w.repeated_follow_pct, 0)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function MarketDrilldown({ data, onClose }) {
  if (!data) return null
  const rec = data.recommendation
  return (
    <div className="panel" data-testid="market-drilldown" style={{ borderLeft: '3px solid #4ea1ff' }}>
      <div className="page-head" style={{ marginBottom: 6 }}>
        <h3 style={{ margin: 0 }}>{(data.question || data.market_id).slice(0, 50)}</h3>
        <button className="secondary" onClick={onClose}>✕ Close</button>
      </div>
      <div style={{ marginBottom: 6 }}>
        <span className={`badge ${REGIME_TONE(data.primary_regime)}`}>{data.primary_regime}</span>
        {data.secondary_regime && <span className="badge neutral" style={{ marginLeft: 4 }}>+ {data.secondary_regime}</span>}
        <span className="muted small"> · confidence {pct(data.regime_confidence, 0)} · {data.final_outcome || (data.resolved ? '' : 'open')}</span>
      </div>
      <div className="cards">
        <Stat label="Opening → Closing" value={`${num(data.price?.opening_prob, 2)} → ${num(data.price?.closing_prob, 2)}`} sub={`range ${num(data.price?.range, 2)}`} />
        <Stat label="Net move / Vol" value={`${num(data.price?.net_move, 3)} / ${num(data.price?.prob_volatility, 3)}`} />
        <Stat label="VWAP" value={num(data.price?.vwap, 3)} />
        <Stat label="Total volume" value={fmt.usd(data.volume?.total_volume)} sub={`${data.volume?.trade_count ?? 0} trades`} />
        <Stat label="Consensus participation" value={pct(data.orderflow?.consensus_participation)} />
        <Stat label="Large-wallet participation" value={pct(data.orderflow?.large_wallet_participation)} />
      </div>
      <p className="muted small" style={{ marginTop: 6 }}>Evidence: {Object.entries(data.regime_evidence || {}).filter(([k]) => k !== 'scores').map(([k, v]) => `${k}=${typeof v === 'number' ? num(v, 3) : JSON.stringify(v)}`).join(' · ')}</p>
      {rec && (
        <div style={{ marginTop: 8 }}>
          <h4 style={{ margin: '8px 0 4px' }}>Recommendation (informational only)</h4>
          <p className="small">Best wallets for <b>{rec.regime}</b>: {(rec.best_wallets || []).slice(0, 5).map((w) => (
            <span key={w.wallet} style={{ marginRight: 8 }}><WalletLink address={w.wallet} /> ({pct(w.win_rate, 0)})</span>
          )) || '—'}</p>
          <p className="small muted">Best strategies: {(rec.best_strategies || []).map((s) => `${s.name} (${pct(s.roi)})`).join(', ') || '—'} · expected edge {pct(rec.expected_edge)} · confidence {pct(rec.research_confidence, 0)}</p>
        </div>
      )}
    </div>
  )
}

// ---- main component ------------------------------------------------------
export default function MarketIntel() {
  const [sub, setSub] = useState('Overview')
  const [dash, setDash] = useState(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)
  const [data, setData] = useState({})
  const [loading, setLoading] = useState(false)
  const [drill, setDrill] = useState(null)

  const loadDash = useCallback(() => api.miDashboard().then((r) => setDash(r?.detail || r)).catch((e) => setMsg(e.message)), [])
  useEffect(() => { loadDash() }, [loadDash])

  const loaders = {
    'Market Explorer': () => api.miMarkets(),
    'Regime Explorer': () => Promise.all([api.miRegimes(), api.miLeaderboards()]).then(([a, b]) => ({ ...(a?.detail || a), ...(b?.detail || b) })),
    Heatmaps: () => api.miStrategySpecialization(),
    'Wallet Specialization': () => api.miWalletSpecialization(),
    'Strategy Specialization': () => api.miStrategySpecialization(),
    'Strategy Decay': () => api.miDecay(),
    'Originality Network': () => api.miOriginality(),
    'Counterfactual Simulator': () => api.miCounterfactual(),
    'Market Recommendations': () => api.miRecommendations(),
    'Nightly Reviews': () => api.miNightlyReviews(),
  }
  useEffect(() => {
    const fn = loaders[sub]
    if (!fn || data[sub]) return
    setLoading(true)
    fn().then((r) => setData((p) => ({ ...p, [sub]: r?.detail || r }))).catch((e) => setMsg(e.message)).finally(() => setLoading(false))
  }, [sub]) // eslint-disable-line react-hooks/exhaustive-deps

  const openMarket = (id) => { setDrill('loading'); api.miMarket(id).then((r) => setDrill(r?.detail || r)).catch((e) => { setMsg(e.message); setDrill(null) }) }

  const runBatch = async () => {
    setBusy(true); setMsg('Running market-intelligence batch…')
    try {
      const r = await api.miRun(150); const d = r?.detail || {}
      setMsg(`Batch done: ${d.profiles?.profiles ?? 0} markets classified, ${d.wallet_regime?.wallets ?? 0} wallets, ${d.strategy_regime?.strategies ?? 0} strategies. ${d.nightly_summary || ''}`)
      setData({}); await loadDash()
    } catch (e) { setMsg(e.message) } finally { setBusy(false) }
  }

  const d = data[sub]
  return (
    <div>
      <div className="page-head" style={{ marginBottom: 8 }}>
        <div>
          <h2 style={{ margin: 0 }}>Market Intelligence <span className="badge sharp">Regime Engine V1</span></h2>
          <p className="muted small" style={{ margin: '2px 0 0' }}>Explains markets: regime classification + which wallets/strategies dominate each environment · read-only</p>
        </div>
        <button data-testid="run-intel" onClick={runBatch} disabled={busy}>{busy ? 'Running…' : '▶ Run Intelligence Batch'}</button>
      </div>
      {msg && <div className="diag-strip">{msg}</div>}

      <div className="live-tabs" style={{ flexWrap: 'wrap' }}>
        {SECTIONS.map((s) => <button key={s} className={`tab ${sub === s ? 'active' : ''}`} onClick={() => { setSub(s); setDrill(null) }}>{s}</button>)}
      </div>

      {drill && drill !== 'loading' && <div style={{ marginTop: 12 }}><MarketDrilldown data={drill} onClose={() => setDrill(null)} /></div>}
      {drill === 'loading' && <Loading />}

      <div style={{ marginTop: 12 }}>
        {sub === 'Overview' && (!dash ? <Empty>Run an intelligence batch to populate.</Empty> : (
          <div>
            <div className="cards">
              <Stat label="Markets classified" value={dash.markets_classified ?? 0} />
              <Stat label="Regimes discovered" value={dash.regimes_discovered ?? 0} />
              <Stat label="Wallets profiled" value={dash.wallets_profiled ?? 0} />
              <Stat label="Strategies profiled" value={dash.strategies_profiled ?? 0} />
              <Stat label="Best counterfactual" value={dash.counterfactual ? `${dash.counterfactual.optimal_shift_s}s` : '—'} sub={dash.counterfactual ? `Δ ${num(dash.counterfactual.expected_improvement, 3)}` : ''} />
            </div>
            <h3 style={{ marginTop: 12 }}>Regime distribution</h3>
            <RegimeBars distribution={dash.regime_distribution} />
            <h3 style={{ marginTop: 12 }}>Leader wallets</h3>
            {!dash.leader_wallets?.length ? <Empty>—</Empty> : dash.leader_wallets.map((l, i) => (
              <div key={i} className="small mono"><WalletLink address={l.wallet} /> · {l.role} · orig {num(l.originality, 2)}</div>
            ))}
            {dash.last_review && <p className="muted small" style={{ marginTop: 8 }}>Last review {fmt.ago(dash.last_review.created_at)}: {dash.last_review.summary}</p>}
            <p className="muted small">🔬 {dash.safety}</p>
          </div>
        ))}
        {sub !== 'Overview' && loading && !d && <Loading />}
        {sub === 'Market Explorer' && d && <MarketTable rows={d.markets} onSelect={openMarket} />}
        {sub === 'Regime Explorer' && d && (
          <div>
            <RegimeBars distribution={Object.fromEntries((d.regimes || []).map((r) => [r.regime, r.count]))} />
            <h3 style={{ marginTop: 12 }}>Best wallets per regime</h3>
            {Object.entries(d.leaderboards || {}).filter(([, v]) => v.length).map(([rg, wl]) => (
              <div key={rg} style={{ marginBottom: 6 }}>
                <span className={`badge ${REGIME_TONE(rg)}`}>{rg}</span>{' '}
                {wl.slice(0, 5).map((w) => <span key={w.wallet} className="small" style={{ marginRight: 8 }}><WalletLink address={w.wallet} /> {pct(w.win_rate, 0)}</span>)}
              </div>
            ))}
          </div>
        )}
        {sub === 'Heatmaps' && d && <StrategyHeatmap rows={d.strategies} />}
        {sub === 'Wallet Specialization' && d && <WalletSpecTable rows={d.wallets} />}
        {sub === 'Strategy Specialization' && d && <StrategyHeatmap rows={d.strategies} />}
        {sub === 'Strategy Decay' && d && (
          <div>
            <h3>Strategies</h3>
            {!d.strategies?.length ? <Empty>No decay data.</Empty> : (
              <div className="table-wrap"><table data-testid="decay-table"><thead><tr><th>Entity</th><th>Trend</th><th className="right">Conf</th><th className="right">Win 7d</th><th className="right">Win life</th></tr></thead>
                <tbody>{d.strategies.map((r, i) => (
                  <tr key={i} data-testid="decay-row"><td className="small">{r.entity}</td>
                    <td className={TREND_TONE[r.trend]}>{TREND_ICON[r.trend] || '—'} {r.trend}</td>
                    <td className="right">{pct(r.trend_confidence, 0)}</td><td className="right">{pct(r.win_rate_7d, 0)}</td><td className="right">{pct(r.win_rate_lifetime, 0)}</td></tr>
                ))}</tbody></table></div>
            )}
            <h3 style={{ marginTop: 12 }}>Wallets</h3>
            {!d.wallets?.length ? <Empty>—</Empty> : d.wallets.slice(0, 20).map((r, i) => (
              <div key={i} className="small"><span className={TREND_TONE[r.trend]}>{TREND_ICON[r.trend]} {r.trend}</span> · <span className="mono"><WalletLink address={r.entity} /></span> · 7d {pct(r.win_rate_7d, 0)}</div>
            ))}
          </div>
        )}
        {sub === 'Originality Network' && d && <OriginalityTable rows={d.wallets} />}
        {sub === 'Counterfactual Simulator' && d && (
          !d.results?.length ? <Empty>No counterfactual results yet.</Empty> : (
            <div>
              {d.results.slice(0, 1).map((r, i) => (
                <div key={i}>
                  <div className="cards">
                    <Stat label="Trades tested" value={r.trades_tested} />
                    <Stat label="Optimal entry shift" value={`${r.optimal_shift_s}s`} />
                    <Stat label="Expected improvement" value={num(r.expected_improvement, 4)} tone={r.expected_improvement > 0 ? 'pos' : 'neg'} />
                  </div>
                  <h3>Timing sensitivity (avg P/L delta by entry shift)</h3>
                  <div className="table-wrap"><table data-testid="cf-table"><thead><tr><th>Shift</th><th className="right">Avg P/L delta</th></tr></thead>
                    <tbody>{Object.entries(r.timing_sensitivity || {}).sort((a, b) => Number(a[0]) - Number(b[0])).map(([s, v]) => (
                      <tr key={s}><td>{s > 0 ? `+${s}s (later)` : `${s}s (earlier)`}</td><td className={`right ${v > 0 ? 'pos' : 'neg'}`}>{num(v, 4)}</td></tr>
                    ))}</tbody></table></div>
                </div>
              ))}
            </div>
          )
        )}
        {sub === 'Market Recommendations' && d && (
          !d.recommendations?.length ? <Empty>No recommendations yet.</Empty> : (
            <div className="table-wrap"><table data-testid="rec-table"><thead><tr><th>Market</th><th>Regime</th><th>Best wallets</th><th className="right">Edge</th><th className="right">Conf</th></tr></thead>
              <tbody>{d.recommendations.map((r) => (
                <tr key={r.market_id} data-testid="rec-row"><td className="small">{(r.market || r.market_id).slice(0, 30)}</td>
                  <td><span className={`badge ${REGIME_TONE(r.regime)}`}>{r.regime}</span></td>
                  <td className="small mono">{(r.best_wallets || []).slice(0, 3).map((w) => <span key={w.wallet} style={{ marginRight: 4 }}><WalletLink address={w.wallet} /></span>)}</td>
                  <td className="right">{pct(r.expected_edge)}</td><td className="right">{pct(r.research_confidence, 0)}</td></tr>
              ))}</tbody></table></div>
          )
        )}
        {sub === 'Nightly Reviews' && d && (
          !d.reviews?.length ? <Empty>No nightly reviews yet.</Empty> : (
            <div>{d.reviews.map((rv) => (
              <div key={rv.id} className="card" data-testid="mi-review" style={{ marginBottom: 10 }}>
                <div className="page-head" style={{ marginBottom: 4 }}><b>Market-Intel Review</b><span className="muted small">{fmt.ago(rv.created_at)}</span></div>
                <p className="small">{rv.summary}</p>
                <div className="risk-grid">
                  {Object.entries(rv.report || {}).filter(([k]) => !['top_wallets_by_regime', 'top_strategies_by_regime'].includes(k)).map(([k, v]) => (
                    <div key={k} className="risk-cell"><span>{k.replace(/_/g, ' ')}</span>
                      <b>{Array.isArray(v) ? (v.length ? `${v.length}` : '—') : typeof v === 'object' && v ? Object.keys(v).length : String(v)}</b></div>
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
