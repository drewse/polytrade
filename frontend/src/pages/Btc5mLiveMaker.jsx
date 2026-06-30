import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { Loading, Empty } from '../components/common.jsx'

const num = (n, d = 2) => (n == null ? '—' : Number(n).toFixed(d))
const ms = (n) => (n == null ? '—' : `${Number(n).toFixed(0)}ms`)
const usd = (n) => (n == null ? '—' : `$${Number(n).toFixed(2)}`)

// Pure presentational — exported for tests.
export function StateBanner({ s }) {
  if (!s) return null
  const locked = s.locked
  const killed = s.kill
  const armed = s.armed
  const live = s.live_path_reachable
  const kind = (locked || killed) ? 'neg' : (armed && live ? 'neg' : (armed ? 'warn' : ''))
  const label = locked ? '🔒 LOCKED (cumulative loss stop)' : (killed ? '🛑 KILLED'
    : (armed ? `🟢 ARMED · ${s.mode?.toUpperCase()}${live ? ' · LIVE-MONEY' : ''}` : '⚪ DISARMED'))
  return (
    <div className={`diag-strip ${['neg', 'warn'].includes(kind) ? 'neg' : ''}`} data-testid="state-banner">
      {label} · master switch {s.enabled ? 'ENABLED' : 'OFF'} · key {s.has_key ? 'set' : 'absent'} ·
      live path {live ? <b className="neg">REACHABLE</b> : 'blocked'}
      {locked && s.lock_reason ? ` · ${s.lock_reason}` : ''}
    </div>
  )
}

export function ExposureCards({ s }) {
  if (!s) return null
  const c = s.caps || {}
  const eb = s.experiment_budget || {}
  return (
    <div className="cards" style={{ marginTop: 8 }}>
      <div className="card"><div className="label">Experiment budget</div>
        <div className="value" data-testid="budget">{usd(eb.committed_capital_usd)}<span className="sub"> / {usd(eb.max_experiment_capital_usd)}</span></div>
        <div className="sub">{usd(eb.remaining_usd)} free · software cap (ignores wallet)</div></div>
      <div className="card"><div className="label">Open exposure</div>
        <div className="value" data-testid="exposure">{usd(s.open_exposure_usd)}</div>
        <div className="sub">cap {usd(c.max_exposure_usd)} · {s.open_orders ?? 0} open</div></div>
      <div className="card"><div className="label">Session P&L</div>
        <div className={`value ${(s.session_realized_pnl || 0) >= 0 ? 'pos' : 'neg'}`}>{usd(s.session_realized_pnl)}</div>
        <div className="sub">stop −{usd(c.session_loss_limit_usd)}</div></div>
      <div className="card"><div className="label">Cumulative P&L</div>
        <div className={`value ${(eb.cumulative_realized_pnl || 0) >= 0 ? 'pos' : 'neg'}`}>{usd(eb.cumulative_realized_pnl)}</div>
        <div className="sub">LOCK at −{usd(eb.cumulative_loss_stop_usd)} · {usd(eb.loss_remaining_to_lock_usd)} left</div></div>
    </div>
  )
}

export function MakerMetrics({ m }) {
  if (!m) return null
  return (
    <div className="panel" style={{ marginTop: 8 }}>
      <div className="label">Execution metrics ({m.real_orders ?? 0} real orders · {m.shadow_orders ?? 0} shadow)</div>
      <div className="cards" data-testid="maker-metrics">
        <div className="card"><div className="label">Fill prob</div><div className="value">{m.fill_probability == null ? '—' : `${(m.fill_probability * 100).toFixed(0)}%`}</div>
          <div className="sub">{m.fills ?? 0} fills · {m.partial_fills ?? 0} partial</div></div>
        <div className="card"><div className="label">Submit / Ack</div><div className="value">{ms(m.avg_submit_latency_ms)}</div>
          <div className="sub">ack {ms(m.avg_ack_latency_ms)}</div></div>
        <div className="card"><div className="label">Time-to-fill</div><div className="value">{ms(m.avg_fill_latency_ms)}</div></div>
        <div className="card"><div className="label">Cancel lat / success</div><div className="value">{ms(m.avg_cancel_latency_ms)}</div>
          <div className="sub">{m.cancel_success_rate == null ? '—' : `${(m.cancel_success_rate * 100).toFixed(0)}%`}</div></div>
        <div className="card"><div className="label">Realized spread</div><div className={`value ${m.avg_realized_spread > 0 ? 'pos' : ''}`}>{num(m.avg_realized_spread, 4)}</div></div>
        <div className="card"><div className="label">Adverse 5s</div><div className={`value ${m.avg_adverse_5s < 0 ? 'neg' : 'pos'}`}>{num(m.avg_adverse_5s, 4)}</div></div>
        <div className="card"><div className="label">Net P&L (after fees)</div><div className={`value ${m.net_pnl_usd >= 0 ? 'pos' : 'neg'}`}>{usd(m.net_pnl_usd)}</div></div>
      </div>
    </div>
  )
}

