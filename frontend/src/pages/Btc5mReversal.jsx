import { useCallback, useEffect, useState } from 'react'
import { api, fmt } from '../api'
import { Loading, Empty, WalletLink, Stat } from '../components/common.jsx'

const num = (n, d = 2) => (n == null ? '—' : Number(n).toFixed(d))
const pct = (n, d = 1) => (n == null ? '—' : `${(Number(n) * 100).toFixed(d)}%`)

const SECTIONS = [
  'Overview', 'Dataset', 'Wallet IQ', 'Wallet Profiles', 'Wallet Clusters',
  'Strategy Lab', 'Consensus Graph', 'Feature Importance', 'Shadow Strategy',
  'Model Performance', 'Leaderboard', 'Research Notes',
]

// ---- pure presentational components (exported for tests) ------------------
export function FeatureBar({ items }) {
  if (!items?.length) return <Empty>No feature importance yet — train a model.</Empty>
  const max = Math.max(...items.map((i) => i.importance || 0), 0.0001)
  return (
    <div data-testid="feature-bars">
      {items.map((it) => (
        <div key={it.feature} className="risk-cell" style={{ alignItems: 'center', gap: 8 }}>
          <span style={{ width: 170, display: 'inline-block' }}>{it.feature}</span>
          <span style={{ flex: 1, background: '#1b2433', borderRadius: 4, height: 12, display: 'inline-block' }}>
            <span style={{ display: 'block', height: 12, borderRadius: 4, background: '#4ea1ff',
              width: `${Math.max(2, (it.importance / max) * 100)}%` }} />
          </span>
          <b style={{ width: 56, textAlign: 'right' }}>{num(it.importance, 3)}</b>
        </div>
      ))}
    </div>
  )
}

