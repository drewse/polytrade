import { api, fmt } from '../api'
import { Loading, PageHead, PnL, Sparkline, Stat, useData } from '../components/common.jsx'

const pct = (n) => fmt.pct((n ?? 0) * 100)

function ExposureBars({ title, data }) {
  const entries = Object.entries(data || {})
  const max = Math.max(1, ...entries.map(([, v]) => v))
  return (
    <div className="panel">
      <h2>{title}</h2>
      {entries.length === 0 && <p className="muted">No open exposure.</p>}
      {entries.map(([k, v]) => (
        <div key={k} className="expo-row">
          <div className="expo-label" title={k}>{k.length > 28 ? k.slice(0, 28) + '…' : k}</div>
          <div className="expo-bar"><div className="expo-fill" style={{ width: `${(v / max) * 100}%` }} /></div>
          <div className="expo-val">{fmt.usd2(v)}</div>
        </div>
      ))}
    </div>
  )
}

export default function Portfolio() {
  const { data, loading, error } = useData(api.top20Portfolio)
  if (loading) return <Loading />
  if (error) return <div className="empty">Error: {error}</div>
  const p = data

  return (
    <div>
      <PageHead title="Portfolio" subtitle="Aggregate paper portfolio across all 20 strategies" />
      <div className="paper-banner">📝 PAPER TRADING ONLY — simulated capital, no real positions</div>

      <div className="cards">
        <Stat label="Equity" value={fmt.usd(p.equity)} sub={`start ${fmt.usd(p.starting_capital)}`} />
        <Stat label="Total P/L" value={<PnL value={p.total_pnl} fmtFn={fmt.usd} />} sub={`realized ${fmt.usd(p.realized_pnl)} · unreal ${fmt.usd(p.unrealized_pnl)}`} />
        <Stat label="Capital utilization" value={pct(p.capital_utilization)} sub={`exposure ${fmt.usd(p.open_exposure)}`} />
        <Stat label="Max drawdown" value={pct(p.max_drawdown)} />
        <Stat label="Rolling Sharpe" value={(p.rolling_sharpe ?? 0).toFixed(2)} sub="last 30 snapshots" />
        <Stat label="Rolling volatility" value={(p.rolling_volatility ?? 0).toFixed(4)} />
        <Stat label="Open positions" value={p.open_positions} />
        <Stat label="Closed positions" value={p.closed_positions} />
      </div>

      <div className="panel">
        <h2>Equity curve</h2>
        <Sparkline points={(p.equity_curve || []).map((e) => e.equity)} />
      </div>

      <div className="grid-2">
        <ExposureBars title="Exposure by category" data={p.exposure_by_category} />
        <ExposureBars title="Exposure by wallet" data={p.exposure_by_wallet} />
      </div>
      <ExposureBars title="Exposure by market" data={p.exposure_by_market} />
    </div>
  )
}