export function SessionSummary({ summary }) {
  if (!summary) return <Empty>No session summary yet — runs after a session ends.</Empty>
  const cf = summary.counterfactual || {}
  return (
    <div className="panel" data-testid="session-summary">
      <div className="label">Auto research summary — session {summary.session_id}
        ({summary.orders_posted ?? 0} orders · {summary.settled_fills ?? 0} settled)</div>
      <div className="cards">
        <div className="card"><div className="label">Fill rate</div><div className="value">{summary.fill_rate == null ? '—' : `${(summary.fill_rate * 100).toFixed(0)}%`}</div></div>
        <div className="card"><div className="label">Avg queue life</div><div className="value">{ms(summary.avg_queue_lifetime_ms)}</div></div>
        <div className="card"><div className="label">Submit/ack lat</div><div className="value">{ms(summary.avg_submit_latency_ms)}</div><div className="sub">ack {ms(summary.avg_ack_latency_ms)}</div></div>
        <div className="card"><div className="label">Realized spread</div><div className={`value ${summary.avg_realized_spread > 0 ? 'pos' : ''}`}>{num(summary.avg_realized_spread, 4)}</div></div>
        <div className="card"><div className="label">Adverse 5s</div><div className={`value ${summary.avg_adverse_5s < 0 ? 'neg' : 'pos'}`}>{num(summary.avg_adverse_5s, 4)}</div></div>
        <div className="card"><div className="label">Net P&L</div><div className={`value ${summary.net_pnl_usd >= 0 ? 'pos' : 'neg'}`}>{usd(summary.net_pnl_usd)}</div></div>
      </div>
      <div className="small" style={{ marginTop: 6 }}>
        Best quote distance: <b className="pos">{summary.best_quote_distance || '—'}</b> · worst: <b className="neg">{summary.worst_quote_distance || '—'}</b> ·
        counterfactual — actual best {cf.actual_best ?? 0}, +1 tick better {cf.one_tick_higher_better ?? 0}, −1 tick better {cf.one_tick_lower_better ?? 0} (of {cf.n ?? 0})
      </div>
      {summary.patterns?.length > 0 && (
        <div className="small" data-testid="patterns" style={{ marginTop: 4 }}>📈 Patterns: {summary.patterns.join(' · ')}</div>
      )}
      {summary.suggested_parameter_changes?.length > 0 && (
        <div className="small" data-testid="suggestions" style={{ marginTop: 4 }}>
          🔧 Suggested next session: <ul style={{ margin: '2px 0 0 16px' }}>{summary.suggested_parameter_changes.map((s, i) => <li key={i}>{s}</li>)}</ul>
        </div>
      )}
      <div className="small muted" style={{ marginTop: 4 }}>{summary.note}</div>
    </div>
  )
}

export function OrdersTable({ orders }) {
  if (!orders?.length) return <Empty>No orders yet.</Empty>
  return (
    <div className="table-wrap"><table data-testid="orders-table">
      <thead><tr><th>Market</th><th className="right">TTR</th><th className="right">Bid/Ask</th><th className="right">Spread</th>
        <th className="right">Price</th><th className="right">Edge</th><th>Status</th><th className="right">Adv5s</th>
        <th className="right">P&L</th><th>Better at</th></tr></thead>
      <tbody>{orders.map((o) => (
        <tr key={o.client_id} data-testid="order-row" title={o.selection_reason || ''}>
          <td className="small">{(o.title || o.market_id || '').slice(0, 22)}</td>
          <td className="right small">{o.secs_to_resolution == null ? '—' : `${o.secs_to_resolution}s`}</td>
          <td className="right small">{num(o.best_bid, 2)}/{num(o.best_ask, 2)}</td>
          <td className="right">{num(o.spread, 3)}</td>
          <td className="right">{num(o.price, 3)}</td>
          <td className={`right ${o.estimated_edge > 0 ? 'pos' : ''}`}>{num(o.estimated_edge, 3)}</td>
          <td className={`small ${o.filled ? 'pos' : ''}`}>{o.status}</td>
          <td className={`right ${o.adverse_5s < 0 ? 'neg' : ''}`}>{num(o.adverse_5s, 3)}</td>
          <td className={`right ${o.realized_pnl > 0 ? 'pos' : (o.realized_pnl < 0 ? 'neg' : '')}`}>{num(o.realized_pnl, 3)}</td>
          <td className="small">{o.counterfactual?.best_choice ? o.counterfactual.best_choice.replace(/_/g, ' ') : '—'}</td>
        </tr>
      ))}</tbody>
    </table></div>
  )
}

