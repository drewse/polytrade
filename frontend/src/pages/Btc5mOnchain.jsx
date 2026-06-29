import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { Loading, Empty, WalletLink } from '../components/common.jsx'

const secs = (n) => (n == null ? '—' : `${Number(n).toFixed(1)}s`)
const pct = (n) => (n == null ? '—' : `${Number(n).toFixed(0)}%`)
const short = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : '—')

const VERDICT = {
  viable: { kind: 'yes', label: '✓ VIABLE' },
  marginal: { kind: 'warn', label: '~ MARGINAL' },
  not_viable: { kind: 'bad', label: '✗ NOT VIABLE' },
  insufficient_data: { kind: 'neutral', label: '… INSUFFICIENT DATA' },
}

// diagnosis code -> tone (answers "why 0 signals?")
const DIAGNOSIS_TONE = {
  detecting: '', not_started: 'warn', rpc_not_configured: 'warn',
  rpc_log_issue: 'neg', no_watched_trade: 'warn', token_map_issue: 'neg', all_ignored: 'warn',
}
const ago = (iso) => {
  if (!iso) return 'never'
  const s = (Date.now() - new Date(iso).getTime()) / 1000
  if (s < 60) return `${Math.floor(s)}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  return `${Math.floor(s / 3600)}h ago`
}

// Read-only diagnostics — pure presentational, exported for tests.
export function DiagnosticsPanel({ diagnostics, diagnosis }) {
  const d = diagnostics || {}
  const dg = diagnosis || {}
  const tm = d.token_map || {}
  const ignored = Object.entries(d.ignored_by_reason || {}).sort((a, b) => b[1] - a[1])
  return (
    <div data-testid="diagnostics-panel">
      <h4 style={{ margin: '12px 0 4px' }}>Diagnostics <span className="badge sharp">read-only</span></h4>
      <div className={`diag-strip ${DIAGNOSIS_TONE[dg.code] || ''}`} data-testid="diagnosis-banner" style={{ marginBottom: 8 }}>
        🔎 <b>{(dg.code || 'unknown').replace(/_/g, ' ')}</b> — {dg.message}
      </div>
      <div className="cards">
        <div className="card"><div className="label">Blocks scanned</div><div className="value">{d.blocks_scanned ?? 0}</div>
          <div className="sub">last block {d.last_block_scanned ?? '—'}</div></div>
        <div className="card"><div className="label">OrderFilled seen</div><div className="value">{d.logs_scanned ?? 0}</div>
          <div className="sub">all wallets (chain health)</div></div>
        <div className="card"><div className="label">Decoded (watched filter)</div><div className="value">{d.orderfilled_decoded ?? 0}</div></div>
        <div className="card"><div className="label">Matching watched wallets</div><div className={`value ${d.events_matching_watched ? 'pos' : ''}`}>{d.events_matching_watched ?? 0}</div></div>
        <div className="card"><div className="label">BTC token-map matches</div><div className={`value ${d.btc_token_map_matches ? 'pos' : ''}`}>{d.btc_token_map_matches ?? 0}</div></div>
        <div className="card"><div className="label">Detector errors</div><div className={`value ${d.error_count ? 'neg' : ''}`}>{d.error_count ?? 0}</div>
          <div className="sub" title={d.last_error || ''}>{d.last_error ? String(d.last_error).slice(0, 24) : 'none'}</div></div>
      </div>

      <div className="cards" style={{ marginTop: 10 }}>
        <div className="card" style={{ flex: 1, minWidth: 300 }}>
          <div className="label">Last events</div>
          <div className="small"><b>OrderFilled:</b> {d.last_orderfilled || '—'} <span className="muted">{ago(d.last_orderfilled_at)}</span></div>
          <div className="small"><b>Watched-wallet:</b> {d.last_watched_event || '—'} <span className="muted">{ago(d.last_watched_event_at)}</span></div>
          <div className="small"><b>BTC market:</b> {d.last_btc_market_event || '—'} <span className="muted">{ago(d.last_btc_market_event_at)}</span></div>
        </div>
        <div className="card" style={{ flex: 1, minWidth: 240 }}>
          <div className="label">RPC endpoint</div>
          {(() => {
            const rpc = d.rpc || {}
            return (
              <>
                <div className="small">scheme <b className={rpc.scheme === 'https' ? 'pos' : 'neg'}>{rpc.scheme || '—'}</b>
                  {rpc.host ? <> · {rpc.host}</> : null}</div>
                <div className="small muted">from {rpc.source || '—'}{rpc.converted_from_wss ? ' (converted wss→https)' : ''} · needs {rpc.requires}</div>
                {rpc.config_error && <div className="small neg" data-testid="rpc-config-error">⚠ {rpc.config_error}</div>}
                {rpc.note && !rpc.config_error && <div className="small muted">ℹ {rpc.note}</div>}
              </>
            )
          })()}
        </div>
      </div>

      <div className="cards" style={{ marginTop: 10 }}>
        <div className="card" style={{ flex: 1, minWidth: 240 }}>
          <div className="label">Token map</div>
          <div className="small">size <b>{tm.size ?? 0}</b> · refreshed {ago(tm.refreshed_at)}</div>
          {tm.error && <div className="small neg">⚠ {tm.error}</div>}
        </div>
        <div className="card" style={{ flex: 1, minWidth: 240 }}>
          <div className="label">Ignored by reason</div>
          {ignored.length
            ? ignored.map(([r, n]) => <div key={r} className="small"><span className="badge neutral">{n}</span> {r}</div>)
            : <div className="small muted">none</div>}
        </div>
      </div>
    </div>
  )
}

function SignalsTable({ rows, testid, empty }) {
  if (!rows?.length) return <Empty>{empty}</Empty>
  return (
    <div className="table-wrap">
      <table data-testid={testid}>
        <thead><tr>
          <th>Block</th><th>Wallet</th><th>Role</th><th>Market</th><th>Dir</th><th>Side</th>
          <th className="right">Price</th><th className="right">Detect</th><th className="right">Drift</th>
          <th className="right">Secs left</th><th>Reason</th>
        </tr></thead>
        <tbody>
          {rows.map((s) => (
            <tr key={s.id} data-testid="onchain-row">
              <td className="small mono">{s.block_number}</td>
              <td className="mono small">{short(s.watched_wallet)}</td>
              <td className="small">{s.wallet_role}</td>
              <td className="small" title={s.question}>{(s.question || s.token_id || '').slice(0, 22)}{s.duration_minutes ? ` (${s.duration_minutes}m)` : ''}</td>
              <td className="small">{s.direction || '—'}</td>
              <td className="small">{s.side}</td>
              <td className="right">{s.price == null ? '—' : s.price}</td>
              <td className={`right ${s.detection_latency_s > 5 ? 'neg' : 'pos'}`}>{secs(s.detection_latency_s)}</td>
              <td className="right">{s.price_drift == null ? '—' : s.price_drift}</td>
              <td className="right">{s.seconds_until_expiry == null ? '—' : Math.round(s.seconds_until_expiry)}</td>
              <td className="small neg" title={s.ignored_reason || ''}>{(s.ignored_reason || '').slice(0, 22)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// Pure presentational panel — exported for tests.
export function OnchainPanel({ status, signals, onStart, onStop, onRunOnce, busy }) {
  const s = status
  if (!s) return <Empty>No on-chain detector status.</Empty>
  const st = s.stats || {}
  const v = VERDICT[st.verdict] || VERDICT.insufficient_data
  return (
    <div data-testid="onchain-panel">
      <div className="diag-strip" style={{ marginBottom: 10 }}>⛓ V3 Phase 1 — {s.safety}</div>

      <div className="page-head" style={{ marginBottom: 8 }}>
        <h3 style={{ margin: 0 }}>On-Chain OrderFilled Detector{' '}
          {s.enabled ? <span className="badge yes">enabled</span> : <span className="badge neutral">disabled (env)</span>}{' '}
          <span className="badge sharp" data-testid="paper-badge">paper-only · no live execution</span>
        </h3>
        <div className="toolbar" style={{ gap: 6 }}>
          <button className="secondary small" onClick={onRunOnce} disabled={busy} data-testid="onchain-runonce">Run once</button>
          {s.running
            ? <button className="danger small" onClick={onStop} disabled={busy} data-testid="onchain-stop">Stop detector</button>
            : <button className="small" onClick={onStart} disabled={busy || !s.enabled} data-testid="onchain-start">▶ Start paper detector</button>}
        </div>
      </div>

      <div className="cards">
        <div className="card"><div className="label">Detector</div><div className="value">{s.running ? '● running' : 'stopped'}</div>
          <div className="sub">RPC {s.rpc_configured ? (s.rpc_connected ? 'connected' : 'configured') : 'not set'}</div></div>
        <div className="card"><div className="label">Verdict</div><div className="value"><span className={`badge ${v.kind}`} data-testid="onchain-verdict">{v.label}</span></div>
          <div className="sub" title={st.recommendation}>{(st.recommendation || '').slice(0, 40)}</div></div>
        <div className="card"><div className="label">Signals captured</div><div className="value">{st.signals ?? 0}</div>
          <div className="sub">{st.actionable_buys ?? 0} actionable buys</div></div>
        <div className="card"><div className="label">Median detection</div><div className={`value ${st.median_latency_s > 5 ? 'neg' : 'pos'}`}>{secs(st.median_latency_s)}</div>
          <div className="sub">target &lt;{st.target_latency_s ?? 5}s</div></div>
        <div className="card"><div className="label">p90 / worst</div><div className="value">{secs(st.p90_latency_s)} / {secs(st.worst_latency_s)}</div>
          <div className="sub">best {secs(st.best_latency_s)}</div></div>
        <div className="card"><div className="label">Under 5s / 10s</div><div className="value">{pct(st.pct_under_5s)} / {pct(st.pct_under_10s)}</div></div>
        <div className="card"><div className="label">Avg price drift</div><div className="value">{st.avg_abs_drift == null ? '—' : st.avg_abs_drift}</div>
          <div className="sub">est ROI loss {st.est_roi_loss_to_latency == null ? '—' : pct(st.est_roi_loss_to_latency * 100)}</div></div>
        <div className="card"><div className="label">Token map</div><div className="value">{s.token_map_size ?? 0}</div>
          <div className="sub">last block {s.last_processed_block ?? '—'}</div></div>
      </div>

      <div className="cards" style={{ marginTop: 10 }}>
        <div className="card" style={{ flex: 1, minWidth: 280 }}>
          <div className="label">Watched wallets</div>
          {(s.watched_wallets || []).length
            ? s.watched_wallets.map((w) => <div key={w} className="small mono"><WalletLink address={w} /></div>)
            : <div className="small muted">none configured</div>}
        </div>
        <div className="card" style={{ flex: 1, minWidth: 280 }}>
          <div className="label">Exchanges subscribed</div>
          {(s.exchanges || []).map((e) => <div key={e} className="small mono">{short(e)}</div>)}
          {s.last_error && <div className="small neg" style={{ marginTop: 4 }}>err: {String(s.last_error).slice(0, 50)}</div>}
        </div>
      </div>

      <DiagnosticsPanel diagnostics={s.diagnostics} diagnosis={s.diagnosis} />

      <h4 style={{ margin: '12px 0 4px' }}>Detected signals ({signals?.signals?.length ?? 0})</h4>
      <SignalsTable rows={signals?.signals} testid="onchain-signals" empty="No watched-wallet BTC up/down BUY signals detected yet." />

      <h4 style={{ margin: '12px 0 4px' }}>Ignored signals ({signals?.ignored?.length ?? 0})</h4>
      <SignalsTable rows={signals?.ignored} testid="onchain-ignored" empty="No ignored signals." />
    </div>
  )
}

export default function Btc5mOnchain() {
  const [status, setStatus] = useState(null)
  const [signals, setSignals] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState(null)

  const load = useCallback(async () => {
    try {
      const [st, sg] = await Promise.all([
        api.btc5mOnchainStatus().then((r) => r?.detail || r),
        api.btc5mOnchainSignals(50).then((r) => r?.detail || r).catch(() => null),
      ])
      setStatus(st); setSignals(sg); setError(null)
    } catch (e) { setError(e.message) } finally { setLoading(false) }
  }, [])
  useEffect(() => { load() }, [load])
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 4500)
    return () => clearTimeout(t)
  }, [toast])

  const act = async (fn) => {
    setBusy(true)
    try {
      const r = await fn().then((x) => x?.detail || x)
      setToast(r?.error || r?.reason || (r?.ok === false ? 'refused' : 'done'))
      await load()
    } catch (e) { setToast(e.message) } finally { setBusy(false) }
  }

  if (loading) return <Loading />
  if (error) return <Empty>On-chain detector unavailable: {error}</Empty>

  return (
    <div>
      <OnchainPanel
        status={status} signals={signals} busy={busy}
        onStart={() => act(() => api.btc5mOnchainStart())}
        onStop={() => act(() => api.btc5mOnchainStop())}
        onRunOnce={() => act(() => api.btc5mOnchainRunOnce())}
      />
      {toast && <div className="toast">{toast}</div>}
    </div>
  )
}
