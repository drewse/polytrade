import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { Loading, Empty } from '../components/common.jsx'

const num = (n, d = 2) => (n == null ? '—' : Number(n).toFixed(d))
const ms = (n) => (n == null ? '—' : `${Number(n).toFixed(0)}ms`)
const usd = (n) => (n == null ? '—' : `$${Number(n).toFixed(2)}`)

// Pure presentational — exported for tests.
export function StateBanner({ s }) {
  if (!s) return null
  const killed = s.kill
  const armed = s.armed
  const live = s.live_path_reachable
  const kind = killed ? 'neg' : (armed && live ? 'neg' : (armed ? 'warn' : ''))
  const label = killed ? '🛑 KILLED' : (armed ? `🟢 ARMED · ${s.mode?.toUpperCase()}${live ? ' · LIVE-MONEY' : ''}` : '⚪ DISARMED')
  return (
    <div className={`diag-strip ${['neg', 'warn'].includes(kind) ? 'neg' : ''}`} data-testid="state-banner">
      {label} · master switch {s.enabled ? 'ENABLED' : 'OFF'} · key {s.has_key ? 'set' : 'absent'} ·
      live path {live ? <b className="neg">REACHABLE</b> : 'blocked'}
    </div>
  )
}

export function ExposureCards({ s }) {
  if (!s) return null
  const c = s.caps || {}
  return (
    <div className="cards" style={{ marginTop: 8 }}>
      <div className="card"><div className="label">Open exposure</div>
        <div className="value" data-testid="exposure">{usd(s.open_exposure_usd)}</div>
        <div className="sub">cap {usd(c.max_exposure_usd)} · {s.open_orders ?? 0} open</div></div>
      <div className="card"><div className="label">Deployed (cumulative)</div>
        <div className="value">{usd(s.deployed_usd)}</div><div className="sub">cap {usd(c.total_cap_usd)}</div></div>
      <div className="card"><div className="label">Session P&L</div>
        <div className={`value ${(s.session_realized_pnl || 0) >= 0 ? 'pos' : 'neg'}`}>{usd(s.session_realized_pnl)}</div>
        <div className="sub">stop −{usd(c.session_loss_limit_usd)}</div></div>
      <div className="card"><div className="label">Per-order / lifetime</div>
        <div className="value">{usd(c.per_order_usd)}</div><div className="sub">{c.queue_lifetime_s}s rest</div></div>
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
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [toast, setToast] = useState(null)

  const load = useCallback(async () => {
    try {
      const [st, ev] = await Promise.all([
        api.btc5mLiveMakerStatus().then((r) => r?.detail || r),
        api.btc5mLiveMakerEvents(60).then((r) => r?.detail || r).catch(() => null),
      ])
      setS(st); setEvents(ev?.events || [])
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
          <button className="secondary" onClick={load} disabled={busy}>↻</button>
        </div>
      </div>

      <StateBanner s={s} />
      <ExposureCards s={s} />
      <MakerMetrics m={s?.metrics} />

      <h3 style={{ margin: '12px 0 4px' }}>Event log (replay source of truth)</h3>
      <EventLog events={events} />

      {toast && <div className="toast">{toast}</div>}
    </div>
  )
}
