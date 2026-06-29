import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { Loading, Empty } from '../components/common.jsx'

const num = (n, d = 4) => (n == null ? '—' : Number(n).toFixed(d))
const pct = (n, d = 1) => (n == null ? '—' : `${(Number(n) * 100).toFixed(d)}%`)
const ago = (iso) => {
  if (!iso) return 'never'
  const s = (Date.now() - new Date(iso).getTime()) / 1000
  if (s < 60) return `${Math.floor(s)}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  return `${Math.floor(s / 3600)}h ago`
}

const STATUS = {
  research_only_not_validated: { kind: 'warn', label: 'Research only — not validated' },
  failed_validation: { kind: 'bad', label: 'Failed validation' },
  paper_validated: { kind: 'yes', label: 'Paper-validated (still no live path)' },
}
const GATE_LABEL = {
  min_100_fills: '≥ 100 paper fills',
  'prob_ev_positive_ge_0.95': 'P(EV>0) ≥ 0.95',
  ci_strictly_above_zero: '95% CI strictly > 0',
  stable_across_2_weeks: 'Stable across ≥ 2 weeks',
  worst_queue_positive: 'Worst-case queue positive',
  no_regime_over_60pct: 'No regime > 60% of EV',
  ev_positive_excluding_top5: 'EV > 0 excluding top-5 fills',
}

// Pure presentational — exported for tests.
export function GateProgress({ gate, fills, target }) {
  const entries = Object.entries(gate || {})
  const passed = entries.filter(([, v]) => v).length
  return (
    <div className="panel" data-testid="gate-progress">
      <div className="label">Pre-registered validation gate — {passed}/{entries.length || 7} passed
        ({fills ?? 0}/{target ?? 100} fills)</div>
      <div className="small mono">
        {entries.map(([k, v]) => (
          <div key={k} className={v ? 'pos' : 'neg'} data-testid="gate-row">
            {v ? '✓' : '✗'} {GATE_LABEL[k] || k}
          </div>
        ))}
        {!entries.length && <div className="muted">no fills yet — gate not evaluated</div>}
      </div>
    </div>
  )
}

export function PaperReport({ status }) {
  if (!status) return <Empty>No harness data yet.</Empty>
  const s = STATUS[status.status] || STATUS.research_only_not_validated
  const ci = status.ci95 || [0, 0]
  const book = status.l2_book || {}
  return (
    <div data-testid="paper-report">
      <div className={`diag-strip ${['bad', 'warn'].includes(s.kind) ? 'neg' : ''}`} data-testid="paper-status">
        🧪 {status.enabled ? 'ENABLED' : 'DISABLED'} · <b>{s.label}</b>
      </div>
      <div className="cards" style={{ marginTop: 8 }}>
        <div className="card"><div className="label">Paper quotes</div><div className="value" data-testid="quote-count">{status.quotes ?? 0}</div>
          <div className="sub">{status.skipped ?? 0} skipped</div></div>
        <div className="card"><div className="label">Paper fills</div><div className="value" data-testid="fill-count">{status.fills ?? 0}</div>
          <div className="sub">fill rate {pct(status.fill_rate)}</div></div>
        <div className="card"><div className="label">EV / fill</div>
          <div className={`value ${status.ev_per_fill > 0 ? 'pos' : 'neg'}`}>{num(status.ev_per_fill, 4)}</div>
          <div className="sub">CI [{num(ci[0], 3)}, {num(ci[1], 3)}]</div></div>
        <div className="card"><div className="label">P(true EV &gt; 0)</div>
          <div className={`value ${status.prob_ev_positive >= 0.95 ? 'pos' : ''}`} data-testid="p-ev">{num(status.prob_ev_positive, 3)}</div>
          <div className="sub">target ≥ 0.95</div></div>
        <div className="card"><div className="label">Weeks covered</div><div className="value">{status.weeks_covered ?? 0}</div></div>
        <div className="card"><div className="label">EV/day est</div><div className="value">{num(status.ev_per_day_estimate, 3)}</div></div>
      </div>

      <div className="cards" style={{ marginTop: 8 }}>
        <div className="card" style={{ flex: 1, minWidth: 220 }}><div className="label">Spread captured</div>
          <div className={`value ${status.spread_captured > 0 ? 'pos' : ''}`}>{num(status.spread_captured, 4)}</div></div>
        <div className="card" style={{ flex: 1, minWidth: 220 }}><div className="label">Adverse selection</div>
          <div className={`value ${status.adverse_selection <= 0 ? 'pos' : 'neg'}`}>{num(status.adverse_selection, 4)}</div>
          <div className="sub">{status.adverse_selection <= 0 ? 'favorable' : 'adverse'}</div></div>
        <div className="card" style={{ flex: 1, minWidth: 220 }}><div className="label">L2 book snapshots</div>
          <div className="value" data-testid="l2-status">{book.snapshots ?? 0}</div>
          <div className="sub">{book.with_book ?? 0} ok · {book.errors ?? 0} err · capture {book.capture_enabled ? 'on' : 'off'}</div></div>
      </div>

      <GateProgress gate={status.gate} fills={status.fills} target={status.fills_target} />
    </div>
  )
}

export function QuotesTable({ rows, testid = 'pm-quotes', kind = 'quotes' }) {
  if (!rows?.length) return <Empty>No {kind} yet.</Empty>
  return (
    <div className="table-wrap"><table data-testid={testid}>
      <thead><tr>
        <th>Market</th><th>Side</th><th className="right">Quote</th><th className="right">Bid/Ask</th>
        <th>Status</th><th className="right">Fill px</th><th className="right">Paper PnL</th><th className="right">Sprd cap</th><th>Note</th>
      </tr></thead>
      <tbody>{rows.map((q, i) => (
        <tr key={i} data-testid="pm-row">
          <td className="small mono">{(q.market_id || '').slice(0, 10)}… <span className="muted">{q.duration_minutes}m</span></td>
          <td className="small">{q.side}</td>
          <td className="right">{num(q.quote_price, 3)}</td>
          <td className="right small">{num(q.best_bid, 2)}/{num(q.best_ask, 2)}</td>
          <td className={`small ${q.filled ? 'pos' : ''}`}>{q.status}</td>
          <td className="right">{num(q.fill_price, 3)}</td>
          <td className={`right ${q.realized_pnl > 0 ? 'pos' : (q.realized_pnl < 0 ? 'neg' : '')}`}>{num(q.realized_pnl, 3)}</td>
          <td className="right">{num(q.spread_captured, 3)}</td>
          <td className="small muted" title={q.reason_not_filled || q.reason_skipped || ''}>{q.reason_not_filled ? 'no fill' : (q.reason_skipped ? 'skipped' : q.regime)}</td>
        </tr>
      ))}</tbody>
    </table></div>
  )
}

export default function Btc5mPassiveMakerPaper() {
  const [status, setStatus] = useState(null)
  const [quotes, setQuotes] = useState(null)
  const [fills, setFills] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState(null)

  const load = useCallback(async () => {
    try {
      const [st, q, f] = await Promise.all([
        api.btc5mPmPaperStatus().then((r) => r?.detail || r),
        api.btc5mPmPaperQuotes(30).then((r) => r?.detail || r).catch(() => null),
        api.btc5mPmPaperFills(30).then((r) => r?.detail || r).catch(() => null),
      ])
      setStatus(st); setQuotes(q?.quotes || []); setFills(f?.fills || [])
    } catch (e) { setToast(e.message) } finally { setLoading(false) }
  }, [])
  useEffect(() => { load() }, [load])
  useEffect(() => { if (!toast) return; const t = setTimeout(() => setToast(null), 6000); return () => clearTimeout(t) }, [toast])

  const runOnce = async () => {
    setBusy(true)
    try { const r = await api.btc5mPmPaperRunOnce().then((x) => x?.detail || x); setToast(JSON.stringify(r).slice(0, 140)); await load() }
    catch (e) { setToast(e.message) } finally { setBusy(false) }
  }

  if (loading) return <Loading />
  const s = status || {}

  return (
    <div>
      <div className="diag-strip" style={{ marginBottom: 10 }}>🧪 {s.safety}</div>
      <div className="page-head" style={{ marginBottom: 8 }}>
        <div>
          <h2 style={{ margin: 0 }}>BTC Passive-Maker Paper Harness</h2>
          <p className="muted small" style={{ margin: '2px 0 0' }}>
            Forward-collects PAPER quotes/fills (join_bid · 5s · worst-case queue) to validate the edge ·
            no orders, no live path · last run {ago(s.last_run_at)}
          </p>
        </div>
        <div className="toolbar" style={{ gap: 6 }}>
          <button onClick={runOnce} disabled={busy} data-testid="run-btn">{busy ? 'Running…' : 'Run once'}</button>
          <button className="secondary" onClick={load} disabled={busy}>↻ Refresh</button>
        </div>
      </div>

      {!s.enabled && (
        <div className="panel" style={{ marginBottom: 8 }} data-testid="disabled-note">
          <div className="small muted">Harness is <b>DISABLED</b> (default). Set <code>BTC_PASSIVE_MAKER_PAPER_ENABLED=true</code>
            to forward-collect. Run-once is a no-op while disabled. There is no live-execution path.</div>
        </div>
      )}

      <PaperReport status={status} />

      <h3 style={{ margin: '14px 0 4px' }}>Latest paper fills</h3>
      <QuotesTable rows={fills} testid="pm-fills" kind="fills" />

      <h3 style={{ margin: '14px 0 4px' }}>Latest paper quotes</h3>
      <QuotesTable rows={quotes} testid="pm-quotes" kind="quotes" />

      {toast && <div className="toast">{toast}</div>}
    </div>
  )
}
