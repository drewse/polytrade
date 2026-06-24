import { useState } from 'react'
import { api, fmt } from '../api'
import { Badge, Loading, PageHead, PnL, ScoreBar, Toast, useData } from '../components/common.jsx'

function CategoryPerf({ perf }) {
  const entries = Object.entries(perf || {})
  if (!entries.length) return <span className="muted">—</span>
  return (
    <span>
      {entries
        .sort((a, b) => b[1] - a[1])
        .slice(0, 3)
        .map(([cat, roi]) => (
          <span key={cat} style={{ marginRight: 8 }} className="mono">
            {cat}:&nbsp;<PnL value={roi * 100} fmtFn={(n) => `${n.toFixed(0)}%`} />
          </span>
        ))}
    </span>
  )
}

export default function Wallets() {
  const { data, loading, error, reload } = useData(api.wallets)
  const { data: attribution } = useData(api.attribution)
  const [toast, setToast] = useState(null)
  const [addr, setAddr] = useState('')
  const [label, setLabel] = useState('')

  const toggleCopy = async (w) => {
    try {
      await api.updateWallet(w.id, { copy_enabled: !w.copy_enabled })
      reload()
    } catch (e) {
      setToast({ msg: e.message, err: true })
    }
  }

  const addWallet = async () => {
    if (!addr.trim()) return
    try {
      await api.addWallet({ address: addr.trim(), label: label.trim() || null })
      setAddr(''); setLabel('')
      setToast({ msg: 'Wallet added' })
      reload()
    } catch (e) {
      setToast({ msg: e.message, err: true })
    }
  }

  const backfill = async () => {
    if (!addr.trim()) return
    try {
      const r = await api.backfillWallet(addr.trim())
      setToast({ msg: `Backfilled: ${r.detail.trades_inserted} trades, score ${r.detail.score} (${r.detail.classification})` })
      setAddr(''); setLabel('')
      reload()
    } catch (e) {
      setToast({ msg: e.message, err: true })
    }
  }

  if (loading) return <Loading />
  if (error) return <div className="empty">Error: {error}</div>

  return (
    <div>
      <PageHead title="Wallets" subtitle="Tracked traders ranked by score (0–100)." />

      <div className="panel">
        <h2>Track a new wallet</h2>
        <div className="toolbar">
          <input placeholder="0x wallet address" value={addr} onChange={(e) => setAddr(e.target.value)} />
          <input placeholder="label (optional)" value={label} onChange={(e) => setLabel(e.target.value)} />
          <button onClick={addWallet}>Add</button>
          <button className="secondary" onClick={backfill} title="Pull recent live history & score (live mode only)">
            Backfill (live)
          </button>
        </div>
        <div className="hint" style={{ marginTop: 8 }}>
          “Backfill” reads the wallet’s recent on-chain trade history from Polymarket’s public API
          (read-only) and scores it. Stats are marked <em>partial</em> — it’s a recent window, not full history.
        </div>
      </div>

      <div className="panel">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Wallet</th><th>Score</th><th>Class</th><th className="right">ROI</th>
                <th className="right">Win%</th><th className="right">Trades</th>
                <th className="right">Avg size</th><th>Top categories</th><th>Last active</th><th>Copy</th>
              </tr>
            </thead>
            <tbody>
              {data.map((w) => {
                const s = w.stats || {}
                return (
                  <tr key={w.id}>
                    <td>{w.label || <span className="mono">{w.address.slice(0, 12)}…</span>}</td>
                    <td><ScoreBar score={s.score || 0} /></td>
                    <td>
                      <Badge kind={s.classification || 'insufficient_data'}>{(s.classification || 'n/a').replace('_', ' ')}</Badge>
                      {s.partial_history && (
                        <span className="src-badge partial" style={{ marginLeft: 6, padding: '2px 6px' }} title="Recent-window stats only">partial</span>
                      )}
                    </td>
                    <td className="right"><PnL value={(s.realized_roi || 0) * 100} fmtFn={fmt.pct} /></td>
                    <td className="right">{fmt.pct((s.win_rate || 0) * 100)}</td>
                    <td className="right">{s.num_trades || 0}</td>
                    <td className="right">{fmt.usd(s.avg_trade_size || 0)}</td>
                    <td><CategoryPerf perf={s.category_performance} /></td>
                    <td className="muted">{fmt.ago(w.last_active)}</td>
                    <td>
                      <button
                        className={`sm ${w.copy_enabled ? 'secondary' : ''}`}
                        onClick={() => toggleCopy(w)}
                      >
                        {w.copy_enabled ? 'Disable' : 'Enable'}
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </div>

      <div className="panel">
        <h2>Copy attribution (paper) — which wallets actually made us money?</h2>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Wallet</th><th>Class</th><th className="right">Copied signals</th>
                <th className="right">Positions</th><th className="right">Closed</th>
                <th className="right">Wins</th><th className="right">Win%</th>
                <th className="right">Realized</th><th className="right">Unrealized</th>
                <th className="right">Total PnL</th><th className="right">ROI</th>
                <th className="right">Avg entry</th>
              </tr>
            </thead>
            <tbody>
              {(!attribution || attribution.length === 0) && (
                <tr><td colSpan="12" className="muted">No copied positions yet.</td></tr>
              )}
              {(attribution || []).map((a) => (
                <tr key={a.wallet_id}>
                  <td>{a.label || <span className="mono">{a.address.slice(0, 12)}…</span>}</td>
                  <td><Badge kind={a.classification}>{a.classification.replace('_', ' ')}</Badge></td>
                  <td className="right">{a.copied_signals}</td>
                  <td className="right">{a.copied_positions}</td>
                  <td className="right">{a.closed_positions}</td>
                  <td className="right">{a.winning_positions}</td>
                  <td className="right">{fmt.pct(a.win_rate * 100)}</td>
                  <td className="right"><PnL value={a.realized_pnl} fmtFn={fmt.usd2} /></td>
                  <td className="right"><PnL value={a.unrealized_pnl} fmtFn={fmt.usd2} /></td>
                  <td className="right"><PnL value={a.total_pnl} fmtFn={fmt.usd2} /></td>
                  <td className="right"><PnL value={a.roi * 100} fmtFn={fmt.pct} /></td>
                  <td className="right">{fmt.price(a.avg_entry_price)}</td>
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
