import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { Loading, Empty, WalletLink } from '../components/common.jsx'

const num = (n, d = 2) => (n == null ? '—' : Number(n).toFixed(d))
const pct = (n) => (n == null ? '—' : `${(Number(n) * 100).toFixed(1)}%`)
const usd = (n) => (n == null ? '—' : `$${Number(n).toLocaleString(undefined, { maximumFractionDigits: 0 })}`)

const GRADE = {
  complete: { label: 'complete', kind: 'yes' },
  high: { label: 'high', kind: 'yes' },
  medium: { label: 'medium', kind: 'open' },
  low: { label: 'low', kind: 'bad' },
  unknown: { label: 'unknown', kind: 'neutral' },
}
function CoverageBadge({ grade, ratio }) {
  const g = GRADE[grade] || GRADE.unknown
  return <span className={`badge ${g.kind}`} title={ratio == null ? 'no coverage data' : `coverage ${pct(ratio)}`}>{g.label}{ratio != null ? ` · ${pct(ratio)}` : ''}</span>
}

// ---- Deep Backfill status panel (Part 2 dashboard) ------------------------
export function DeepBackfillPanel({ status, onRun, running }) {
  const s = status || {}
  const by = s.by_status || {}
  return (
    <div className="panel" data-testid="deep-backfill-panel">
      <div className="page-head" style={{ marginBottom: 8 }}>
        <h2 style={{ margin: 0 }}>Deep Historical Backfill</h2>
        <div className="toolbar">
          <button onClick={onRun} disabled={running} data-testid="run-backfill">
            {running ? 'Backfilling…' : '▶ Run backfill batch'}
          </button>
        </div>
      </div>
      <p className="muted small" style={{ marginTop: -4 }}>
        Pages older trade history per wallet until coverage target ({pct(s.coverage_target)}) or the history is exhausted.
        Idempotent + resumable · page size {s.page_size ?? '—'} · max {s.max_pages_per_run ?? '—'} pages/run.
        Read-only: never trades, never auto-approves.
      </p>
      <div className="cards">
        <div className="card"><div className="label">Queued</div><div className="value">{s.queued ?? 0}</div></div>
        <div className="card"><div className="label">Running</div><div className="value">{(s.running || []).length}</div></div>
        <div className="card"><div className="label">Completed</div><div className="value pos">{s.completed ?? 0}</div></div>
        <div className="card"><div className="label">Failed</div><div className={`value ${s.failed ? 'neg' : ''}`}>{s.failed ?? 0}</div></div>
        <div className="card"><div className="label">Tracked wallets</div><div className="value">{s.tracked ?? 0}</div></div>
        <div className="card"><div className="label">Avg coverage</div><div className="value">{s.average_coverage == null ? '—' : pct(s.average_coverage)}</div></div>
      </div>
      {!!(s.top_low_coverage_production || []).length && (
        <>
          <h3 style={{ marginBottom: 4 }}>Lowest-coverage production wallets</h3>
          <div className="table-wrap">
            <table data-testid="low-coverage-table">
              <thead><tr><th>Wallet</th><th className="right">Coverage</th><th>Grade</th></tr></thead>
              <tbody>
                {s.top_low_coverage_production.map((r) => (
                  <tr key={r.address}>
                    <td className="mono"><WalletLink address={r.address} /></td>
                    <td className="right">{pct(r.coverage_ratio)}</td>
                    <td><CoverageBadge grade={r.grade} ratio={r.coverage_ratio} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}

// ---- Approved Wallets table (Part 3) --------------------------------------
// Pure + interactive — exported for testing. `onAction(address, action)` is the
// only side-effect channel; the wrapper wires it to the approval API.
export function ApprovedWalletsTable({ wallets, onAction, busy }) {
  const rows = wallets || []
  if (!rows.length) return <Empty>No wallets are eligible or manually managed yet.</Empty>
  const act = (addr, action) => onAction && onAction(addr, action)
  return (
    <div className="table-wrap">
      <table data-testid="approved-table">
        <thead>
          <tr>
            <th>#</th><th>Wallet</th><th>State</th><th>Approval</th>
            <th className="right">Prod score</th><th className="right">ROI</th><th className="right">PF</th>
            <th className="right">Settled</th><th className="right">Public P/L</th><th>Coverage</th>
            <th>Copyable?</th><th>Controls</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((w) => (
            <tr key={w.address} data-testid="approved-row" className={w.manually_disabled ? 'row-disabled' : ''}>
              <td>{w.production_rank ?? '—'}</td>
              <td className="mono"><WalletLink address={w.address} /></td>
              <td>
                {w.manually_disabled
                  ? <span className="badge bad" data-testid="disabled-badge" title={w.note || 'manually disabled'}>⛔ disabled</span>
                  : <span className="badge yes">enabled</span>}
              </td>
              <td>
                {w.manually_approved
                  ? <span className="badge yes">✓ approved</span>
                  : <span className={`badge ${w.approval_status === 'rejected' ? 'bad' : w.approval_status === 'watchlist' ? 'open' : 'neutral'}`}>{w.approval_status}</span>}
              </td>
              <td className="right"><b>{num(w.production_rank_score, 1)}</b></td>
              <td className={`right ${w.roi > 0 ? 'pos' : w.roi < 0 ? 'neg' : ''}`}>{w.roi == null ? '—' : pct(w.roi)}</td>
              <td className="right">{num(w.profit_factor, 2)}</td>
              <td className="right">{w.num_settled ?? '—'}</td>
              <td className={`right ${w.public_all_time_pnl > 0 ? 'pos' : w.public_all_time_pnl < 0 ? 'neg' : ''}`}>{usd(w.public_all_time_pnl)}</td>
              <td><CoverageBadge grade={w.coverage_grade} ratio={w.coverage_ratio} /></td>
              <td className="small">
                {w.copyable
                  ? <span className="pos">✓ copied</span>
                  : <span className="neg" title={w.why_not_copyable || ''}>✗ {w.why_not_copyable || 'not copyable'}</span>}
              </td>
              <td>
                <div className="toolbar" style={{ gap: 4 }}>
                  {w.manually_disabled
                    ? <button className="secondary small" disabled={busy === w.address} onClick={() => act(w.address, 'enable')}>Enable</button>
                    : <button className="danger small" disabled={busy === w.address} onClick={() => act(w.address, 'disable')}>Disable</button>}
                  {w.manually_approved
                    ? <button className="secondary small" disabled={busy === w.address} onClick={() => act(w.address, 'remove_approval')}>Unapprove</button>
                    : <button className="secondary small" disabled={busy === w.address} onClick={() => act(w.address, 'approve')}>Approve</button>}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ---- Data-fetching wrapper -------------------------------------------------
export default function ApprovedWallets() {
  const [data, setData] = useState(null)
  const [backfill, setBackfill] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [toast, setToast] = useState(null)

  const load = useCallback(async () => {
    try {
      const [aw, bf] = await Promise.all([
        api.liveApprovedWallets(),
        api.liveDeepBackfillStatus().catch(() => null),
      ])
      setData(aw); setBackfill(bf); setError(null)
    } catch (e) { setError(e.message) } finally { setLoading(false) }
  }, [])

  useEffect(() => { load() }, [load])
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 4000)
    return () => clearTimeout(t)
  }, [toast])

  const onAction = async (address, action) => {
    if (action === 'disable' && !window.confirm(`Disable ${address}? It will NOT be copied even if it ranks #1 (hard override).`)) return
    setBusy(address)
    try {
      const res = await api.liveWalletApproval(address, action)
      setToast({ message: res?.ok === false ? (res.error || 'failed') : `${action} applied`, isErr: res?.ok === false })
      await load()
    } catch (e) { setToast({ message: e.message, isErr: true }) } finally { setBusy('') }
  }

  const onRunBackfill = async () => {
    setBusy('__backfill__')
    try {
      const res = await api.liveDeepBackfillRunOnce(3)
      const d = res?.detail || res || {}
      setToast({ message: `Backfill: ${d.processed ?? 0} wallet(s), +${d.trades_inserted ?? 0} trades` })
      await load()
    } catch (e) { setToast({ message: e.message, isErr: true }) } finally { setBusy('') }
  }

  if (loading) return <Loading />
  if (error) return <Empty>Approved wallets unavailable: {error}</Empty>

  return (
    <div>
      <DeepBackfillPanel status={backfill} onRun={onRunBackfill} running={busy === '__backfill__'} />
      <div className="panel">
        <div className="page-head" style={{ marginBottom: 8 }}>
          <h2 style={{ margin: 0 }}>Approved Wallets</h2>
          <button className="secondary" onClick={load}>↻ Refresh</button>
        </div>
        <p className="muted small" style={{ marginTop: -4 }}>
          {data?.copied_count ?? 0} copied · {data?.approved_count ?? 0} manually approved · {data?.disabled_count ?? 0} disabled ·
          manual-approval-required: <b>{String(data?.require_manual_approval)}</b>.
          <b> Manual disable is a hard override</b> — a disabled wallet is never copied even if it ranks #1.
          Approval is a positive marker that still requires the normal safety gates.
        </p>
        <ApprovedWalletsTable wallets={data?.wallets || []} onAction={onAction} busy={busy} />
      </div>
      {toast && <div className={`toast ${toast.isErr ? 'err' : ''}`}>{toast.message}</div>}
    </div>
  )
}
