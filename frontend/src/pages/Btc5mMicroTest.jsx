import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { Loading, Empty, WalletLink } from '../components/common.jsx'

const num = (n, d = 2) => (n == null ? '—' : Number(n).toFixed(d))
const pct = (n, d = 1) => (n == null ? '—' : `${(Number(n) * 100).toFixed(d)}%`)
const usd = (n) => (n == null ? '—' : `$${Number(n).toFixed(2)}`)
const short = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : '—')

function StateBadge({ s }) {
  if (!s?.enabled) return <span className="badge neutral" data-testid="mt-state">disabled (env)</span>
  if (s.stopped) return <span className="badge bad" data-testid="mt-state">stopped — re-arm required</span>
  if (s.armed) return <span className="badge yes" data-testid="mt-state">● armed</span>
  return <span className="badge open" data-testid="mt-state">enabled · disarmed</span>
}

// Pure + interactive panel — exported for tests. All side effects go through the
// on* callbacks; this renders state only.
export function MicroTestPanel({ status, onArm, onDisarm, onRunPaper, onRunLive, busy }) {
  const s = status
  if (!s) return <Empty>No micro-test status.</Empty>
  const c = s.config || {}
  const active = s.active_position
  return (
    <div data-testid="micro-test-panel">
      <div className="diag-strip" style={{ marginBottom: 10 }}>
        🧪 Isolated minimum-size BTC 5M micro-test. {s.safety}
      </div>

      <div className="page-head" style={{ marginBottom: 8 }}>
        <h3 style={{ margin: 0 }}>BTC 5M Micro-Test <StateBadge s={s} /></h3>
        <div className="toolbar" style={{ gap: 6 }}>
          <button className="secondary small" onClick={onRunPaper} disabled={busy} data-testid="run-paper">
            {busy ? '…' : '▶ Run paper cycle'}
          </button>
          {s.armed
            ? <button className="danger small" onClick={onDisarm} disabled={busy} data-testid="disarm-btn">Disarm</button>
            : <button className="small" onClick={onArm} disabled={busy || !s.enabled} data-testid="arm-btn">⚡ Arm (live)</button>}
          <button className="danger small" onClick={onRunLive} disabled={busy || !s.armed} data-testid="run-live"
            title="Place at most one real minimum-size order if a signal qualifies">Run LIVE cycle</button>
        </div>
      </div>

      {s.stopped && <div className="diag-strip neg" style={{ marginBottom: 8 }} data-testid="stop-banner">
        ■ Stopped: {s.stop_reason}. Manual re-arm required.</div>}

      <div className="cards">
        <div className="card"><div className="label">Status</div><div className="value"><StateBadge s={s} /></div>
          <div className="sub">{s.armed_by ? `armed by ${s.armed_by}` : 'not armed'}</div></div>
        <div className="card"><div className="label">Realized test P/L</div>
          <div className={`value ${s.realized_pnl > 0 ? 'pos' : s.realized_pnl < 0 ? 'neg' : ''}`}>{usd(s.realized_pnl)}</div>
          <div className="sub">paper twin {usd(s.paper_realized_pnl)} · Δ {usd(s.paper_vs_live_delta)}</div></div>
        <div className="card"><div className="label">Unrealized test P/L</div><div className="value">{usd(s.unrealized_pnl)}</div></div>
        <div className="card"><div className="label">Test trades</div><div className="value">{s.test_trades ?? 0}<span className="sub"> / {c.max_trades}</span></div>
          <div className="sub">{s.trades_remaining} remaining</div></div>
        <div className="card"><div className="label">Win rate</div><div className="value">{pct(s.win_rate)}</div></div>
        <div className="card"><div className="label">Max loss remaining</div><div className="value">{usd(s.max_loss_remaining)}</div>
          <div className="sub">day {usd(s.day_loss_remaining)}</div></div>
        <div className="card"><div className="label">Open positions</div><div className="value">{s.open_positions ?? 0}<span className="sub"> / {c.max_concurrent}</span></div></div>
        <div className="card"><div className="label">Max loss / trade</div><div className="value neg">{usd(c.expected_max_loss_per_trade)}</div>
          <div className="sub">{c.fixed_shares} sh × ≤{usd(c.max_entry_price)}</div></div>
      </div>

      <div className="cards" style={{ marginTop: 10 }}>
        <div className="card" style={{ flex: 1, minWidth: 280 }}>
          <div className="label">Configuration</div>
          <div className="small">Primary: <span className="mono">{c.primary_wallet ? <WalletLink address={c.primary_wallet} /> : '— not set —'}</span></div>
          <div className="small">Backups: {(c.backup_wallets || []).length
            ? c.backup_wallets.map((w) => <span key={w} className="mono" style={{ marginRight: 6 }}><WalletLink address={w} /></span>)
            : '—'}</div>
          <div className="small muted" style={{ marginTop: 4 }}>
            fixed {c.fixed_shares} shares · max entry {usd(c.max_entry_price)} · ≥{c.min_seconds_remaining}s left ·
            regimes [{(c.allowed_regimes || []).join(', ')}] · confidence gate {String(c.require_confidence)} (≥{c.min_confidence})
          </div>
          <div className="small muted">stops: daily {usd(c.daily_loss_stop)} · total {usd(c.total_loss_stop)} · max {c.max_trades} trades · {c.max_concurrent} concurrent</div>
        </div>
        <div className="card" style={{ flex: 1, minWidth: 280 }}>
          <div className="label">Latest signal</div>
          <div className="small mono">{s.last_signal || '— none —'}</div>
          <div className="label" style={{ marginTop: 8 }}>Latest rejection</div>
          <div className="small neg">{s.last_rejection || '— none —'}</div>
        </div>
      </div>

      <h4 style={{ margin: '12px 0 4px' }}>Active test position</h4>
      {!active ? <Empty>No active BTC 5M micro-test position.</Empty> : (
        <div className="table-wrap">
          <table data-testid="active-table"><thead><tr>
            <th>Market</th><th>Dir</th><th>Wallet</th><th className="right">Ref</th><th className="right">Fill</th>
            <th className="right">Shares</th><th className="right">Stake</th><th>Status</th>
          </tr></thead><tbody>
            <tr>
              <td className="small">{(active.market || active.market_id || '').slice(0, 40)}</td>
              <td><span className="badge open">{active.direction}</span></td>
              <td className="mono"><WalletLink address={active.wallet} /> <span className="muted small">({active.role})</span></td>
              <td className="right">{num(active.reference_price, 3)}</td>
              <td className="right">{num(active.fill_price, 3)}</td>
              <td className="right">{num(active.shares, 2)}</td>
              <td className="right">{usd(active.size_usd)}</td>
              <td><span className="badge yes">{active.status}</span></td>
            </tr>
          </tbody></table>
        </div>
      )}

      <h4 style={{ margin: '12px 0 4px' }}>Recent test trades (paper-vs-live) — {(s.recent_trades || []).length}</h4>
      {!(s.recent_trades || []).length ? <Empty>No micro-test trades yet.</Empty> : (
        <div className="table-wrap">
          <table data-testid="trades-table"><thead><tr>
            <th>Time</th><th>Mkt</th><th>Dir</th><th>Wallet</th><th>Exec</th><th className="right">Ref</th>
            <th className="right">Fill</th><th className="right">Stake</th><th>Outcome</th>
            <th className="right">Live P/L</th><th className="right">Paper P/L</th><th>Reason</th>
          </tr></thead><tbody>
            {s.recent_trades.map((t) => (
              <tr key={t.id} data-testid="trade-row">
                <td className="muted small">{t.created_at ? t.created_at.slice(5, 16).replace('T', ' ') : '—'}</td>
                <td className="small" title={t.market}>{(t.market || t.market_id || '').slice(0, 18)}</td>
                <td className="small">{t.direction}</td>
                <td className="mono small">{short(t.wallet)}</td>
                <td><span className={`badge ${t.executor === 'polymarket' ? 'yes' : 'neutral'}`}>{t.executor}</span></td>
                <td className="right">{num(t.reference_price, 3)}</td>
                <td className="right">{num(t.fill_price, 3)}</td>
                <td className="right">{usd(t.size_usd)}</td>
                <td><span className={`badge ${t.status === 'rejected' ? 'bad' : t.status === 'closed' ? 'closed' : 'open'}`}>{t.fill_outcome || t.status}</span></td>
                <td className={`right ${(t.realized_pnl ?? 0) > 0 ? 'pos' : (t.realized_pnl ?? 0) < 0 ? 'neg' : ''}`}>{t.realized_pnl == null ? '—' : usd(t.realized_pnl)}</td>
                <td className="right muted">{t.paper_realized_pnl == null ? '—' : usd(t.paper_realized_pnl)}</td>
                <td className="small neg" title={t.venue_error || t.rejection_reason || ''}>{(t.rejection_reason || t.venue_error || '').slice(0, 30)}</td>
              </tr>
            ))}
          </tbody></table>
        </div>
      )}
    </div>
  )
}

