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

const STAGE_LABEL = {
  '1_btc_markets_in_main': 'BTC markets in Market table',
  '2_btc5m_indexed': 'btc5m indexed markets',
  '2b_btc5m_resolved': '  ↳ resolved',
  '3_lab_markets': 'Lab dataset markets',
  '3b_lab_points': '  ↳ lab points',
  '4_paper_quotes': 'Paper quotes',
  '5_paper_fills': 'Paper fills',
  '6_settled_fills': 'Settled fills',
}

// Pure presentational — exported for tests.
export function FunnelDiagnostics({ diag }) {
  if (!diag) return <Empty>No forward-pipeline diagnostics yet.</Empty>
  const f = diag.funnel || {}
  const blocked = diag.blocked_stages || []
  return (
    <div className="panel" data-testid="funnel-diag">
      <div className="label">Data funnel — forward worker {diag.forward_enabled ? 'ENABLED' : 'DISABLED'} ·
        main ingest {diag.main_ingest?.running ? 'running' : 'idle'} · last run {diag.last_run_at ? ago(diag.last_run_at) : 'never'}</div>
      {diag.pipeline_blocked && (
        <div className="diag-strip neg" data-testid="stall-warning" style={{ margin: '6px 0' }}>
          ⛔ Pipeline STALLED at: <b>{blocked.map((b) => STAGE_LABEL[b] || b).join(', ')}</b> — upstream data isn't converting downstream.
        </div>
      )}
      <div className="table-wrap"><table data-testid="funnel-table">
        <thead><tr><th>Stage</th><th className="right">Total</th><th className="right">New</th><th>Latest</th><th>Status</th></tr></thead>
        <tbody>{Object.entries(f).map(([k, v]) => (
          <tr key={k} data-testid="funnel-row" className={v.blocked ? 'neg' : ''}>
            <td className="small">{STAGE_LABEL[k] || k}</td>
            <td className="right"><b>{v.total}</b></td>
            <td className={`right ${v.new_since_last > 0 ? 'pos' : 'muted'}`}>{v.new_since_last > 0 ? `+${v.new_since_last}` : '—'}</td>
            <td className="small muted">{v.latest_ts ? ago(v.latest_ts) : '—'}</td>
            <td className="small">{v.blocked ? <span className="neg">⛔ blocked</span> : <span className="pos">ok</span>}</td>
          </tr>
        ))}</tbody>
      </table></div>
      {diag.last_summary && Object.keys(diag.last_summary).length > 0 && (
        <div className="small muted" style={{ marginTop: 4 }}>last cycle: {JSON.stringify(diag.last_summary)}</div>
      )}
    </div>
  )
}

export function FamilyBreakdown({ breakdown }) {
  const entries = Object.entries(breakdown || {})
  if (!entries.length) return <Empty>No cohorts yet.</Empty>
  return (
    <div className="table-wrap"><table data-testid="family-breakdown">
      <thead><tr><th>Cohort (family:kind)</th><th className="right">Quotes</th><th className="right">Fills</th>
        <th className="right">EV/fill</th><th className="right">P(EV&gt;0)</th><th>Gate</th></tr></thead>
      <tbody>{entries.map(([k, v]) => (
        <tr key={k} data-testid="cohort-row" className={k === 'btc:independent' ? 'pos' : ''}>
          <td className="small mono">{k}{k === 'btc:independent' ? ' ★ (THE gate)' : ''}</td>
          <td className="right">{v.quotes}</td>
          <td className="right">{v.fills}</td>
          <td className={`right ${v.ev_per_fill > 0 ? 'pos' : (v.ev_per_fill < 0 ? 'neg' : '')}`}>{num(v.ev_per_fill, 4)}</td>
          <td className="right">{num(v.prob_ev_positive, 3)}</td>
          <td className="small">{v.gate_passed}/{v.gate_total} · {v.gate_status === 'paper_validated' ? <span className="pos">validated</span> : (v.gate_status === 'failed_validation' ? <span className="neg">failed</span> : 'open')}</td>
        </tr>
      ))}</tbody>
    </table></div>
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
  const [diag, setDiag] = useState(null)
  const [quotes, setQuotes] = useState(null)
  const [fills, setFills] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState(null)

  const load = useCallback(async () => {
    try {
      const [st, dg, q, f] = await Promise.all([
        api.btc5mPmPaperStatus().then((r) => r?.detail || r),
        api.btc5mPmForwardDiagnostics().then((r) => r?.detail || r).catch(() => null),
        api.btc5mPmPaperQuotes(30).then((r) => r?.detail || r).catch(() => null),
        api.btc5mPmPaperFills(30).then((r) => r?.detail || r).catch(() => null),
      ])
      setStatus(st); setDiag(dg); setQuotes(q?.quotes || []); setFills(f?.fills || [])
    } catch (e) { setToast(e.message) } finally { setLoading(false) }
  }, [])
  useEffect(() => { load() }, [load])
  useEffect(() => { if (!toast) return; const t = setTimeout(() => setToast(null), 6000); return () => clearTimeout(t) }, [toast])

  const act = async (fn) => {
    setBusy(true)
    try { const r = await fn().then((x) => x?.detail || x); setToast(JSON.stringify(r).slice(0, 140)); await load() }
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
          <button onClick={() => act(api.btc5mPmPaperRunOnce)} disabled={busy} data-testid="run-btn">{busy ? 'Running…' : 'Run quote cycle'}</button>
          <button className="secondary" onClick={() => act(api.btc5mPmForwardRunOnce)} disabled={busy} data-testid="fwd-btn">Run forward cycle</button>
          <button className="secondary" onClick={load} disabled={busy}>↻ Refresh</button>
        </div>
      </div>

      {!s.enabled && (
        <div className="panel" style={{ marginBottom: 8 }} data-testid="disabled-note">
          <div className="small muted">Harness is <b>DISABLED</b> (default). Set <code>BTC_PASSIVE_MAKER_PAPER_ENABLED=true</code>
            (and <code>BTC_PASSIVE_MAKER_FORWARD_ENABLED=true</code> for auto-conversion) to forward-collect.
            Run-once is a no-op while disabled. There is no live-execution path.</div>
        </div>
      )}

      <FunnelDiagnostics diag={diag} />

      <PaperReport status={status} />

      <h3 style={{ margin: '14px 0 4px' }}>Cohorts — BTC gate vs broad universe vs multi-point (each gated separately)</h3>
      <FamilyBreakdown breakdown={s.family_breakdown} />

      <h3 style={{ margin: '14px 0 4px' }}>Latest paper fills</h3>
      <QuotesTable rows={fills} testid="pm-fills" kind="fills" />

      <h3 style={{ margin: '14px 0 4px' }}>Latest paper quotes</h3>
      <QuotesTable rows={quotes} testid="pm-quotes" kind="quotes" />

      {toast && <div className="toast">{toast}</div>}
    </div>
  )
}
