import { useCallback, useEffect, useRef, useState } from 'react'
import { api, fmt } from '../api'
import { Loading, Empty, WalletLink } from '../components/common.jsx'
import PromotionCandidates from './PromotionCandidates.jsx'
import ShadowPortfolio from './ShadowPortfolio.jsx'
import DiscoveryCandidates from './DiscoveryCandidates.jsx'

const REFRESH_MS = 10000

const short = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : '—')
const num = (n, d = 2) => (n == null ? '—' : Number(n).toFixed(d))
const pct = (n, d = 1) => (n == null ? '—' : `${(Number(n) * 100).toFixed(d)}%`)

// ---- overall live-state pill — driven by backend trading_state -----------
const STATE_MAP = {
  running: { emoji: '🟢', label: 'Running', tone: 'pos' },
  paused: { emoji: '🟡', label: 'Paused', tone: 'warn' },
  halted: { emoji: '🔴', label: 'Halted', tone: 'neg' },
  error: { emoji: '🔴', label: 'Error', tone: 'neg' },
}
function liveState(s) {
  if (!s) return { emoji: '…', label: 'Loading', tone: 'muted', detail: '' }
  const real = s.executor === 'polymarket'
  const misconfig =
    !s.wallet_check?.configuration_valid ||
    (real && (!s.auth?.py_clob_client_installed || !s.auth?.l1_private_key_present))
  if (misconfig) return { emoji: '⚠️', label: 'Misconfigured', tone: 'warn', detail: s.wallet_check?.note || 'check wallet/auth config' }
  const m = STATE_MAP[s.trading_state] || { emoji: '🟡', label: s.trading_state || 'unknown', tone: 'warn' }
  const detail = s.state?.halt_reason
    || (s.trading_state === 'running'
        ? (s.open_positions > 0 ? `${s.open_positions} open position(s)` : 'armed — copying eligible signals')
        : '')
  return { ...m, detail }
}

function Pill({ tone, children }) {
  return <span className={`live-pill ${tone}`}>{children}</span>
}

function StatusCard({ label, value, tone, sub }) {
  return (
    <div className="card">
      <div className="label">{label}</div>
      <div className={`value ${tone || ''}`}>{value}</div>
      {sub != null && <div className="sub">{sub}</div>}
    </div>
  )
}

function YesNo({ ok, yes = 'Yes', no = 'No' }) {
  return <span className={ok ? 'pos' : 'neg'}>{ok ? `✓ ${yes}` : `✗ ${no}`}</span>
}

// ---- executions table ----------------------------------------------------
const EXEC_TONE = { open: 'open', closed: 'closed', rejected: 'bad' }
function execBadge(e) {
  // filled = green, open/submitted = blue, rejected = red
  if (e.status === 'closed' || (e.status === 'open' && e.fill_price)) {
    if (e.status === 'closed') return <span className="badge closed">closed</span>
    return <span className="badge yes">filled</span>
  }
  if (e.status === 'open') return <span className="badge open">open</span>
  if (e.status === 'rejected') return <span className="badge bad">rejected</span>
  return <span className="badge neutral">{e.status}</span>
}

// dynamic risk-aware sizing breakdown — every position size is explainable
export function SizingCell({ sizing }) {
  if (!sizing) return <span className="muted small">—</span>
  const c = sizing.constraints || {}
  const tip = [
    `method: ${sizing.method}`,
    sizing.market_price != null ? `price: ${sizing.market_price}` : null,
    sizing.confidence != null ? `confidence: ${sizing.confidence} → ×${sizing.confidence_multiplier}` : null,
    sizing.edge != null ? `edge: ${sizing.edge} → ×${sizing.edge_factor}` : null,
    sizing.raw_target_stake != null ? `raw target: $${sizing.raw_target_stake}` : null,
    sizing.share_cap != null ? `share cap: $${sizing.share_cap} (${sizing.max_shares_per_trade} sh)` : null,
    Object.keys(c).length ? `caps: ${Object.entries(c).map(([k, v]) => `${k}=${v}`).join(', ')}` : null,
    `→ final: $${sizing.final_stake} (${sizing.final_shares ?? '—'} sh) · limited by ${sizing.limiting_constraint}`,
  ].filter(Boolean).join('\n')
  return (
    <span className="small" title={tip} style={{ cursor: 'help' }}>
      <span className="badge neutral">{(sizing.limiting_constraint || '—').replace(/_/g, ' ')}</span>
    </span>
  )
}

