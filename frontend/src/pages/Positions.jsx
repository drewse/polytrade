import { useState } from 'react'
import { api, fmt } from '../api'
import { Badge, Loading, PageHead, PnL, Toast, useData } from '../components/common.jsx'

export default function Positions() {
  const [filter, setFilter] = useState('') // '', 'open', 'closed'
  const { data, loading, error, reload } = useData(() => api.positions(filter), [filter])
  const [toast, setToast] = useState(null)
  const [busy, setBusy] = useState(null)

  const close = async (id) => {
    setBusy(id)
    try {
      const r = await api.closePosition(id)
      setToast({ msg: `Closed #${id}: PnL ${fmt.usd2(r.detail.realized_pnl)}` })
      reload()
    } catch (e) {
      setToast({ msg: e.message, err: true })
    } finally {
      setBusy(null)
    }
  }

  if (loading) return <Loading />
  if (error) return <div className="empty">Error: {error}</div>

  return (
    <div>
      <PageHead title="Paper Positions" subtitle="Simulated copied positions. No real orders placed.">
        <select style={{ width: 140 }} value={filter} onChange={(e) => setFilter(e.target.value)}>
          <option value="">All</option>
          <option value="open">Open</option>
          <option value="closed">Closed</option>
        </select>
      </PageHead>

      <div className="panel">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Status</th><th>Market</th><th>Outcome</th><th>Source wallet</th>
                <th className="right">Size</th><th className="right">Entry</th>
                <th className="right">Current</th><th className="right">PnL</th>
                <th>Opened</th><th></th>
              </tr>
            </thead>
            <tbody>
              {data.length === 0 && (
                <tr><td colSpan="10" className="muted">No positions.</td></tr>
              )}
              {data.map((p) => {
                const pnl = p.status === 'open' ? p.unrealized_pnl : p.realized_pnl
                return (
                  <tr key={p.id}>
                    <td><Badge kind={p.status}>{p.status}</Badge></td>
                    <td style={{ maxWidth: 240 }}>{p.market_question || p.market_id}</td>
                    <td><Badge kind={p.outcome === 'Yes' ? 'yes' : 'no'}>{p.outcome}</Badge></td>
                    <td className="mono">{p.wallet_address?.slice(0, 10)}…</td>
                    <td className="right">{fmt.usd(p.size)}</td>
                    <td className="right">{fmt.price(p.entry_price)}</td>
                    <td className="right">{fmt.price(p.exit_price ?? p.current_price)}</td>
                    <td className="right"><PnL value={pnl} fmtFn={fmt.usd2} /></td>
                    <td className="muted">{fmt.ago(p.opened_at)}</td>
                    <td>
                      {p.status === 'open' ? (
                        <button className="sm danger" disabled={busy === p.id} onClick={() => close(p.id)}>
                          {busy === p.id ? '…' : 'Close'}
                        </button>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </div>

      <Toast message={toast?.msg} error={toast?.err} onDone={() => setToast(null)} />
    </div>
  )
}
