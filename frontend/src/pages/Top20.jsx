import { Fragment, useState } from 'react'
import { api, fmt } from '../api'
import { Badge, Loading, PageHead, PnL, useData } from '../components/common.jsx'

const num = (n, d = 2) => (n ?? 0).toFixed(d)
const pct = (n) => fmt.pct((n ?? 0) * 100)

// metric columns the table can sort by ("sort by any metric")
const COLS = [
  { key: 'score', label: 'Score', render: (s) => num(s.score, 0), dir: -1 },
  { key: 'sharpe', label: 'Sharpe', render: (s) => num(s.sharpe), dir: -1 },
  { key: 'sortino', label: 'Sortino', render: (s) => num(s.sortino), dir: -1 },
  { key: 'profit_factor', label: 'PF', render: (s) => num(s.profit_factor), dir: -1 },
  { key: 'win_rate', label: 'Win%', render: (s) => pct(s.win_rate), dir: -1 },
  { key: 'max_drawdown', label: 'Max DD', render: (s) => pct(s.max_drawdown), dir: 1 },
  { key: 'total_return', label: 'Return%', render: (s) => <PnL value={s.total_return * 100} fmtFn={(n) => fmt.pct(n)} />, dir: -1 },
  { key: 'total_pnl', label: 'P/L', render: (s) => <PnL value={s.total_pnl} fmtFn={fmt.usd2} />, dir: -1 },
  { key: 'expectancy', label: 'Expectancy', render: (s) => <PnL value={s.expectancy} fmtFn={fmt.usd2} />, dir: -1 },
  { key: 'trades_entered', label: 'Trades', render: (s) => s.trades_entered, dir: -1 },
  { key: 'signal_acceptance', label: 'Accept%', render: (s) => pct(s.signal_acceptance), dir: -1 },
]

const METRIC_GRID = [
  ['Total return', (m) => pct(m.total_return)],
  ['Annualized', (m) => pct(m.annualized_return)],
  ['Sharpe', (m) => num(m.sharpe)],
  ['Sortino', (m) => num(m.sortino)],
  ['Profit factor', (m) => num(m.profit_factor)],
  ['Expectancy', (m) => fmt.usd2(m.expectancy)],
  ['Win rate', (m) => pct(m.win_rate)],
  ['Avg win', (m) => fmt.usd2(m.avg_win)],
  ['Avg loss', (m) => fmt.usd2(m.avg_loss)],
  ['Largest win', (m) => fmt.usd2(m.largest_win)],
  ['Largest loss', (m) => fmt.usd2(m.largest_loss)],
  ['Max drawdown', (m) => pct(m.max_drawdown)],
  ['Consec. wins', (m) => m.consecutive_wins],
  ['Consec. losses', (m) => m.consecutive_losses],
  ['Avg hold (min)', (m) => num(m.avg_holding_min, 0)],
  ['Median hold (min)', (m) => num(m.median_holding_min, 0)],
  ['Kelly growth', (m) => num(m.kelly_growth_rate, 4)],
  ['Consistency', (m) => pct(m.consistency)],
  ['Signals seen', (m) => m.signals_seen],
  ['Signals taken', (m) => m.signals_taken],
  ['Acceptance', (m) => pct(m.signal_acceptance)],
  ['Avg Kelly frac', (m) => num(m.avg_kelly_fraction, 3)],
  ['Avg position', (m) => fmt.usd2(m.avg_position_size)],
]