// ---- gate trail (the key readability feature) ----------------------------
const GATE_LABEL = {
  trading_enabled: 'trading enabled',
  wallet_eligible: 'wallet eligible',
  edge_ok: 'edge',
  confidence_ok: 'confidence',
  market_open: 'market open',
  fresh: 'fresh',
  duplicate_check: 'not duplicate',
  risk_passed: 'risk',
  submitted: 'submitted',
  filled: 'filled',
}
function GateTrail({ gates, reason, status }) {
  const entries = Object.entries(gates || {})
  if (!entries.length) return <span className="muted small">{reason || '—'}</span>
  const lines = []
  for (const [k, v] of entries) {
    if (v) {
      lines.push(<div key={k} className="gate ok">✓ {GATE_LABEL[k] || k}</div>)
    } else {
      // the failing gate carries the human reason (e.g. "slippage 7.1% > 3%")
      lines.push(<div key={k} className="gate bad">✗ {reason || GATE_LABEL[k] || k}</div>)
      break
    }
  }
  if (status === 'filled') lines.push(<div key="done" className="gate ok">★ order placed</div>)
  return <div className="gate-trail">{lines}</div>
}

const DEC_TONE = { filled: 'yes', skipped: 'neutral', rejected: 'bad', expired: 'neutral', eligible: 'open' }

