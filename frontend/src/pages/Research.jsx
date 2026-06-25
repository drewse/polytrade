import { useState } from 'react'
import { api, fmt } from '../api'
import { Loading, PageHead, PnL, Stat, useData } from '../components/common.jsx'

const num = (n, d = 3) => (n ?? 0).toFixed(d)
const pct = (n) => fmt.pct((n ?? 0) * 100)

function Progress({ value, target, label }) {
  const f = Math.min(1, (value || 0) / target)
  return (
    <div className="expo-row">
      <div className="expo-label">{label}</div>
      <div className="expo-bar"><div className="expo-fill" style={{ width: `${f * 100}%` }} /></div>
      <div className="expo-val">{(value || 0).toLocaleString()} / {target.toLocaleString()}</div>
    </div>
  )
}

function Benchmark() {
  const { data, loading } = useData(api.researchBenchmark)
  if (loading) return <Loading />
  if (!data || data.insufficient_data && data.n === 0) return <p className="muted">No labeled data yet — run the replay.</p>
  const est = data.estimators || {}
  return (
    <div className="panel">
      <h2>Probability model benchmark {data.insufficient_data && <span className="lowdata small">⚠ low data (n={data.n})</span>}</h2>
      <p className="small muted">Baseline before ML. Lower Brier / log-loss = better; AUC 0.5 = no skill. n={data.n}, base rate {pct(data.base_rate)}.</p>
      <div className="table-wrap"><table className="mini">
        <thead><tr><th>Estimator</th><th className="right">Brier</th><th className="right">Log loss</th><th className="right">AUC</th><th className="right">Calib. err</th></tr></thead>
        <tbody>{Object.entries(est).map(([name, m]) => (
          <tr key={name} className={name === data.best_by_brier ? 'top20-name' : ''}>
            <td>{name}{name === data.best_by_brier ? ' ★' : ''}</td>
            <td className="right">{num(m.brier)}</td><td className="right">{num(m.log_loss)}</td>
            <td className="right">{m.auc == null ? '—' : num(m.auc)}</td><td className="right">{num(m.calibration_error)}</td></tr>
        ))}</tbody></table></div>
      <h4>Reliability (current estimator)</h4>
      <div className="table-wrap"><table className="mini">
        <thead><tr><th>Bin</th><th className="right">n</th><th className="right">Predicted</th><th className="right">Actual</th></tr></thead>
        <tbody>{(data.reliability?.diagram || []).filter((b) => b.n > 0).map((b) => (
          <tr key={b.bin}><td>{b.bin}</td><td className="right">{b.n}</td>
            <td className="right">{num(b.pred, 2)}</td><td className="right">{num(b.actual, 2)}</td></tr>
        ))}</tbody></table></div>
    </div>
  )
}

