import { Fragment, useState } from 'react'
import { api, fmt } from '../api'
import { Badge, Loading, PageHead, PnL, Toast, useData } from '../components/common.jsx'

const SORTS = {
  pnl: { label: 'Total P/L', fn: (a, b) => b.total_pnl - a.total_pnl },
  roi: { label: 'Return %', fn: (a, b) => b.roi - a.roi },
  win: { label: 'Win rate', fn: (a, b) => b.win_rate - a.win_rate },
  drawdown: { label: 'Drawdown', fn: (a, b) => a.max_drawdown - b.max_drawdown },
  trades: { label: 'Trade count', fn: (a, b) => b.trades_entered - a.trades_entered },
}

function ExpandRow({ id }) {
  const { data, loading } = useData(() => api.top20Strategy(id), [id])
  if (loading) return <tr><td colSpan="12"><Loading /></td></tr>
  if (!data) return null
  return (
    <tr className="top20-detail">
      <td colSpan="12">
        <div className="top20-detail-grid">
          <div>
            <h4>{data.name}</h4>
            <p className="muted">{data.description}</p>
            <div className="top20-mini">
              <span>Signals evaluated: <b>{data.signals_evaluated}</b></span>
              <span>Paper trades: <b>{data.trades_entered}</b></span>
              <span>Open: <b>{data.open_positions}</b></span>
              <span>Closed: <b>{data.closed_positions}</b></span>
              <span>Avg return/trade: <b>{fmt.pct((data.avg_return_per_trade || 0) * 100)}</b></span>
              <span>Realized: <b><PnL value={data.realized_pnl} fmtFn={fmt.usd2} /></b></span>
              <span>Unrealized: <b><PnL value={data.unrealized_pnl} fmtFn={fmt.usd2} /></b></span>
            </div>
          </div>
          <div>
            <h4>Top copied wallets</h4>
            {data.top_wallets?.length ? (
              <table className="mini">
                <thead><tr><th>Wallet</th><th className="right">Trades</th><th className="right">P/L</th></tr></thead>
                <tbody>
                  {data.top_wallets.map((w) => (
                    <tr key={w.address}>
                      <td className="mono">{w.address.slice(0, 12)}…</td>
                      <td className="right">{w.trades}</td>
                      <td className="right"><PnL value={w.pnl} fmtFn={fmt.usd2} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : <p className="muted">No trades yet.</p>}
          </div>
        </div>
        <h4>Most recent paper trades</h4>
        {data.recent_trades?.length ? (
          <div className="table-wrap">
            <table className="mini">
              <thead>
                <tr>
                  <th>When</th><th>Wallet</th><th>Market</th><th>Out</th>
                  <th className="right">Entry</th><th className="right">Stake</th>
                  <th className="right">Shares</th><th className="right">Est. p</th>
                  <th className="right">Kelly</th><th>Status</th><th className="right">P/L</th>
                </tr>
              </thead>
              <tbody>
                {data.recent_trades.map((t) => (
                  <tr key={t.id}>
                    <td>{fmt.ago(t.entry_time)}</td>
                    <td className="mono">{t.wallet_address.slice(0, 10)}…</td>
                    <td title={t.market_question}>{(t.market_question || t.market_id).slice(0, 38)}</td>
                    <td>{t.outcome}</td>
                    <td className="right">{fmt.price(t.entry_price)}</td>
                    <td className="right">{fmt.usd2(t.stake)}</td>
                    <td className="right">{t.size_shares}</td>
                    <td className="right">{(t.estimated_probability ?? 0).toFixed(2)}</td>
                    <td className="right">{(t.kelly_fraction ?? 0).toFixed(3)}</td>
                    <td><Badge kind={t.status === 'closed' ? 'open' : 'yes'}>{t.status}</Badge></td>
                    <td className="right">
                      <PnL value={t.status === 'closed' ? t.realized_pnl : t.unrealized_pnl} fmtFn={fmt.usd2} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <p className="muted">No paper trades yet.</p>}
      </td>
    </tr>
  )
}

export default function Top20() {
  const { data, loading, error, reload } = useData(api.top20Strategies)
  const [sort, setSort] = useState('pnl')
  const [expanded, setExpanded] = useState(null)
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState(null)

  if (loading) return <Loading />
  if (error) return <div className="empty">Error: {error}</div>

  const strategies = [...(data?.strategies || [])].sort(SORTS[sort].fn)

  const recompute = async () => {
    setBusy(true)
    try {
      const r = await api.top20Recompute()
      setToast({ msg: `Evaluated ${r.detail.evaluated} signals · entered ${r.detail.entered} · closed ${r.detail.closed}` })
      reload()
    } catch (e) {
      setToast({ msg: e.message, err: true })
    } finally {
      setBusy(false)
    }
  }

  const reset = async () => {
    if (!window.confirm('Reset all TOP 20 paper trades and snapshots? (paper data only)')) return
    setBusy(true)
    try {
      const r = await api.top20Reset()
      setToast({ msg: `Reset: removed ${r.detail.trades_deleted} paper trades` })
      reload()
    } catch (e) {
      setToast({ msg: e.message, err: true })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <PageHead title="TOP 20" subtitle="20 paper copy-trading strategies, same signal stream, different rules">
        <button className="secondary" onClick={reset} disabled={busy}>Reset paper</button>
        <button onClick={recompute} disabled={busy}>{busy ? 'Running…' : 'Recompute'}</button>
      </PageHead>

      <div className="paper-banner">📝 PAPER TRADING ONLY — fractional-Kelly sizing, no real orders, no wallets, no keys</div>

      <div className="top20-controls">
        <span className="muted">Sort by</span>
        {Object.entries(SORTS).map(([k, s]) => (
          <button key={k} className={`chip ${sort === k ? 'active' : ''}`} onClick={() => setSort(k)}>
            {s.label}
          </button>
        ))}
      </div>

      <div className="panel">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>#</th><th>Strategy</th><th className="right">Bankroll</th>
                <th className="right">Total P/L</th><th className="right">Return %</th>
                <th className="right">Win rate</th><th className="right">Trades</th>
                <th className="right">Open</th><th className="right">Drawdown</th>
                <th>Last trade</th><th>Status</th><th></th>
              </tr>
            </thead>
            <tbody>
              {strategies.map((s, i) => (
                <Fragment key={s.id}>
                  <tr className="top20-row" onClick={() => setExpanded(expanded === s.id ? null : s.id)}>
                    <td>{i + 1}</td>
                    <td>
                      <div className="top20-name">{s.name}</div>
                      <div className="muted small">{s.description}</div>
                    </td>
                    <td className="right">{fmt.usd(s.bankroll)}</td>
                    <td className="right"><PnL value={s.total_pnl} fmtFn={fmt.usd2} /></td>
                    <td className="right"><PnL value={s.roi * 100} fmtFn={(n) => fmt.pct(n)} /></td>
                    <td className="right">{fmt.pct(s.win_rate * 100)}</td>
                    <td className="right">{s.trades_entered}</td>
                    <td className="right">{s.open_positions}</td>
                    <td className="right">{fmt.pct(s.max_drawdown * 100)}</td>
                    <td>{s.last_trade_at ? fmt.ago(s.last_trade_at) : '—'}</td>
                    <td><Badge kind={s.active ? 'yes' : 'bad'}>{s.active ? 'active' : 'inactive'}</Badge></td>
                    <td className="right">{expanded === s.id ? '▾' : '▸'}</td>
                  </tr>
                  {expanded === s.id && <ExpandRow id={s.id} />}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <Toast message={toast?.msg} error={toast?.err} onDone={() => setToast(null)} />
    </div>
  )
}