function StrategyDetail({ id }) {
  const { data, loading } = useData(() => api.top20Strategy(id), [id])
  if (loading) return <tr><td colSpan="13"><Loading /></td></tr>
  if (!data) return null
  const m = data.metrics || data
  return (
    <tr className="top20-detail">
      <td colSpan="13">
        <p className="muted">{data.description} · <b>exit:</b> {data.exit_policy} · <b>philosophy:</b> {data.philosophy}</p>
        <div className="metric-grid">
          {METRIC_GRID.map(([label, fn]) => (
            <div key={label} className="metric-cell"><span>{label}</span><b>{fn(m)}</b></div>
          ))}
        </div>
        <div className="top20-detail-grid">
          <div>
            <h4>Top copied wallets</h4>
            {data.top_wallets?.length ? (
              <table className="mini"><thead><tr><th>Wallet</th><th className="right">Trades</th><th className="right">P/L</th></tr></thead>
                <tbody>{data.top_wallets.map((w) => (
                  <tr key={w.address}><td className="mono">{w.address.slice(0, 12)}…</td>
                    <td className="right">{w.trades}</td><td className="right"><PnL value={w.pnl} fmtFn={fmt.usd2} /></td></tr>
                ))}</tbody></table>
            ) : <p className="muted">No trades yet.</p>}
          </div>
        </div>
        <h4>Most recent paper trades — each explains itself</h4>
        {data.recent_trades?.length ? (
          <div className="table-wrap"><table className="mini">
            <thead><tr><th>When</th><th>Market</th><th>Out</th><th className="right">Entry</th>
              <th className="right">Stake</th><th className="right">Est.p</th><th>Status</th>
              <th className="right">P/L</th><th>Why</th></tr></thead>
            <tbody>{data.recent_trades.map((t) => (
              <tr key={t.id}>
                <td>{fmt.ago(t.entry_time)}</td>
                <td title={t.market_question}>{(t.market_question || t.market_id).slice(0, 30)}</td>
                <td>{t.outcome}</td>
                <td className="right">{fmt.price(t.entry_price)}</td>
                <td className="right">{fmt.usd2(t.stake)}</td>
                <td className="right">{num(t.estimated_probability)}</td>
                <td><Badge kind={t.status === 'closed' ? 'open' : 'yes'}>{t.status}</Badge></td>
                <td className="right"><PnL value={t.status === 'closed' ? t.realized_pnl : t.unrealized_pnl} fmtFn={fmt.usd2} /></td>
                <td className="explain" title={t.explanation?.summary || ''}>{t.explanation?.summary?.slice(0, 60) || '—'}…</td>
              </tr>
            ))}</tbody></table></div>
        ) : <p className="muted">No paper trades yet.</p>}
      </td>
    </tr>
  )
}

function StrategiesTab({ strategies }) {
  const [sort, setSort] = useState('score')
  const [expanded, setExpanded] = useState(null)
  const col = COLS.find((c) => c.key === sort)
  const sorted = [...strategies].sort((a, b) => col.dir * ((b[sort] ?? 0) - (a[sort] ?? 0)))
  return (
    <>
      <div className="top20-controls">
        <span className="muted">Sort by</span>
        {COLS.map((c) => (
          <button key={c.key} className={`chip ${sort === c.key ? 'active' : ''}`} onClick={() => setSort(c.key)}>{c.label}</button>
        ))}
      </div>
      <div className="panel"><div className="table-wrap"><table>
        <thead><tr>
          <th>#</th><th>Strategy</th>
          {COLS.filter((c) => c.key !== 'score').map((c) => (
            <th key={c.key} className={`right ${sort === c.key ? 'sorted' : ''}`}>{c.label}</th>
          ))}
          <th></th>
        </tr></thead>
        <tbody>
          {sorted.map((s, i) => (
            <Fragment key={s.id}>
              <tr className="top20-row" onClick={() => setExpanded(expanded === s.id ? null : s.id)}>
                <td>{i + 1}</td>
                <td><div className="top20-name">{s.name}</div><div className="muted small">{s.philosophy}</div></td>
                {COLS.filter((c) => c.key !== 'score').map((c) => (
                  <td key={c.key} className="right">{c.render(s)}</td>
                ))}
                <td className="right">{expanded === s.id ? '▾' : '▸'}</td>
              </tr>
              {expanded === s.id && <StrategyDetail id={s.id} />}
            </Fragment>
          ))}
        </tbody>
      </table></div></div>
    </>
  )
}