export function EventLog({ events }) {
  if (!events?.length) return <Empty>No events yet.</Empty>
  return (
    <div className="table-wrap"><table data-testid="event-log">
      <thead><tr><th>Time</th><th>Type</th><th>Order</th><th>Detail</th></tr></thead>
      <tbody>{events.map((e, i) => (
        <tr key={i} data-testid="event-row">
          <td className="small mono">{e.ts?.slice(11, 19)}</td>
          <td className={`small ${['kill', 'reject', 'error'].includes(e.type) ? 'neg' : (e.type === 'fill' ? 'pos' : '')}`}>{e.type}</td>
          <td className="small mono">{e.order_client_id ? e.order_client_id.slice(0, 8) : '—'}</td>
          <td className="small muted">{JSON.stringify(e.payload).slice(0, 80)}</td>
        </tr>
      ))}</tbody>
    </table></div>
  )
}

export default function Btc5mLiveMaker() {
  const [s, setS] = useState(null)
  const [events, setEvents] = useState([])
  const [orders, setOrders] = useState([])
  const [summary, setSummary] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [toast, setToast] = useState(null)

  const load = useCallback(async () => {
    try {
      const [st, ev, od, sm] = await Promise.all([
        api.btc5mLiveMakerStatus().then((r) => r?.detail || r),
        api.btc5mLiveMakerEvents(60).then((r) => r?.detail || r).catch(() => null),
        api.btc5mLiveMakerOrders(40).then((r) => r?.detail || r).catch(() => null),
        api.btc5mLiveMakerSummary().then((r) => r?.detail || r).catch(() => null),
      ])
      setS(st); setEvents(ev?.events || []); setOrders(od?.orders || []); setSummary(sm?.summary || null)
    } catch (e) { setToast(e.message) } finally { setLoading(false) }
  }, [])
  useEffect(() => { load() }, [load])
  useEffect(() => { if (!toast) return; const t = setTimeout(() => setToast(null), 6000); return () => clearTimeout(t) }, [toast])

  const act = async (key, fn, confirmMsg) => {
    if (confirmMsg && !window.confirm(confirmMsg)) return
    setBusy(key)
    try { const r = await fn().then((x) => x?.detail || x); setToast(JSON.stringify(r).slice(0, 140)); await load() }
    catch (e) { setToast(e.message) } finally { setBusy('') }
  }

  if (loading) return <Loading />

  return (
    <div>
      <div className="diag-strip" style={{ marginBottom: 8 }}>🧪 {s?.safety}</div>
      <div className="page-head" style={{ marginBottom: 8 }}>
        <h2 style={{ margin: 0 }}>BTC 5M Live Maker — execution trial</h2>
        <div className="toolbar" style={{ gap: 6 }}>
          <button className="secondary" onClick={() => act('shadow', () => api.btc5mLiveMakerArm('shadow', 20))} disabled={busy} data-testid="arm-shadow">Arm SHADOW</button>
          <button onClick={() => act('live', () => api.btc5mLiveMakerArm('live', 20), 'Arm a LIVE real-money session?')} disabled={busy} data-testid="arm-live">Arm LIVE</button>
          <button className="secondary" onClick={() => act('cycle', api.btc5mLiveMakerRunCycle)} disabled={busy} data-testid="cycle">Run cycle</button>
          <button className="secondary" onClick={() => act('disarm', api.btc5mLiveMakerDisarm)} disabled={busy}>Disarm</button>
          <button onClick={() => act('kill', api.btc5mLiveMakerKill, 'KILL: cancel all orders + latch the kill flag?')} disabled={busy} data-testid="kill"
            style={{ background: '#b00', color: '#fff', fontWeight: 700 }}>🛑 KILL</button>
          {s?.locked && <button onClick={() => act('reset', api.btc5mLiveMakerResetLock, 'Clear the permanent cumulative-loss LOCK?')} disabled={busy} data-testid="reset-lock">Reset lock</button>}
          <button className="secondary" onClick={() => act('reconcile', api.btc5mLiveMakerReconcile)} disabled={busy} data-testid="reconcile">Reconcile</button>
          <button className="secondary" onClick={load} disabled={busy}>↻</button>
        </div>
      </div>

      <StateBanner s={s} />
      <ExposureCards s={s} />
      <MakerMetrics m={s?.metrics} />

      <h3 style={{ margin: '12px 0 4px' }}>Session research summary</h3>
      <SessionSummary summary={summary} />

      <h3 style={{ margin: '12px 0 4px' }}>Order decisions (research dataset — hover for selection reason)</h3>
      <OrdersTable orders={orders} />

      <h3 style={{ margin: '12px 0 4px' }}>Event log (replay source of truth)</h3>
      <EventLog events={events} />

      {toast && <div className="toast">{toast}</div>}
    </div>
  )
}
