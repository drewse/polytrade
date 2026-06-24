import { api, fmt } from '../api'
import { Badge, Loading, PageHead, PnL, useData } from '../components/common.jsx'

// signal-quality moves are signed price changes (0..1). Show in cents.
const cents = (v) => (v == null ? '—' : `${v >= 0 ? '+' : ''}${(v * 100).toFixed(1)}¢`)
function Move({ v }) {
  if (v == null) return <span className="muted">—</span>
  return <span className={v > 0 ? 'pos' : v < 0 ? 'neg' : 'muted'}>{cents(v)}</span>
}

export default function Signals() {
  const { data, loading, error } = useData(api.signalQuality)
  if (loading) return <Loading />
  if (error) return <div className="empty">Error: {error}</div>

  return (
    <div>
      <PageHead
        title="Signals"
        subtitle="Copy-trade signals + how the market actually moved afterward (quality)."
      />
      <div className="panel">
        <p className="muted" style={{ marginTop: 0 }}>
          Move columns show the signed price change in the predicted direction at each horizon.
          Positive = the market moved our way. MFE/MAE are the best/worst excursions seen.
          Right after seeding these are synthesized; the worker refines them from live price
          snapshots as time passes.
        </p>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>When</th><th>Wallet</th><th>Market</th><th>Out</th>
                <th className="right">Price</th><th className="right">Edge</th>
                <th className="right">Conf</th><th>Copied</th>
                <th className="right">+5m</th><th className="right">+30m</th>
                <th className="right">+2h</th><th className="right">close</th>
                <th className="right">MFE</th><th className="right">MAE</th>
              </tr>
            </thead>
            <tbody>
              {data.length === 0 && (
                <tr><td colSpan="14" className="muted">No signals yet — run an ingest cycle.</td></tr>
              )}
              {data.map((s) => (
                <tr key={s.id}>
                  <td className="muted">{fmt.ago(s.created_at)}</td>
                  <td className="mono">{s.wallet_address?.slice(0, 10)}…</td>
                  <td style={{ maxWidth: 220 }}>{s.market_question || s.market_id}</td>
                  <td><Badge kind={s.outcome === 'Yes' ? 'yes' : 'no'}>{s.outcome}</Badge></td>
                  <td className="right">{fmt.price(s.observed_price)}</td>
                  <td className="right"><PnL value={s.edge_estimate * 100} fmtFn={(n) => `${n.toFixed(0)}¢`} /></td>
                  <td className="right">{s.confidence.toFixed(0)}</td>
                  <td>{s.copied ? <Badge kind="sharp">yes</Badge> : <Badge kind="neutral">no</Badge>}</td>
                  <td className="right"><Move v={s.move_5m} /></td>
                  <td className="right"><Move v={s.move_30m} /></td>
                  <td className="right"><Move v={s.move_2h} /></td>
                  <td className="right"><Move v={s.move_close} /></td>
                  <td className="right pos">{cents(s.mfe)}</td>
                  <td className="right neg">{cents(s.mae)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