function LeaderboardTab() {
  const { data, loading } = useData(api.top20Leaderboard)
  if (loading) return <Loading />
  if (!data) return null
  const ranked = data.ranking.filter((r) => r.has_trades)
  return (
    <div>
      {data.head_to_head && <div className="panel hilite"><b>Why #1 beats #2:</b> {data.head_to_head}</div>}
      <p className="muted small">Weighted score = 30% Sharpe · 20% Profit Factor · 15% (low) Drawdown · 15% CAGR · 10% Win Rate · 10% Consistency</p>
      <div className="lb-grid">
        {ranked.map((r) => (
          <div key={r.id} className="lb-card">
            <div className="lb-rank">#{r.rank}</div>
            <div className="lb-body">
              <div className="lb-name">{r.name} <span className="lb-score">{num(r.score, 0)}</span></div>
              <div className="muted small">{r.reason}</div>
              {r.strengths?.length > 0 && <div className="small"><b className="up">Strengths:</b> {r.strengths.join(', ')}</div>}
              {r.weaknesses?.length > 0 && <div className="small"><b className="down">Weaknesses:</b> {r.weaknesses.join(', ')}</div>}
            </div>
          </div>
        ))}
      </div>
      {ranked.length === 0 && <p className="muted">No closed trades yet — ranking needs settled results. Let positions resolve, then recompute.</p>}
    </div>
  )
}

function ForwardTab() {
  const { data, loading } = useData(api.top20ForwardTest)
  if (loading) return <Loading />
  if (!data) return null
  return (
    <div className="panel">
      <p className="muted small">{data.split}. Decisions were made at entry time — no look-ahead.</p>
      <div className="table-wrap"><table className="mini">
        <thead><tr><th>Strategy</th><th>Closed</th>
          <th className="right">Train P/L</th><th className="right">Train Sharpe</th>
          <th className="right">Val P/L</th><th className="right">Val Sharpe</th>
          <th className="right">Forward P/L</th><th className="right">Forward Sharpe</th></tr></thead>
        <tbody>{data.strategies.filter((s) => s.total_closed > 0).map((s) => (
          <tr key={s.id}><td>{s.name}</td><td>{s.total_closed}</td>
            <td className="right"><PnL value={s.segments.train.pnl} fmtFn={fmt.usd2} /></td>
            <td className="right">{num(s.segments.train.sharpe)}</td>
            <td className="right"><PnL value={s.segments.validation.pnl} fmtFn={fmt.usd2} /></td>
            <td className="right">{num(s.segments.validation.sharpe)}</td>
            <td className="right"><PnL value={s.segments.forward.pnl} fmtFn={fmt.usd2} /></td>
            <td className="right">{num(s.segments.forward.sharpe)}</td></tr>
        ))}</tbody></table></div>
    </div>
  )
}

export default function Top20() {
  const { data, loading, error, reload } = useData(api.top20Strategies)
  const [tab, setTab] = useState('strategies')
  const [busy, setBusy] = useState(false)

  if (loading) return <Loading />
  if (error) return <div className="empty">Error: {error}</div>
  const strategies = data?.strategies || []

  const act = async (fn) => {
    setBusy(true)
    try { await fn(); reload() } finally { setBusy(false) }
  }

  return (
    <div>
      <PageHead title="TOP 20" subtitle="Quant research lab — which strategy has the best risk-adjusted returns, and why">
        <button className="secondary" onClick={() => act(() => window.confirm('Reset paper data?') ? api.top20Reset() : null)} disabled={busy}>Reset</button>
        <button onClick={() => act(api.top20Recompute)} disabled={busy}>{busy ? 'Running…' : 'Recompute'}</button>
      </PageHead>
      <div className="paper-banner">📝 PAPER TRADING ONLY — fractional-Kelly sizing, statistical probability model, no real orders, no wallets, no keys</div>
      <div className="top20-tabs">
        {[['strategies', 'Strategies'], ['leaderboard', 'Best Strategy'], ['forward', 'Forward Test']].map(([k, l]) => (
          <button key={k} className={`tab ${tab === k ? 'active' : ''}`} onClick={() => setTab(k)}>{l}</button>
        ))}
      </div>
      {tab === 'strategies' && <StrategiesTab strategies={strategies} />}
      {tab === 'leaderboard' && <LeaderboardTab />}
      {tab === 'forward' && <ForwardTab />}
    </div>
  )
}