// Data-fetching wrapper used by the BTC 5M Reversal "Micro-Test (Live)" section.
export default function Btc5mMicroTest() {
  const [status, setStatus] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState(null)

  const load = useCallback(async () => {
    try { setStatus(await api.btc5mMicroTestStatus().then((r) => r?.detail || r)); setError(null) }
    catch (e) { setError(e.message) } finally { setLoading(false) }
  }, [])
  useEffect(() => { load() }, [load])
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 4500)
    return () => clearTimeout(t)
  }, [toast])

  const act = async (fn, confirmMsg) => {
    if (confirmMsg && !window.confirm(confirmMsg)) return
    setBusy(true)
    try {
      const r = await fn().then((x) => x?.detail || x)
      setToast(r?.reason || r?.error || (r?.ran === false ? `no-op: ${r.reason || ''}` : 'done'))
      await load()
    } catch (e) { setToast(e.message) } finally { setBusy(false) }
  }

  if (loading) return <Loading />
  if (error) return <Empty>Micro-test status unavailable: {error}</Empty>

  return (
    <div>
      <MicroTestPanel
        status={status}
        busy={busy}
        onArm={() => act(() => api.btc5mMicroTestArm('dashboard'),
          'ARM the BTC 5M micro-test? Once armed, a single minimum-size REAL order may be placed when the primary wallet opens a qualifying BTC 5M position (only if you then run a LIVE cycle / the worker is live). Continue?')}
        onDisarm={() => act(() => api.btc5mMicroTestDisarm())}
        onRunPaper={() => act(() => api.btc5mMicroTestRunOnce(false))}
        onRunLive={() => act(() => api.btc5mMicroTestRunOnce(true),
          'Run a LIVE micro-test cycle now? This may place ONE real minimum-size (5-share) order if a signal qualifies. Max loss for the trade ≤ $3. Continue?')}
      />
      {toast && <div className="toast">{toast}</div>}
    </div>
  )
}