export default function Research() {
  const status = useData(api.replayStatus)
  const drift = useData(api.researchDrift)
  const regimes = useData(api.researchRegimes)
  const lb = useData(api.top20Leaderboard)
  const [busy, setBusy] = useState('')
  const [msg, setMsg] = useState(null)

  if (status.loading) return <Loading />
  const s = status.data || {}

  const act = async (name, fn) => {
    setBusy(name)
    try { const r = await fn(); setMsg(JSON.stringify(r).slice(0, 160)); status.reload() }
    catch (e) { setMsg('Error: ' + e.message) }
    finally { setBusy('') }
  }

  const best = (lb.data?.ranking || []).find((r) => r.has_trades)

  return (
    <div>
      <PageHead title="Research" subtitle="Historical replay engine, labeled dataset, and quant analysis" />
      <div className="paper-banner">📝 PAPER TRADING ONLY & deterministic — historical replay, no orders, no signing, no exchange connectivity</div>

      <div className="cards">
        <Stat label="Replay status" value={s.status} />
        <Stat label="Resolved markets" value={(s.resolved_markets || 0).toLocaleString()} sub={`${(s.markets_backfilled || 0).toLocaleString()} backfilled`} />
        <Stat label="Wallets" value={s.wallets} sub={`${s.wallets_backfilled || 0} backfilled`} />
        <Stat label="Feature vectors" value={(s.feature_vectors_total || 0).toLocaleString()} sub={`${(s.feature_vectors_labeled || 0).toLocaleString()} labeled`} />
        <Stat label="Replay paper trades" value={(s.replay_paper_trades || 0).toLocaleString()} />
        <Stat label="Signals generated" value={(s.signals_generated || 0).toLocaleString()} />
        <Stat label="Checkpoint (trade id)" value={s.checkpoint_trade_id} />
        <Stat label="Best historical strategy" value={best ? best.name : '—'} sub={best ? `score ${num(best.score, 0)}` : 'needs closed trades'} />
      </div>

      <div className="panel">
        <h2>Targets</h2>
        <Progress value={s.resolved_markets} target={s.targets?.markets || 5000} label="Resolved markets" />
        <Progress value={s.feature_vectors_total} target={s.targets?.feature_vectors || 10000} label="Labeled feature vectors" />
        <div className="top20-controls" style={{ marginTop: 12 }}>
          <button onClick={() => act('m', () => api.replayBackfillMarkets(5))} disabled={busy}>{busy === 'm' ? '…' : 'Backfill markets'}</button>
          <button onClick={() => act('w', () => api.replayBackfillWallets(5))} disabled={busy}>{busy === 'w' ? '…' : 'Backfill wallets'}</button>
          <button onClick={() => act('r', () => api.replayRun(400))} disabled={busy}>{busy === 'r' ? '…' : 'Run replay'}</button>
          <button className="secondary" onClick={() => act('x', () => api.replayReset())} disabled={busy}>Reset replay</button>
        </div>
        {msg && <p className="small muted" style={{ marginTop: 8 }}>{msg}</p>}
        <p className="small muted">Replay is chronological + checkpointed: no look-ahead (wallet reputation uses only positions resolved before each signal), resumable, incremental. Run repeatedly to accumulate.</p>
      </div>

      <Benchmark />

      <div className="grid-2">
        <div className="panel">
          <h2>Strategy drift {drift.data?.decay && <span className={`small ${drift.data.decay === 'degrading' ? 'down' : 'up'}`}>({drift.data.decay})</span>}</h2>
          <div className="table-wrap"><table className="mini">
            <thead><tr><th>Month</th><th className="right">Trades</th><th className="right">Sharpe</th><th className="right">Win%</th><th className="right">Avg edge</th><th className="right">Avg Kelly</th></tr></thead>
            <tbody>{(drift.data?.months || []).map((m) => (
              <tr key={m.month}><td>{m.month}</td><td className="right">{m.trades}</td>
                <td className="right">{num(m.sharpe, 2)}</td><td className="right">{pct(m.win_rate)}</td>
                <td className="right">{pct(m.avg_edge)}</td><td className="right">{num(m.avg_kelly, 3)}</td></tr>
            ))}</tbody></table>
            {(!drift.data?.months || drift.data.months.length === 0) && <p className="muted small">No closed trades yet.</p>}
          </div>
        </div>
        <div className="panel">
          <h2>Market regimes</h2>
          <div className="table-wrap"><table className="mini">
            <thead><tr><th>Regime</th><th className="right">Trades</th><th>Best strategy</th><th className="right">P/L</th></tr></thead>
            <tbody>{(regimes.data?.regime_performance || []).map((r) => (
              <tr key={r.regime}><td>{r.regime}</td><td className="right">{r.trades}</td>
                <td>{r.best_strategy || '—'}</td><td className="right"><PnL value={r.best_pnl} fmtFn={fmt.usd2} /></td></tr>
            ))}</tbody></table>
            {(!regimes.data?.regime_performance || regimes.data.regime_performance.length === 0) && <p className="muted small">No regime data yet.</p>}
          </div>
        </div>
      </div>
    </div>
  )
}
