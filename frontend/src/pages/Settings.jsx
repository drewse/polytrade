import { useEffect, useState } from 'react'
import { api } from '../api'
import { Loading, PageHead, Toast, useData } from '../components/common.jsx'

const FIELDS = [
  { key: 'bankroll', label: 'Starting bankroll (USD)', step: 100, hint: 'Paper money to allocate.' },
  { key: 'min_wallet_score', label: 'Min wallet score', step: 1, hint: 'Only copy wallets at/above this score (0–100).' },
  { key: 'min_trade_count', label: 'Min trade count', step: 1, hint: 'Wallet must have at least this many trades.' },
  { key: 'min_trade_size', label: 'Min trade size (USD)', step: 5, hint: 'Ignore signals smaller than this.' },
  { key: 'max_position_pct', label: 'Max position %', step: 0.25, hint: 'Per-position cap as % of bankroll.' },
  { key: 'max_market_exposure_pct', label: 'Max market exposure %', step: 0.5, hint: 'Total exposure cap per market.' },
  { key: 'slippage_cents', label: 'Slippage (cents)', step: 0.5, hint: 'Simulated fill slippage on a 0–1 price.' },
  { key: 'min_market_liquidity', label: 'Min market liquidity (USD)', step: 100, hint: 'Skip illiquid markets.' },
  { key: 'max_price_staleness_min', label: 'Max price staleness (min)', step: 5, hint: 'Ignore trades older than this.' },
  { key: 'min_confidence', label: 'Min signal confidence', step: 1, hint: 'Only open positions above this confidence.' },
  { key: 'min_volume', label: 'Min market volume (USD)', step: 100, hint: 'Skip low-volume markets.' },
  { key: 'min_edge', label: 'Min estimated edge', step: 0.01, hint: 'Min (est. P(win) − price) to copy. Can be negative.' },
  { key: 'polling_interval_seconds', label: 'Polling interval (sec)', step: 5, hint: 'Worker loop cadence.' },
]

const RISK_FIELDS = [
  { key: 'max_daily_loss', label: 'Max daily loss (USD)', step: 50, hint: 'Stop opening once today’s realized loss exceeds this.' },
  { key: 'max_open_positions', label: 'Max open positions', step: 1, hint: 'Hard cap on concurrent open positions.' },
  { key: 'max_correlated_exposure_pct', label: 'Max correlated exposure %', step: 0.5, hint: 'Max open exposure per category (% of bankroll).' },
  { key: 'cooldown_losses', label: 'Cooldown after N losses', step: 1, hint: 'Consecutive losing closes that trigger a cooldown.' },
  { key: 'cooldown_minutes', label: 'Cooldown minutes', step: 5, hint: 'Pause new entries this long after the streak.' },
]

const DISCOVERY_FIELDS = [
  { key: 'discovery_interval_minutes', label: 'Discovery interval (min)', step: 1, hint: 'Min minutes between auto-discovery runs.' },
  { key: 'max_wallets_to_backfill_per_cycle', label: 'Max backfills / cycle', step: 1, hint: 'Cap on live wallet backfills per discovery run.' },
  { key: 'min_candidate_trade_count', label: 'Min candidate trades', step: 1, hint: 'Below this a wallet is “insufficient data”.' },
  { key: 'min_candidate_notional', label: 'Min candidate notional (USD)', step: 5, hint: 'Ignore dust traders below this avg notional.' },
]

export default function Settings() {
  const { data, loading, error } = useData(api.settings)
  const [form, setForm] = useState(null)
  const [toast, setToast] = useState(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => { if (data) setForm(data) }, [data])

  if (loading || !form) return <Loading />
  if (error) return <div className="empty">Error: {error}</div>

  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }))

  const save = async () => {
    setBusy(true)
    try {
      const payload = {}
      ;[...FIELDS, ...RISK_FIELDS, ...DISCOVERY_FIELDS].forEach((f) => { payload[f.key] = Number(form[f.key]) })
      payload.data_mode = form.data_mode
      payload.auto_discovery_enabled = form.auto_discovery_enabled ? 1 : 0
      const updated = await api.updateSettings(payload)
      setForm(updated)
      setToast({ msg: 'Settings saved' })
    } catch (e) {
      setToast({ msg: e.message, err: true })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <PageHead title="Settings" subtitle="Strategy & risk parameters used by the worker.">
        <button onClick={save} disabled={busy}>{busy ? 'Saving…' : 'Save changes'}</button>
      </PageHead>

      <div className="panel">
        <div className="field">
          <label>Data mode</label>
          <select value={form.data_mode} onChange={(e) => set('data_mode', e.target.value)}>
            <option value="mock">mock (offline, generated data)</option>
            <option value="live">live (Polymarket public APIs — verify endpoints first)</option>
          </select>
          <div className="hint">
            Mock runs fully offline. Live mode hits Polymarket public endpoints; the client parsing
            is best-effort and marked with TODOs — verify before trusting it.
          </div>
        </div>
      </div>

      <div className="panel">
        <h2>Strategy & sizing</h2>
        <div className="grid-2">
          {FIELDS.map((f) => (
            <div className="field" key={f.key}>
              <label>{f.label}</label>
              <input type="number" step={f.step} value={form[f.key]}
                onChange={(e) => set(f.key, e.target.value)} />
              <div className="hint">{f.hint}</div>
            </div>
          ))}
        </div>
      </div>

      <div className="panel">
        <h2>Risk controls</h2>
        <div className="grid-2">
          {RISK_FIELDS.map((f) => (
            <div className="field" key={f.key}>
              <label>{f.label}</label>
              <input type="number" step={f.step} value={form[f.key]}
                onChange={(e) => set(f.key, e.target.value)} />
              <div className="hint">{f.hint}</div>
            </div>
          ))}
        </div>
      </div>

      <div className="panel">
        <h2>Wallet discovery</h2>
        <div className="field">
          <label>Auto-discovery</label>
          <select value={form.auto_discovery_enabled ? '1' : '0'}
            onChange={(e) => set('auto_discovery_enabled', Number(e.target.value))}>
            <option value="0">Off (run manually from Discovery page)</option>
            <option value="1">On (worker discovers + backfills each cycle)</option>
          </select>
          <div className="hint">When on, the background worker discovers and ranks new wallets on the interval below.</div>
        </div>
        <div className="grid-2">
          {DISCOVERY_FIELDS.map((f) => (
            <div className="field" key={f.key}>
              <label>{f.label}</label>
              <input type="number" step={f.step} value={form[f.key]}
                onChange={(e) => set(f.key, e.target.value)} />
              <div className="hint">{f.hint}</div>
            </div>
          ))}
        </div>
      </div>

      <Toast message={toast?.msg} error={toast?.err} onDone={() => setToast(null)} />
    </div>
  )
}
