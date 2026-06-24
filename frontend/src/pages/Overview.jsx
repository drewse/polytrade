import { useState } from 'react'
import { api, fmt } from '../api'
import { Badge, Loading, PageHead, PnL, ScoreBar, Sparkline, Stat, Toast, useData } from '../components/common.jsx'

export default function Overview() {
  const { data, loading, error, reload } = useData(api.overview)
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState(null)

  const runIngest = async () => {
    setBusy(true)
    try {
      const r = await api.runIngest()
      setToast({ msg: `Ingest: ${r.detail.new_trades} trades, ${r.detail.signals} signals` })
      reload()
    } catch (e) {
      setToast({ msg: e.message, err: true })
    } finally {
      setBusy(false)
    }
  }

  const seed = async () => {
    setBusy(true)
    try {
      const r = await api.seed()
      setToast({ msg: `Seeded ${r.detail.wallets} wallets, ${r.detail.markets} markets` })
      reload()
    } catch (e) {
      setToast({ msg: e.message, err: true })
    } finally {
      setBusy(false)
    }
  }

  if (loading) return <Loading />
  if (error) return <div className="empty">Error: {error}</div>
  const o = data

  return (
    <div>
      <PageHead title="Overview" subtitle="Would copying these wallets have made money?">
        <button className="secondary" onClick={seed} disabled={busy}>Re-seed mock</button>
        <button onClick={runIngest} disabled={busy}>{busy ? 'Running…' : 'Run ingest'}</button>
      </PageHead>

      <div className="cards">
        <Stat label="Bankroll" value={fmt.usd(o.bankroll)} sub={`start ${fmt.usd(o.starting_bankroll)}`} />
        <Stat label="Equity (mark-to-market)" value={fmt.usd(o.equity)} />
        <Stat label="Total PnL" value={<PnL value={o.total_pnl} fmtFn={fmt.usd} />} sub={`realized ${fmt.usd(o.realized_pnl)} · unreal ${fmt.usd(o.unrealized_pnl)}`} />
        <Stat label="ROI" value={<PnL value={o.roi} fmtFn={(n) => fmt.pct(n)} />} />
        <Stat label="Win rate (closed)" value={fmt.pct(o.win_rate)} sub={`${o.closed_positions} closed`} />
        <Stat label="Open positions" value={o.open_positions} />
        <Stat label="Signals today" value={o.signals_today} />
        <Stat label="Tracked" value={`${o.tracked_wallets} wallets`} sub={`${o.tracked_markets} markets`} />
      </div>

      <div className="panel">
        <h2>Equity curve</h2>
        <Sparkline points={o.equity_curve} />
      </div>

      <div className="panel">
        <h2>Top copied wallets</h2>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Wallet</th><th>Score</th><th>Class</th><th className="right">Realized ROI</th><th className="right">Copied positions</th>
              </tr>
            </thead>
            <tbody>
              {o.top_wallets.length === 0 && (
                <tr><td colSpan="5" className="muted">No wallets yet — seed mock data.</td></tr>
              )}
              {o.top_wallets.map((w) => (
                <tr key={w.wallet_id}>
                  <td>{w.label || <span className="mono">{w.address.slice(0, 12)}…</span>}</td>
                  <td><ScoreBar score={w.score} /></td>
                  <td><Badge kind={w.classification}>{w.classification}</Badge></td>
                  <td className="right"><PnL value={w.realized_roi * 100} fmtFn={fmt.pct} /></td>
                  <td className="right">{w.copied_positions}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <Toast message={toast?.msg} error={toast?.err} onDone={() => setToast(null)} />
    </div>
  )
}
