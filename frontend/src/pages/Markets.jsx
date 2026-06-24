import { useMemo, useState } from 'react'
import { api, fmt } from '../api'
import { Badge, Loading, PageHead, useData } from '../components/common.jsx'

export default function Markets() {
  const { data, loading, error } = useData(api.markets)
  const [cat, setCat] = useState('')

  const categories = useMemo(
    () => [...new Set((data || []).map((m) => m.category).filter(Boolean))].sort(),
    [data],
  )
  const rows = useMemo(
    () => (data || []).filter((m) => !cat || m.category === cat),
    [data, cat],
  )

  if (loading) return <Loading />
  if (error) return <div className="empty">Error: {error}</div>

  return (
    <div>
      <PageHead title="Markets" subtitle="Tracked Polymarket markets, prices, liquidity & volume.">
        <select style={{ width: 160 }} value={cat} onChange={(e) => setCat(e.target.value)}>
          <option value="">All categories</option>
          {categories.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
      </PageHead>

      <div className="panel">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Market</th><th>Category</th><th>Prices</th>
                <th className="right">Liquidity</th><th className="right">Volume</th><th>Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((m) => (
                <tr key={m.id}>
                  <td style={{ maxWidth: 360 }}>{m.question}</td>
                  <td className="muted">{m.category || '—'}</td>
                  <td className="mono">
                    {m.outcomes.map((o, i) => (
                      <span key={o} style={{ marginRight: 10 }}>
                        {o} {Number(m.prices[i] ?? 0).toFixed(2)}
                      </span>
                    ))}
                  </td>
                  <td className="right">{fmt.usd(m.liquidity)}</td>
                  <td className="right">{fmt.usd(m.volume)}</td>
                  <td>
                    {m.resolved
                      ? <Badge kind="closed">resolved: {m.resolved_outcome}</Badge>
                      : <Badge kind="open">open</Badge>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
