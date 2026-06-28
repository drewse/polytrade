import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { Loading, Empty, WalletLink } from '../components/common.jsx'

const num = (n, d = 2) => (n == null ? '—' : Number(n).toFixed(d))
const pct = (n) => (n == null ? '—' : `${(Number(n) * 100).toFixed(1)}%`)
const usd = (n) => (n == null ? '—' : `$${Number(n).toLocaleString(undefined, { maximumFractionDigits: 0 })}`)

const GRADE = {
  complete: 'yes', high: 'yes', medium: 'open', low: 'bad', unknown: 'neutral',
}
function CoverageBadge({ grade, ratio }) {
  return <span className={`badge ${GRADE[grade] || 'neutral'}`} title={ratio == null ? 'no coverage data' : `coverage ${pct(ratio)}`}>
    {grade || 'unknown'}{ratio != null ? ` · ${pct(ratio)}` : ''}
  </span>
}

// Pure + interactive table. `onAction(address, action)` is the only side-effect.
export function ApprovalQueueTable({ candidates, onAction, busy }) {
  const rows = candidates || []
  if (!rows.length) return <Empty>No wallets currently meet the approval-queue criteria.</Empty>
  const act = (addr, action) => onAction && onAction(addr, action)
  return (
    <div className="table-wrap">
      <table data-testid="queue-table">
        <thead>
          <tr>
            <th className="right">Rec</th><th>Wallet</th><th className="right">Public P/L</th>
            <th className="right">ROI</th><th className="right">PF</th><th className="right">Settled</th>
            <th>Coverage</th><th>Why recommended</th><th>Not auto-approved</th><th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((c) => (
            <tr key={c.address} data-testid="queue-row">
              <td className="right"><b>{num(c.recommendation_score, 1)}</b></td>
              <td className="mono">
                <WalletLink address={c.address} />
                {c.watchlisted && <span className="badge open" style={{ marginLeft: 4 }}>👁 watch</span>}
              </td>
              <td className={`right ${c.public_all_time_pnl > 0 ? 'pos' : c.public_all_time_pnl < 0 ? 'neg' : ''}`}>{usd(c.public_all_time_pnl)}</td>
              <td className={`right ${c.roi > 0 ? 'pos' : 'neg'}`}>{c.roi == null ? '—' : pct(c.roi)}</td>
              <td className="right">{num(c.profit_factor, 2)}</td>
              <td className="right">{c.num_settled ?? '—'}</td>
              <td><CoverageBadge grade={c.coverage_grade} ratio={c.coverage_ratio} /></td>
              <td className="small" title={c.why_recommended}>{c.why_recommended}</td>
              <td className="small neg" title={c.why_not_auto_approved}>{c.why_not_auto_approved}</td>
              <td>
                <div className="toolbar" style={{ gap: 4 }}>
                  <button className="small" disabled={busy === c.address} onClick={() => act(c.address, 'approve')}>Approve</button>
                  <button className="danger small" disabled={busy === c.address} onClick={() => act(c.address, 'reject')}>Reject</button>
                  <button className="secondary small" disabled={busy === c.address} onClick={() => act(c.address, 'watchlist')}>Watchlist</button>
                  <button className="secondary small" disabled={busy === c.address} onClick={() => act(c.address, 'request_backfill')}
                    title="Queue this wallet for deeper history backfill">Deeper backfill</button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function WalletApprovalQueue() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [toast, setToast] = useState(null)

  const load = useCallback(async () => {
    try {
      const d = await api.liveWalletApprovalQueue()
      setData(d); setError(null)
    } catch (e) { setError(e.message) } finally { setLoading(false) }
  }, [])

  useEffect(() => { load() }, [load])
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 4000)
    return () => clearTimeout(t)
  }, [toast])

  const onAction = async (address, action) => {
    if (action === 'approve' && !window.confirm(`Approve ${address}? It becomes eligible to ENTER production ranking — only copied if it also passes the safety gates.`)) return
    setBusy(address)
    try {
      const res = await api.liveWalletApproval(address, action)
      const label = action === 'request_backfill' ? 'deeper backfill requested' : `${action} applied`
      setToast({ message: res?.ok === false ? (res.error || 'failed') : label, isErr: res?.ok === false })
      await load()
    } catch (e) { setToast({ message: e.message, isErr: true }) } finally { setBusy('') }
  }

  if (loading) return <Loading />
  if (error) return <Empty>Approval queue unavailable: {error}</Empty>
  const cr = data?.criteria || {}

  return (
    <div className="panel">
      <h2>Wallet Approval Queue — gated promotion</h2>
      <p className="muted small" style={{ marginTop: -6 }}>
        {data?.count ?? 0} candidate(s) meeting criteria: public P/L &gt; {usd(cr.min_public_pnl)} ·
        ROI &gt; {pct(cr.min_roi)} · PF ≥ {num(cr.min_pf, 2)} · settled ≥ {cr.min_settled} ·
        coverage ≥ {pct(cr.min_coverage)} (or grade medium+). <b>Nothing here is copied until you approve it</b>, and
        approval only means "allowed to enter production ranking if it passes the gates."
      </p>
      <ApprovalQueueTable candidates={data?.candidates || []} onAction={onAction} busy={busy} />
      {toast && <div className={`toast ${toast.isErr ? 'err' : ''}`}>{toast.message}</div>}
    </div>
  )
}
