import { useEffect, useState } from 'react'
import { COPY_CLASS, api, fmt } from '../api'
import { Badge, Loading, MultiLineChart, PageHead, PnL, ScoreBar, Toast } from '../components/common.jsx'

const CLASS_ORDER = ['elite_candidate', 'good_candidate', 'watchlist', 'ignore', 'insufficient_data']

function ClassBadge({ c }) {
  const m = COPY_CLASS[c] || { label: c, kind: 'neutral' }
  return <Badge kind={m.kind}>{m.label}</Badge>
}

function CandidateModal({ address, onClose, onChanged, setToast }) {
  const [d, setD] = useState(null)
  useEffect(() => { api.candidate(address).then(setD).catch(() => setD(false)) }, [address])
  if (d === false) return null
  const act = async (fn, label) => {
    try { const r = await fn(address); setToast({ msg: r.message }); onChanged() } catch (e) { setToast({ msg: e.message, err: true }) }
  }
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        {!d ? <Loading /> : (
          <>
            <div className="page-head">
              <div>
                <h1 style={{ fontSize: 18 }}>{d.label || 'Candidate'} <ClassBadge c={d.classification} /></h1>
                <p className="mono">{d.address}</p>
              </div>
              <div className="toolbar">
                <button className="sm" onClick={() => act(api.trackCandidate)}>Track</button>
                <button className="sm secondary" onClick={() => act(api.ignoreCandidate)}>Ignore</button>
                <button className="sm secondary" onClick={onClose}>Close</button>
              </div>
            </div>

            {d.weak_sample && (
              <div className="warn-box">⚠ Weak sample — too few settled trades to trust this edge. Treat with caution.</div>
            )}
            {d.suspected_noise && (
              <div className="warn-box">⚠ Suspected spoof/noise wallet — flagged by the copyability engine.</div>
            )}

            <div className="cards">
              <div className="card"><div className="label">Copyability</div><div className="value">{d.copyability_score.toFixed(0)}</div></div>
              <div className="card"><div className="label">ROI</div><div className="value"><PnL value={d.realized_roi * 100} fmtFn={fmt.pct} /></div></div>
              <div className="card"><div className="label">Win rate</div><div className="value">{fmt.pct(d.win_rate * 100)}</div></div>
              <div className="card"><div className="label">Trades</div><div className="value">{d.num_trades}</div><div className="sub">{d.distinct_markets} markets</div></div>
              <div className="card"><div className="label">Avg notional</div><div className="value">{fmt.usd(d.avg_trade_size)}</div></div>
              <div className="card"><div className="label">Copied paper PnL</div><div className="value"><PnL value={d.copied_paper_pnl} fmtFn={fmt.usd} /></div><div className="sub">{d.copied_positions} positions</div></div>
            </div>

            <div className="panel">
              <h2>Reasons</h2>
              <ul className="reasons">{d.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
            </div>

            <div className="grid-2">
              <div className="panel">
                <h2>Best categories</h2>
                {d.best_categories.length === 0 ? <span className="muted">—</span> :
                  d.best_categories.map((c) => (
                    <div key={c.category} className="catrow"><span>{c.category}</span><PnL value={c.roi * 100} fmtFn={fmt.pct} /></div>
                  ))}
                <h2 style={{ marginTop: 16 }}>Worst categories</h2>
                {d.worst_categories.length === 0 ? <span className="muted">—</span> :
                  d.worst_categories.map((c) => (
                    <div key={c.category} className="catrow"><span>{c.category}</span><PnL value={c.roi * 100} fmtFn={fmt.pct} /></div>
                  ))}
              </div>
              <div className="panel">
                <h2>Profit curve (cumulative realized PnL)</h2>
                {d.profit_curve.length < 2 ? <span className="muted">Not enough settled trades.</span> :
                  <MultiLineChart height={160} series={[{ name: 'cum PnL', color: '#36c275', curve: d.profit_curve.map((p) => ({ t: p.t, value: p.pnl })) }]} />}
              </div>
            </div>

            <div className="panel">
              <h2>Recent trades</h2>
              <div className="table-wrap">
                <table>
                  <thead><tr><th>When</th><th>Market</th><th>Outcome</th><th className="right">Price</th><th className="right">Size</th><th className="right">PnL</th></tr></thead>
                  <tbody>
                    {d.recent_trades.map((t, i) => (
                      <tr key={i}>
                        <td className="muted">{fmt.ago(t.timestamp)}</td>
                        <td className="mono">{t.market_id.slice(0, 12)}</td>
                        <td>{t.outcome}</td>
                        <td className="right">{fmt.price(t.price)}</td>
                        <td className="right">{fmt.usd(t.size)}</td>
                        <td className="right">{t.realized_pnl ? <PnL value={t.realized_pnl} fmtFn={fmt.usd2} /> : <span className="muted">open</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

export default function Discovery() {
  const [cands, setCands] = useState(null)
  const [cls, setCls] = useState('')
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState(null)
  const [selected, setSelected] = useState(null)

  const load = () => api.candidates(cls || undefined).then(setCands)
  useEffect(() => { load() }, [cls]) // eslint-disable-line

  const run = async () => {
    setBusy(true)
    try {
      const r = await api.runDiscovery()
      const by = r.detail.by_classification
      setToast({ msg: `Discovery: ${by.elite_candidate} elite, ${by.good_candidate} good, ${by.watchlist} watchlist, ${by.ignore + by.insufficient_data} ignored` })
      load()
    } catch (e) { setToast({ msg: e.message, err: true }) } finally { setBusy(false) }
  }

  const act = async (fn, address) => {
    try { const r = await fn(address); setToast({ msg: r.message }); load() } catch (e) { setToast({ msg: e.message, err: true }) }
  }
  const backfill = async (address) => {
    try { const r = await api.backfillWallet(address, 300); setToast({ msg: `Backfilled ${r.detail.trades_inserted} trades` }); run() }
    catch (e) { setToast({ msg: e.message, err: true }) }
  }

  if (!cands) return <Loading />

  return (
    <div>
      <PageHead title="Discovery" subtitle="Automatically surface & rank the best wallets to copy.">
        <select style={{ width: 170 }} value={cls} onChange={(e) => setCls(e.target.value)}>
          <option value="">All classes</option>
          {CLASS_ORDER.map((c) => <option key={c} value={c}>{COPY_CLASS[c].label}</option>)}
        </select>
        <button onClick={run} disabled={busy}>{busy ? 'Scanning…' : 'Run discovery'}</button>
      </PageHead>

      <div className="panel">
        <p className="muted" style={{ marginTop: 0 }}>
          Copyability is scored separately from raw profitability — it rewards consistent, diversified,
          recently-active wallets and penalizes tiny samples, too-good-to-be-true win rates, and
          micro-notional spoof/noise wallets. Click a row for detail.
        </p>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Wallet</th><th>Copyability</th><th>Class</th><th className="right">ROI</th>
                <th className="right">Win%</th><th className="right">Trades</th><th className="right">Markets</th>
                <th className="right">Avg notional</th><th>Last active</th><th>State</th><th>Reasons</th><th></th>
              </tr>
            </thead>
            <tbody>
              {cands.length === 0 && <tr><td colSpan="12" className="muted">No candidates — run discovery.</td></tr>}
              {cands.map((w) => (
                <tr key={w.wallet_id} style={{ cursor: 'pointer' }} onClick={() => setSelected(w.address)}>
                  <td>{w.label || <span className="mono">{w.address.slice(0, 12)}…</span>}</td>
                  <td><ScoreBar score={w.copyability_score} /></td>
                  <td>
                    <ClassBadge c={w.classification} />
                    {w.partial_history && <span className="src-badge partial" style={{ marginLeft: 6, padding: '2px 6px' }}>partial</span>}
                  </td>
                  <td className="right"><PnL value={w.realized_roi * 100} fmtFn={fmt.pct} /></td>
                  <td className="right">{fmt.pct(w.win_rate * 100)}</td>
                  <td className="right">{w.num_trades}</td>
                  <td className="right">{w.distinct_markets}</td>
                  <td className="right">{fmt.usd(w.avg_trade_size)}</td>
                  <td className="muted">{fmt.ago(w.last_active)}</td>
                  <td>{w.state === 'tracked' ? <Badge kind="sharp">tracked</Badge> : w.state === 'ignored' ? <Badge kind="bad">ignored</Badge> : <span className="muted">new</span>}</td>
                  <td className="muted" style={{ maxWidth: 220, fontSize: 11 }}>{(w.reasons || []).slice(0, 2).join('; ')}</td>
                  <td onClick={(e) => e.stopPropagation()}>
                    <div className="toolbar">
                      <button className="sm" onClick={() => act(api.trackCandidate, w.address)}>Track</button>
                      <button className="sm secondary" onClick={() => act(api.ignoreCandidate, w.address)}>Ignore</button>
                      <button className="sm secondary" onClick={() => backfill(w.address)} title="Live only">Backfill</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {selected && (
        <CandidateModal address={selected} onClose={() => setSelected(null)}
          onChanged={load} setToast={setToast} />
      )}
      <Toast message={toast?.msg} error={toast?.err} onDone={() => setToast(null)} />
    </div>
  )
}