// ===========================================================================
export default function LiveTrading() {
  const [data, setData] = useState({ status: null, execs: [], decisions: [], ranking: null })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState('')
  const [toast, setToast] = useState(null)
  const [diag, setDiag] = useState(null)
  const [balance, setBalance] = useState('')
  const [account, setAccount] = useState(null)        // reconciled venue+local account snapshot
  const [reconciling, setReconciling] = useState(false)
  const [tab, setTab] = useState('dashboard')   // dashboard | promotion
  const timer = useRef(null)

  const load = useCallback(async () => {
    try {
      const [status, execRes, decRes, ranking] = await Promise.all([
        api.liveStatus(),
        api.liveExecutions(50).catch(() => ({ executions: [] })),
        api.liveDecisions(100).catch(() => ({ detail: { decisions: [] } })),
        api.liveRanking(20).catch(() => null),
      ])
      setData({
        status,
        execs: execRes?.executions || [],
        decisions: decRes?.detail?.decisions || [],
        ranking,
      })
      setError(null)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
    timer.current = setInterval(load, REFRESH_MS)
    return () => clearInterval(timer.current)
  }, [load])

  const flash = (message, isErr) => setToast({ message, isErr })
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 4000)
    return () => clearTimeout(t)
  }, [toast])

  const act = async (key, fn, confirmMsg) => {
    if (confirmMsg && !window.confirm(confirmMsg)) return
    setBusy(key)
    try {
      const res = await fn()
      flash(typeof res?.message === 'string' ? res.message : 'done')
      await load()
      return res
    } catch (e) {
      flash(e.message, true)
    } finally {
      setBusy('')
    }
  }

  const onRunOnce = async () => {
    const res = await act('runonce', api.liveRunOnce)
    if (res?.detail) setDiag(res.detail)
  }
  const onReconcile = async () => {
    const bal = parseFloat(balance)
    if (Number.isNaN(bal)) return flash('Enter the venue balance to reconcile against', true)
    await act('reconcile', () => api.liveReconcile(bal), `Reconcile bankroll against reported balance $${bal}?`)
  }
  const onReconcileFills = async () => {
    const res = await act('reconcilefills', () => api.liveReconcileFills(300),
      'Reconcile recorded fills against the venue’s actual executions? (accounting only — no orders)')
    if (res?.detail) flash(`Fills reconciled: ${res.detail.corrected} corrected, ${res.detail.marked_pending || 0} pending.`)
  }

  // Account reconciliation runs SEPARATELY from the 10s status poll (it hits the
  // venue, so it's slower) — async, never blocking the dashboard UI.
  const reconcileAccount = useCallback(async () => {
    setReconciling(true)
    try {
      const res = await api.liveReconcileAccount()
      if (res?.detail) setAccount(res.detail)
      flash('Account reconciled')
      load()                                  // refresh counts after settling ended markets
    } catch (e) {
      flash(e.message, true)
    } finally {
      setReconciling(false)
    }
  }, [load])

  useEffect(() => { reconcileAccount() }, [reconcileAccount])  // one async fetch on mount

  const rebaselineBankroll = async () => {
    const r = account?.rebaseline_recommendation
    const warn = r
      ? `Re-baseline local bankroll to venue reality?\n\n  bankroll: ${fmt.usd2(r.old_bankroll)} → ${fmt.usd2(r.proposed_bankroll)}\n  starting: ${fmt.usd2(r.old_starting_bankroll)} → ${fmt.usd2(r.proposed_starting_bankroll)}\n\nAccounting only — realized P/L, open positions, executions and history are preserved. No orders are placed.`
      : 'Re-baseline local bankroll to venue reality? (accounting only — no orders, history preserved)'
    if (!window.confirm(warn)) return
    setReconciling(true)
    try {
      const res = await api.liveRebaselineBankroll()
      const d = res?.detail || {}
      if (d.ok === false) { flash(d.error || 'rebaseline refused', true) }
      else { flash(`Bankroll re-baselined: ${fmt.usd2(d.old_bankroll)} → ${fmt.usd2(d.new_bankroll)} (realized P/L preserved ${fmt.usd2(d.realized_pnl_preserved)})`) }
      await reconcileAccount()                 // refresh account + status after
    } catch (e) { flash(e.message, true) } finally { setReconciling(false) }
  }

  if (loading) return <Loading />
  const s = data.status
  if (error && !s) return <Empty>Live status unavailable: {error}</Empty>

  const ls = liveState(s)
  const lim = s?.limits_usd || {}
  const stopped = s?.trading_state !== 'running'   // halted | paused | error

  return (
    <div className="live-page">
      {/* ---- safety banner ---- */}
      <div className="live-banner">
        <div className="live-banner-title">⚠ LIVE REAL-MONEY EXECUTION</div>
        <div className="live-banner-row">
          <span>Position size: <b>{fmt.usd2(s?.sizing?.position_usd)}</b></span>
          <span>Max total risk: <b>{fmt.usd2(lim.max_total_risk)}</b></span>
          <span>Max possible loss: <b>{fmt.usd2(s?.max_possible_loss)}</b></span>
        </div>
      </div>

      <div className="page-head">
        <div>
          <h1>Live Trading <Pill tone={ls.tone}>{ls.emoji} {ls.label}</Pill></h1>
          <p>{ls.detail} · executor <b>{s?.executor}</b> · strategy <b>{s?.strategy_copied}</b>
            {s?.execution && <> · orders <b>{s.execution.order_mode}</b> (TTL {s.execution.order_ttl_seconds}s, cancel-if-unfilled)</>}
            · auto-refresh 10s
            {error && <span className="neg"> · refresh error: {error}</span>}</p>
        </div>
        <div className="toolbar live-controls">
          <button className="secondary" onClick={load} disabled={busy === 'refresh'}>↻ Refresh</button>
          <button onClick={() => act('resume', api.liveResume, 'Resume live trading — new orders may be placed. Continue?')}
            disabled={busy === 'resume' || stopped === false}>▶ Resume Trading</button>
          <button className="danger" onClick={() => act('pause', api.livePause, 'Pause live trading. No new orders until you resume. Continue?')}
            disabled={busy === 'pause' || stopped === true}>⏸ Pause / Halt Trading</button>
          <button className="secondary" onClick={onRunOnce} disabled={busy === 'runonce'}>
            {busy === 'runonce' ? 'Running…' : 'Run once (diagnostic)'}
          </button>
          <button onClick={reconcileAccount} disabled={reconciling}>
            {reconciling ? 'Reconciling…' : '⟳ Reconcile Account'}
          </button>
          <button className="secondary" onClick={onReconcileFills} disabled={busy === 'reconcilefills'}>
            {busy === 'reconcilefills' ? 'Reconciling…' : '⟳ Reconcile Fills'}
            {s?.fills_pending_reconciliation > 0 && <span className="badge bad" style={{ marginLeft: 4 }}>{s.fills_pending_reconciliation}</span>}
          </button>
          <div className="reconcile-box">
            <input type="number" step="0.01" placeholder="venue $balance" value={balance}
              onChange={(e) => setBalance(e.target.value)} style={{ width: 120 }} />
            <button className="secondary" onClick={onReconcile} disabled={busy === 'reconcile'}>Manual reconcile</button>
          </div>
        </div>
      </div>

      {/* ---- tabs ---- */}
      <div className="live-tabs">
        <button className={`tab ${tab === 'dashboard' ? 'active' : ''}`} onClick={() => setTab('dashboard')}>Dashboard</button>
        <button className={`tab ${tab === 'promotion' ? 'active' : ''}`} onClick={() => setTab('promotion')}>Promotion Candidates</button>
        <button className={`tab ${tab === 'shadow' ? 'active' : ''}`} onClick={() => setTab('shadow')}>Shadow Portfolio</button>
        <button className={`tab ${tab === 'discovery' ? 'active' : ''}`} onClick={() => setTab('discovery')}>Discovery Candidates</button>
      </div>

      {tab === 'promotion' && <PromotionCandidates />}
      {tab === 'shadow' && <ShadowPortfolio />}
      {tab === 'discovery' && <DiscoveryCandidates />}

      {tab === 'dashboard' && <>
      {/* ---- trading control state ---- */}
      <div className="panel live-control-panel">
        <div className="live-control-state">
          <span className={`live-pill ${ls.tone}`} style={{ fontSize: 14, marginLeft: 0 }}>{ls.emoji} {String(s?.trading_state || '—').toUpperCase()}</span>
          <span className="muted small">{ls.detail}</span>
        </div>
        <div className="live-control-metrics">
          <div><span>Open positions</span><b>{s?.open_positions ?? 0}{s?.ended_unsettled ? <span className="muted small"> (+{s.ended_unsettled} ended, unsettled)</span> : null}</b></div>
          <div><span>Open exposure</span><b>{fmt.usd2(s?.open_exposure)}</b></div>
          <div><span>Real orders placed</span><b title="lifetime count — informational only, not a cap">{s?.real_orders_placed ?? 0}</b></div>
          <div><span>Latest venue error</span><b className={s?.latest_venue_error ? 'neg' : 'pos'} title={s?.latest_venue_error || ''}>
            {s?.latest_venue_error ? String(s.latest_venue_error).slice(0, 60) + '…' : 'none'}</b></div>
        </div>
      </div>

      {/* ---- account reconciliation (venue vs local, async) ---- */}
      <div className="panel">
        <div className="page-head" style={{ marginBottom: 8 }}>
          <h2 style={{ margin: 0 }}>Account {reconciling && <span className="muted small">· reconciling…</span>}</h2>
          <div className="toolbar">
            <button onClick={reconcileAccount} disabled={reconciling}>
              {reconciling ? 'Reconciling…' : '⟳ Reconcile Account'}
            </button>
            <button className="danger" onClick={rebaselineBankroll} disabled={reconciling}
              title="Align local bankroll baseline with venue reality (accounting only)">
              ⚖ Re-baseline Local Bankroll
              {account?.rebaseline_recommendation && <span className="badge bad" style={{ marginLeft: 4 }}>drift</span>}
            </button>
          </div>
        </div>
        {account?.rebaseline_recommendation && (
          <div className="diag-strip neg" style={{ marginBottom: 8 }}>
            ⚖ Baseline drift detected: bankroll {fmt.usd2(account.rebaseline_recommendation.old_bankroll)} →
            <b> {fmt.usd2(account.rebaseline_recommendation.proposed_bankroll)}</b> · starting
            {' '}{fmt.usd2(account.rebaseline_recommendation.old_starting_bankroll)} →
            <b> {fmt.usd2(account.rebaseline_recommendation.proposed_starting_bankroll)}</b>
            {' '}(venue cash {fmt.usd2(account.rebaseline_recommendation.venue_cash)} + open exposure
            {' '}{fmt.usd2(account.rebaseline_recommendation.open_exposure)}; realized P/L
            {' '}{fmt.usd2(account.rebaseline_recommendation.realized_pnl)} preserved).
          </div>
        )}
        <div className="cards">
          <StatusCard label="Venue cash (live)" value={account?.venue_cash == null ? '—' : fmt.usd2(account.venue_cash)}
            sub={account?.venue_balance_error ? 'unavailable' : (account?.venue_balance_source || 'on-venue USDC')} tone="pos" />
          <StatusCard label="Local bankroll" value={fmt.usd2(account?.local_bankroll ?? s?.state?.bankroll)} sub="accounting (start + realized)" />
          <StatusCard label="Open exposure" value={fmt.usd2(account?.open_exposure ?? s?.open_exposure)} sub={`${account?.open_positions ?? s?.open_positions ?? 0} open`} />
          <StatusCard label="Realized P/L" value={fmt.usd2(account?.realized_pnl ?? s?.total_realized)}
            tone={(account?.realized_pnl ?? s?.total_realized) > 0 ? 'pos' : (account?.realized_pnl ?? s?.total_realized) < 0 ? 'neg' : ''} />
          <StatusCard label="Unrealized P/L" value={account?.unrealized_pnl == null ? '—' : fmt.usd2(account.unrealized_pnl)}
            tone={account?.unrealized_pnl > 0 ? 'pos' : account?.unrealized_pnl < 0 ? 'neg' : ''} sub="open mark-to-market" />
          <StatusCard label="Drift (venue − expected)" value={account?.drift == null ? '—' : fmt.usd2(account.drift)}
            tone={account?.reconciled ? 'pos' : account?.drift == null ? '' : 'warn'}
            sub={account?.reconciled == null ? 'reconcile to check' : account.reconciled ? '✓ reconciled' : 'review drift'} />
        </div>
        {account && (
          <p className="muted small" style={{ marginTop: 6 }}>
            Last reconciled {fmt.ago(account.reconciled_at)} · settled {account.positions_settled ?? 0} ended position(s)
            {account.markets_newly_resolved ? `, resolved ${account.markets_newly_resolved} market(s)` : ''}
            {account.venue_balance_error ? ` · venue balance: ${account.venue_balance_error}` : ''}.
          </p>
        )}
      </div>

      {/* ---- 1. status cards ---- */}
      <div className="cards">
        <StatusCard label="Status" value={<span className={ls.tone}>{ls.emoji} {ls.label}</span>} sub={ls.detail} />
        <StatusCard label="Executor" value={s?.executor} sub={s?.live_trading_enabled ? 'live enabled' : 'disabled'} />
        <StatusCard label="Bankroll" value={fmt.usd2(s?.state?.bankroll)} sub={`start ${fmt.usd2(s?.state?.starting_bankroll)}`} />
        <StatusCard label="Open exposure" value={fmt.usd2(s?.open_exposure)} />
        <StatusCard label="Open positions" value={s?.open_positions ?? 0} sub={s?.ended_unsettled ? `${s.ended_unsettled} ended, unsettled` : 'active markets'} />
        <StatusCard label="Day P/L" value={fmt.usd2(s?.day_pnl)} tone={s?.day_pnl > 0 ? 'pos' : s?.day_pnl < 0 ? 'neg' : ''} />
        <StatusCard label="Total realized P/L" value={fmt.usd2(s?.total_realized)} tone={s?.total_realized > 0 ? 'pos' : s?.total_realized < 0 ? 'neg' : ''} />
        <StatusCard label="Position size (base)" value={fmt.usd2(s?.sizing?.position_usd)}
          sub={s?.sizing?.dynamic_enabled ? `dynamic · ${s?.sizing?.max_shares_per_trade} sh cap` : s?.sizing?.method} />
        <StatusCard label="Max total risk" value={fmt.usd2(lim.max_total_risk)} sub="open-exposure cap ($)" />
        <StatusCard label="Real orders placed" value={s?.real_orders_placed ?? 0} sub="lifetime — not a cap" />
        <StatusCard label="Max possible loss" value={fmt.usd2(s?.max_possible_loss)} tone="neg" />
        <StatusCard label="Wallet config" value={<YesNo ok={s?.wallet_check?.configuration_valid} yes="Valid" no="Invalid" />} sub={s?.wallet_check?.addresses_match ? 'addresses match' : 'proxy/mismatch'} />
        <StatusCard label="py-clob-client" value={<YesNo ok={s?.auth?.py_clob_client_installed} yes="Installed" no="Missing" />} sub={`sig type ${s?.auth?.signature_type}`} />
      </div>

      {/* ---- 2. risk controls ---- */}
      <div className="panel">
        <h2>Risk controls (absolute-dollar limits)</h2>
        <div className="risk-grid">
          {[
            ['Max position', fmt.usd2(lim.max_position)],
            ['Max total risk', fmt.usd2(lim.max_total_risk)],
            ['Max per market', fmt.usd2(lim.max_per_market)],
            ['Max per wallet', fmt.usd2(lim.max_per_wallet)],
            ['Daily loss stop', fmt.usd2(lim.daily_loss_stop)],
            ['Total loss stop', fmt.usd2(lim.total_loss_stop)],
            ['Max slippage', pct(s?.max_slippage_pct, 0)],
          ].map(([k, v]) => (
            <div key={k} className="risk-cell"><span>{k}</span><b>{v}</b></div>
          ))}
        </div>
      </div>

      {/* ---- 4. decision / audit feed (most important) ---- */}
      <div className="panel">
        <h2>Decision feed — every signal, every reason ({data.decisions.length})</h2>
        {diag && (
          <div className="diag-strip">
            <b>Last run-once diagnostic:</b> seen {diag.signals_seen} · new {diag.new_evaluated} · eligible {diag.eligible} ·
            placed {diag.placed} · executor_called {String(diag.executor_called)} — {diag.reason}
          </div>
        )}
        {!data.decisions.length ? (
          <Empty>No decisions recorded yet. The worker logs one per evaluated signal.</Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Time</th><th>Signal</th><th>Status</th><th>Category</th><th>Market</th>
                  <th>Wallet</th><th className="right">Edge</th><th className="right">Conf</th>
                  <th className="right">Prod</th><th>Reason</th><th>Gate trail</th>
                </tr>
              </thead>
              <tbody>
                {data.decisions.map((d) => (
                  <tr key={d.id}>
                    <td className="muted small">{fmt.ago(d.created_at)}</td>
                    <td className="mono">{d.signal_id}</td>
                    <td><span className={`badge ${DEC_TONE[d.status] || 'neutral'}`}>{d.status}</span></td>
                    <td className="small">{d.category}</td>
                    <td className="small" title={d.market || ''}>{(d.market || d.market_id || '—').slice(0, 40)}</td>
                    <td className="mono"><WalletLink address={d.wallet} /></td>
                    <td className="right">{num(d.edge, 3)}</td>
                    <td className="right">{num(d.confidence, 0)}</td>
                    <td className="right">{num(d.production_score, 1)}</td>
                    <td className="small">{d.reason}</td>
                    <td><GateTrail gates={d.gates} reason={d.reason} status={d.status} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ---- 3. recent executions ---- */}
      <div className="panel">
        <h2>Recent live executions ({data.execs.length})</h2>
        {!data.execs.length ? (
          <Empty>No executions yet (real orders only appear once a signal fully qualifies and fills).</Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Time</th><th>Status</th><th>Market</th><th>Outcome</th><th>Side</th>
                  <th className="right">Stake</th><th className="right">Exp</th><th className="right">Limit</th>
                  <th className="right">Fill</th><th>Src</th><th className="right">Shares</th><th className="right">Slip</th>
                  <th>Sizing</th><th>Order id</th><th>Wallet copied</th><th>Reason / error</th>
                </tr>
              </thead>
              <tbody>
                {data.execs.map((e) => (
                  <tr key={e.id}>
                    <td className="muted small">{fmt.ago(e.created_at)}</td>
                    <td>{execBadge(e)}</td>
                    <td className="small" title={e.market_question || ''}>{(e.market_question || e.market_id || '—').slice(0, 36)}</td>
                    <td className="small">{e.outcome}</td>
                    <td className="small">{e.side}</td>
                    <td className="right">{fmt.usd2(e.size_usd)}</td>
                    <td className="right">{num(e.expected_price, 3)}</td>
                    <td className="right">{num(e.limit_price, 3)}</td>
                    <td className="right">{num(e.fill_price, 3)}</td>
                    <td title={e.reconciled_at ? `reconciled ${fmt.ago(e.reconciled_at)}` : ''}>
                      {e.fill_pending_reconciliation
                        ? <span className="badge open" title="awaiting venue fill reconciliation">⏳ pending</span>
                        : e.fill_source ? <span className={`badge ${e.fill_source === 'venue' ? 'yes' : 'neutral'}`}>{e.fill_source}</span> : '—'}</td>
                    <td className="right">{num(e.shares, 2)}</td>
                    <td className="right">{e.slippage == null ? '—' : pct(e.slippage)}</td>
                    <td><SizingCell sizing={e.sizing_detail} /></td>
                    <td className="mono small" title={e.order_id || ''}>{e.order_id ? short(e.order_id) : '—'}</td>
                    <td className="mono"><WalletLink address={e.wallet} /></td>
                    <td className="small" title={e.venue_error || ''}>
                      {e.fill_outcome && <b>{e.fill_outcome}</b>}{e.fill_outcome ? ' — ' : ''}
                      {e.venue_error || e.exit_reason || e.entry_reason || '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ---- 5. top eligible wallets ---- */}
      <div className="panel">
        <h2>Top eligible wallets (production ranking) — {data.ranking?.eligible_count ?? 0} eligible</h2>
        {!data.ranking?.top?.length ? (
          <Empty>No eligible wallets currently pass the production profitability filters.</Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>#</th><th>Wallet</th><th className="right">Prod score</th><th className="right">Copyability</th>
                  <th className="right">Reputation</th><th className="right">PF</th><th className="right">ROI</th>
                  <th className="right">Settled</th>
                </tr>
              </thead>
              <tbody>
                {data.ranking.top.map((w, i) => (
                  <tr key={w.address}>
                    <td>{i + 1}</td>
                    <td className="mono"><WalletLink address={w.address} /></td>
                    <td className="right"><b>{num(w.production_rank_score, 1)}</b></td>
                    <td className="right">{num(w.copyability, 1)}</td>
                    <td className="right">{w.reputation_score == null ? '—' : num(w.reputation_score, 1)}</td>
                    <td className="right">{num(w.profit_factor, 2)}</td>
                    <td className={`right ${w.roi > 0 ? 'pos' : 'neg'}`}>{pct(w.roi)}</td>
                    <td className="right">{w.num_settled}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <p className="muted small" style={{ marginTop: 8 }}>
        Read-only monitor. No control here can enable live trading; no private keys or secrets are exposed.
      </p>
      </>}

      {toast && <div className={`toast ${toast.isErr ? 'err' : ''}`}>{toast.message}</div>}
    </div>
  )
}