export function ModelLeaderboard({ rows }) {
  if (!rows?.length) return <Empty>No models trained yet — run a research batch.</Empty>
  return (
    <div className="table-wrap">
      <table data-testid="model-table">
        <thead><tr>
          <th>Model</th><th className="right">Accuracy</th><th className="right">Precision</th>
          <th className="right">Recall</th><th className="right">F1</th><th className="right">CV F1</th>
          <th className="right">Overfit gap</th><th className="right">Train/Test</th><th>Champion</th>
        </tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.name} data-testid="model-row" className={r.is_champion ? 'highlight' : ''}>
              <td><b>{r.name}</b></td>
              <td className="right">{pct(r.accuracy)}</td>
              <td className="right">{pct(r.precision)}</td>
              <td className="right">{pct(r.recall)}</td>
              <td className="right"><b>{num(r.f1, 3)}</b></td>
              <td className="right">{num(r.cv_f1, 3)}</td>
              <td className="right">{r.overfit_gap == null ? '—' : num(r.overfit_gap, 3)}</td>
              <td className="right small">{r.n_train}/{r.n_test}</td>
              <td>{r.is_champion ? <span className="badge yes">★ champion</span> : ''}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function WalletIqCard({ card }) {
  return (
    <div className="card" data-testid="iq-card" style={{ minWidth: 240 }}>
      <div className="page-head" style={{ marginBottom: 6 }}>
        <b className="mono"><WalletLink address={card.wallet} /></b>
        <span className="badge sharp">IQ {card.copy_confidence}</span>
      </div>
      <div className="small"><b>Strategy:</b> {card.strategy}</div>
      <div className="small"><b>Avg entry:</b> {card.average_entry}</div>
      <div className="small"><b>Avg hold:</b> {card.average_hold}</div>
      <div className="small"><b>Confidence:</b> {card.average_confidence}</div>
      <div className="small pos"><b>Strength:</b> {card.strength}</div>
      <div className="small neg"><b>Weakness:</b> {card.weakness}</div>
      <div className="small muted">ROI {pct(card.roi)} · PF {num(card.profit_factor)} · WR {pct(card.win_rate)}</div>
    </div>
  )
}

export function ConsensusView({ data }) {
  if (!data) return <Empty>Run a research batch to compute consensus.</Empty>
  return (
    <div>
      <h3>Consensus groups (profitable together)</h3>
      {!data.consensus_groups?.length ? <Empty>No consensus groups detected yet.</Empty> : (
        <div className="cards">
          {data.consensus_groups.map((g, i) => (
            <div key={i} className="card" data-testid="consensus-group">
              <div><b>Group of {g.size}</b> · <span className="pos">{g.profitable_together_pct}% together</span></div>
              <div className="small mono">{g.wallets.map((w) => <span key={w} style={{ marginRight: 6 }}><WalletLink address={w} /></span>)}</div>
            </div>
          ))}
        </div>
      )}
      <h3 style={{ marginTop: 16 }}>Leader → follower (time-lag)</h3>
      {!data.followers?.length ? <Empty>No leader/follower relationships yet.</Empty> : (
        <div className="table-wrap">
          <table data-testid="follower-table">
            <thead><tr><th>Follower</th><th>Follows leader</th><th className="right">Lag (s)</th><th className="right">Agreement</th></tr></thead>
            <tbody>
              {data.followers.map((f, i) => (
                <tr key={i} data-testid="follower-row">
                  <td className="mono"><WalletLink address={f.wallet} /></td>
                  <td className="mono"><WalletLink address={f.follows} /></td>
                  <td className="right">{num(f.lag_s, 1)}</td>
                  <td className="right">{pct(f.agreement)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data.independent?.length > 0 && (
        <p className="muted small">Independent alpha (no strong links): {data.independent.length} wallet(s).</p>
      )}
    </div>
  )
}

export function Dashboard({ d }) {
  if (!d) return <Empty>No dashboard data — run a research batch.</Empty>
  return (
    <div>
      <div className="cards">
        <Stat label="Wallets analyzed" value={d.wallet_count ?? 0} sub={`${d.profitable_wallets ?? 0} profitable`} />
        <Stat label="Trades indexed" value={d.trade_count ?? 0} />
        <Stat label="Markets indexed" value={d.markets_indexed ?? 0} />
        <Stat label="Models trained" value={d.models_trained ?? 0} />
        <Stat label="Best model" value={d.best_model || '—'} sub={d.best_model_accuracy == null ? '' : `acc ${pct(d.best_model_accuracy)} · F1 ${num(d.best_model_f1, 3)}`} />
        <Stat label="Largest cluster" value={d.largest_cluster ? `${d.largest_cluster.cluster} (${d.largest_cluster.count})` : '—'} />
        <Stat label="Consensus groups" value={d.consensus_opportunities?.length ?? 0} />
        <Stat label="Shadow hit rate" value={pct(d.shadow_performance?.hit_rate)} sub={`${d.shadow_performance?.resolved ?? 0} resolved`} />
      </div>
      <div className="cards" style={{ marginTop: 12 }}>
        <div className="card" style={{ flex: 1, minWidth: 280 }}>
          <div className="label">Top features (champion)</div>
          <FeatureBar items={(d.top_features || []).slice(0, 6)} />
        </div>
        <div className="card" style={{ flex: 1, minWidth: 280 }}>
          <div className="label">Leader wallets</div>
          {!d.leader_wallets?.length ? <Empty>—</Empty> : d.leader_wallets.map((l, i) => (
            <div key={i} className="small mono"><WalletLink address={l.wallet} /> · {l.links} links · lead {num(l.avg_lead_s, 1)}s</div>
          ))}
        </div>
        <div className="card" style={{ flex: 1, minWidth: 280 }}>
          <div className="label">Latest reconstructed signals</div>
          {!d.latest_signals?.length ? <Empty>—</Empty> : d.latest_signals.slice(0, 6).map((s) => (
            <div key={s.id} className="small">{s.action} · conf {pct(s.confidence)} · <span className="muted">{(s.market || '').slice(0, 28)}</span></div>
          ))}
        </div>
      </div>
      <p className="muted small" style={{ marginTop: 10 }}>🔬 {d.safety}</p>
    </div>
  )
}

// ---- main page -----------------------------------------------------------
export default function Btc5mReversal() {
  const [tab, setTab] = useState('Overview')
  const [dash, setDash] = useState(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)
  const [tabData, setTabData] = useState({})
  const [tabLoading, setTabLoading] = useState(false)

  const loadDash = useCallback(() => api.btc5mDashboard()
    .then((r) => setDash(r?.detail || r)).catch((e) => setMsg(e.message)), [])
  useEffect(() => { loadDash() }, [loadDash])

  const loaders = {
    Dataset: () => api.btc5mDataset(),
    'Wallet IQ': () => api.btc5mWalletIq(60),
    'Wallet Profiles': () => api.btc5mWalletProfiles(200),
    'Wallet Clusters': () => api.btc5mClusters(),
    'Strategy Lab': () => api.btc5mStrategyLab('global'),
    'Consensus Graph': () => api.btc5mConsensus(),
    'Feature Importance': () => api.btc5mFeatureImportance('global'),
    'Shadow Strategy': () => api.btc5mShadow(50),
    'Model Performance': () => api.btc5mShadow(50),
    Leaderboard: () => api.btc5mModels('global'),
    'Research Notes': () => api.btc5mResearchNotes(40),
  }

  useEffect(() => {
    const fn = loaders[tab]
    if (!fn || tabData[tab]) return
    setTabLoading(true)
    fn().then((r) => setTabData((prev) => ({ ...prev, [tab]: r?.detail || r })))
      .catch((e) => setMsg(e.message)).finally(() => setTabLoading(false))
  }, [tab]) // eslint-disable-line react-hooks/exhaustive-deps

  const runBatch = async () => {
    setBusy(true); setMsg('Running research batch…')
    try {
      const r = await api.btc5mRefresh(60)
      const d = r?.detail || {}
      setMsg(`Batch done: ${d.dataset?.markets_indexed ?? 0} markets, ${d.dataset?.trades_indexed ?? 0} new trades, ${d.fingerprint?.profiles ?? 0} wallets, champion ${d.champion || 'none'} (F1 ${num(d.champion_f1, 3)}).`)
      setTabData({})            // invalidate cached tabs
      await loadDash()
    } catch (e) { setMsg(e.message) } finally { setBusy(false) }
  }

  const td = tabData[tab]
  return (
    <div className="panel">
      <div className="page-head" style={{ marginBottom: 8 }}>
        <div>
          <h1 style={{ margin: 0 }}>BTC 5M Reversal <span className="badge sharp">Research</span></h1>
          <p className="muted small" style={{ margin: '4px 0 0' }}>
            Reverse-engineering profitable BTC 5-minute traders · read-only analytics, never trades
          </p>
        </div>
        <button data-testid="run-batch" onClick={runBatch} disabled={busy}>
          {busy ? 'Running…' : '▶ Run Research Batch'}
        </button>
      </div>
      {msg && <div className="diag-strip">{msg}</div>}

      <div className="live-tabs" style={{ flexWrap: 'wrap' }}>
        {SECTIONS.map((s) => (
          <button key={s} className={`tab ${tab === s ? 'active' : ''}`} onClick={() => setTab(s)}>{s}</button>
        ))}
      </div>

      <div style={{ marginTop: 12 }}>
        {tab === 'Overview' && <Dashboard d={dash} />}
        {tab !== 'Overview' && tabLoading && !td && <Loading />}
        {tab === 'Dataset' && td && (
          <div>
            <div className="cards">
              <Stat label="Markets indexed" value={td.markets_indexed ?? 0} sub={`${td.markets_resolved ?? 0} resolved`} />
              <Stat label="Trades indexed" value={td.trades_indexed ?? 0} />
              <Stat label="Wallets" value={td.wallets ?? 0} />
              <Stat label="Features / trade" value={td.feature_names?.length ?? 0} />
            </div>
            <h3>Recent markets</h3>
            {!td.recent_markets?.length ? <Empty>No BTC 5m markets indexed yet. Run a research batch.</Empty> : (
              <div className="table-wrap"><table data-testid="dataset-table">
                <thead><tr><th>Market</th><th>Resolved</th><th>Outcome</th><th className="right">Trades</th><th className="right">Wallets</th><th className="right">Volume</th></tr></thead>
                <tbody>{td.recent_markets.map((m) => (
                  <tr key={m.market_id}><td className="small">{(m.question || m.market_id).slice(0, 48)}</td>
                    <td>{m.resolved ? '✓' : '—'}</td><td>{m.final_outcome || '—'}</td>
                    <td className="right">{m.trade_count}</td><td className="right">{m.wallet_count}</td>
                    <td className="right">{fmt.usd(m.volume)}</td></tr>
                ))}</tbody>
              </table></div>
            )}
          </div>
        )}
        {tab === 'Wallet IQ' && td && (
          !td.cards?.length ? <Empty>No Wallet IQ profiles yet — run a research batch.</Empty> :
            <div className="cards">{td.cards.map((c) => <WalletIqCard key={c.wallet} card={c} />)}</div>
        )}
        {tab === 'Wallet Profiles' && td && (
          !td.profiles?.length ? <Empty>No wallet profiles yet.</Empty> : (
            <div className="table-wrap"><table data-testid="profiles-table">
              <thead><tr><th>Wallet</th><th>Cluster</th><th className="right">Trades</th><th className="right">ROI</th>
                <th className="right">PF</th><th className="right">Win%</th><th className="right">P/L</th><th className="right">Avg size</th><th>Profitable</th></tr></thead>
              <tbody>{td.profiles.map((p) => (
                <tr key={p.wallet} data-testid="profile-row"><td className="mono"><WalletLink address={p.wallet} /></td>
                  <td>{p.cluster} <span className="muted small">{pct(p.cluster_confidence, 0)}</span></td>
                  <td className="right">{p.trade_count}</td><td className="right">{pct(p.roi)}</td>
                  <td className="right">{num(p.profit_factor)}</td><td className="right">{pct(p.win_rate)}</td>
                  <td className={`right ${p.realized_pnl >= 0 ? 'pos' : 'neg'}`}>{fmt.usd2(p.realized_pnl)}</td>
                  <td className="right">{fmt.usd2(p.avg_trade_size)}</td>
                  <td>{p.profitable ? <span className="badge yes">yes</span> : <span className="badge neutral">no</span>}</td></tr>
              ))}</tbody>
            </table></div>
          )
        )}
        {tab === 'Wallet Clusters' && td && (
          !td.clusters?.length ? <Empty>No clusters yet.</Empty> : (
            <div className="cards">{td.clusters.map((c) => (
              <div key={c.cluster} className="card" data-testid="cluster-card">
                <div className="page-head" style={{ marginBottom: 4 }}><b>{c.cluster}</b><span className="badge sharp">{c.count}</span></div>
                <div className="small">avg confidence {pct(c.avg_confidence, 0)} · avg ROI {pct(c.avg_roi)} · {c.profitable} profitable</div>
                <div className="small mono" style={{ marginTop: 4 }}>{c.wallets.slice(0, 6).map((w) => <span key={w} style={{ marginRight: 6 }}><WalletLink address={w} /></span>)}</div>
              </div>
            ))}</div>
          )
        )}
        {tab === 'Strategy Lab' && td && (
          <div>
            <p className="muted small">Predicting trade direction from reconstructed pre-entry market features. Train/test split + cross-validation; champion picked by held-out F1.</p>
            <ModelLeaderboard rows={td.leaderboard} />
            <h3 style={{ marginTop: 16 }}>Champion feature importance</h3>
            <FeatureBar items={td.feature_importance} />
          </div>
        )}
        {tab === 'Consensus Graph' && td && <ConsensusView data={td} />}
        {tab === 'Feature Importance' && td && <FeatureBar items={td.feature_importance} />}
        {(tab === 'Shadow Strategy' || tab === 'Model Performance') && td && (
          <div>
            <div className="cards">
              <Stat label="Total signals" value={td.performance?.total_signals ?? 0} />
              <Stat label="Actionable" value={td.performance?.actionable ?? 0} sub={`${td.performance?.no_trade ?? 0} no-trade`} />
              <Stat label="Resolved" value={td.performance?.resolved ?? 0} />
              <Stat label="Hit rate" value={pct(td.performance?.hit_rate)} />
              <Stat label="Paper P/L" value={num(td.performance?.paper_pnl, 3)} tone={td.performance?.paper_pnl >= 0 ? 'pos' : 'neg'} />
            </div>
            <h3>Shadow signals (paper only — never real orders)</h3>
            {!td.signals?.length ? <Empty>No shadow signals yet — run a research batch.</Empty> : (
              <div className="table-wrap"><table data-testid="shadow-table">
                <thead><tr><th>Market</th><th>Action</th><th className="right">Confidence</th><th className="right">Edge</th>
                  <th className="right">P(yes)</th><th>Model</th><th className="right">Support</th><th>Result</th></tr></thead>
                <tbody>{td.signals.map((s) => (
                  <tr key={s.id} data-testid="shadow-row"><td className="small">{(s.market || s.market_id).slice(0, 36)}</td>
                    <td><span className={`badge ${s.action === 'NO_TRADE' ? 'neutral' : 'open'}`}>{s.action}</span></td>
                    <td className="right">{pct(s.confidence)}</td><td className="right">{num(s.expected_edge, 3)}</td>
                    <td className="right">{num(s.predicted_probability, 2)}</td><td className="small">{s.model}</td>
                    <td className="right">{s.supporting_wallets?.length ?? 0}</td>
                    <td>{s.correct == null ? '—' : s.correct ? <span className="pos">✓</span> : <span className="neg">✗</span>}</td></tr>
                ))}</tbody>
              </table></div>
            )}
          </div>
        )}
        {tab === 'Leaderboard' && td && <ModelLeaderboard rows={td.leaderboard} />}
        {tab === 'Research Notes' && td && (
          !td.notes?.length ? <Empty>No research notes yet — run a research batch.</Empty> : (
            <div>{td.notes.map((n) => (
              <div key={n.id} className="diag-strip" style={{ marginBottom: 6 }}>
                <b>{n.kind === 'promotion' ? '★ ' : ''}{n.title}</b> <span className="muted small">{fmt.ago(n.created_at)}</span>
                <div className="small">{n.body}</div>
              </div>
            ))}</div>
          )
        )}
      </div>
    </div>
  )
}
