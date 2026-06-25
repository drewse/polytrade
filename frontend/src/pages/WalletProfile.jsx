import { useParams, Link } from 'react-router-dom'
import { api, fmt } from '../api'
import { Badge, Loading, PageHead, PnL, Sparkline, Stat, useData } from '../components/common.jsx'

const pct = (n) => fmt.pct((n ?? 0) * 100)

export default function WalletProfile() {
  const { address } = useParams()
  const { data, loading, error } = useData(() => api.walletProfile(address), [address])
  if (loading) return <Loading />
  if (error) return <div className="empty">Error: {error}</div>
  const p = data

  return (
    <div>
      <PageHead title="Wallet Profile" subtitle={p.address}>
        <Link className="nav-link inline" to="/discovery">← Discovery</Link>
      </PageHead>
      <div className="paper-banner">📝 PAPER TRADING ONLY — read-only on-chain analytics, no orders</div>

      <div className="cards">
        <Stat label="Copyability" value={(p.copyability ?? 0).toFixed(1)} sub={<Badge kind={p.classification}>{p.classification}</Badge>} />
        <Stat label="ROI (realized)" value={<PnL value={(p.roi ?? 0) * 100} fmtFn={(n) => fmt.pct(n)} />} />
        <Stat label="Win rate" value={pct(p.win_rate)} sub={`${p.num_settled} settled`} />
        <Stat label="Sharpe (proxy)" value={(p.sharpe ?? 0).toFixed(2)} />
        <Stat label="Sharpe (settled)" value={(p.sharpe_of_settled ?? 0).toFixed(2)} />
        <Stat label="Profit factor" value={(p.profit_factor ?? 0).toFixed(2)} />
        <Stat label="Max drawdown" value={pct(p.max_drawdown)} />
        <Stat label="Avg position" value={fmt.usd2(p.avg_position_size)} />
      </div>

      <div className="cards">
        <Stat label="Last 7d" value={<PnL value={p.recent_7d?.pnl} fmtFn={fmt.usd} />} sub={`${p.recent_7d?.settled || 0} settled`} />
        <Stat label="Last 30d" value={<PnL value={p.recent_30d?.pnl} fmtFn={fmt.usd} />} sub={`${p.recent_30d?.settled || 0} settled`} />
        <Stat label="Lifetime" value={<PnL value={p.lifetime?.pnl} fmtFn={fmt.usd} />} sub={`${p.lifetime?.settled || 0} settled`} />
      </div>

      <div className="panel">
        <h2>Realized P&L curve (settled positions)</h2>
        <Sparkline points={(p.equity_curve || []).map((e) => e.pnl)} />
      </div>

      <div className="grid-2">
        <div className="panel">
          <h2>Best categories</h2>
          {p.best_categories?.length ? p.best_categories.map((c) => (
            <div key={c.category} className="expo-row"><div className="expo-label">{c.category}</div>
              <div className="expo-val"><PnL value={c.pnl} fmtFn={fmt.usd2} /> · {c.trades} trades</div></div>
          )) : <p className="muted">No profitable categories yet.</p>}
        </div>
        <div className="panel">
          <h2>Worst categories</h2>
          {p.worst_categories?.length ? p.worst_categories.map((c) => (
            <div key={c.category} className="expo-row"><div className="expo-label">{c.category}</div>
              <div className="expo-val"><PnL value={c.pnl} fmtFn={fmt.usd2} /> · {c.trades} trades</div></div>
          )) : <p className="muted">No losing categories.</p>}
        </div>
      </div>

      <div className="panel">
        <h2>Category breakdown</h2>
        <div className="table-wrap"><table className="mini">
          <thead><tr><th>Category</th><th className="right">Trades</th><th className="right">P/L</th></tr></thead>
          <tbody>{(p.category_breakdown || []).map((c) => (
            <tr key={c.category}><td>{c.category}</td><td className="right">{c.trades}</td>
              <td className="right"><PnL value={c.pnl} fmtFn={fmt.usd2} /></td></tr>
          ))}</tbody></table></div>
      </div>
    </div>
  )
}
