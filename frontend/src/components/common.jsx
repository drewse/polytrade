import { useEffect, useState } from 'react'

export function PageHead({ title, subtitle, children }) {
  return (
    <div className="page-head">
      <div>
        <h1>{title}</h1>
        {subtitle && <p>{subtitle}</p>}
      </div>
      <div className="toolbar">{children}</div>
    </div>
  )
}

export function Stat({ label, value, sub, tone }) {
  return (
    <div className="card">
      <div className="label">{label}</div>
      <div className={`value ${tone || ''}`}>{value}</div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  )
}

export function Badge({ kind, children }) {
  return <span className={`badge ${kind}`}>{children}</span>
}

export function PnL({ value, fmtFn }) {
  const cls = value > 0 ? 'pos' : value < 0 ? 'neg' : 'muted'
  const sign = value > 0 ? '+' : ''
  return <span className={cls}>{sign}{fmtFn ? fmtFn(value) : value}</span>
}

export function ScoreBar({ score }) {
  return (
    <span title={`score ${score}`}>
      <span className="score-bar">
        <span className="score-fill" style={{ width: `${Math.max(2, score)}%` }} />
      </span>
      {Number(score).toFixed(0)}
    </span>
  )
}

export function Loading() {
  return <div className="loading">Loading…</div>
}

export function Empty({ children }) {
  return <div className="empty">{children}</div>
}

export function Toast({ message, error, onDone }) {
  useEffect(() => {
    if (!message) return
    const t = setTimeout(onDone, 3000)
    return () => clearTimeout(t)
  }, [message, onDone])
  if (!message) return null
  return <div className={`toast ${error ? 'err' : ''}`}>{message}</div>
}

// Small hook: load data once, expose {data, loading, error, reload}.
export function useData(loader, deps = []) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const reload = () => {
    setLoading(true)
    loader()
      .then((d) => { setData(d); setError(null) })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }
  useEffect(reload, deps) // eslint-disable-line react-hooks/exhaustive-deps
  return { data, loading, error, reload }
}

// Multi-series time chart. `series` = [{name, color, curve: [{t, value}]}].
// `kind` controls light formatting only. Points with null `t` are dropped.
export function MultiLineChart({ series, height = 220, yLabel }) {
  const cleaned = (series || []).map((s) => ({
    ...s,
    pts: (s.curve || []).filter((p) => p.t != null).map((p) => ({ x: new Date(p.t).getTime(), y: p.value })),
  }))
  const allPts = cleaned.flatMap((s) => s.pts)
  if (allPts.length < 2) return <div className="muted">Not enough data to chart.</div>
  const xs = allPts.map((p) => p.x)
  const ys = allPts.map((p) => p.y)
  const xMin = Math.min(...xs), xMax = Math.max(...xs)
  const yMin = Math.min(...ys), yMax = Math.max(...ys)
  const w = 800, h = height, padL = 56, padB = 22, padT = 10, padR = 10
  const sx = (x) => padL + ((x - xMin) / (xMax - xMin || 1)) * (w - padL - padR)
  const sy = (y) => padT + (1 - (y - yMin) / (yMax - yMin || 1)) * (h - padT - padB)
  const ticks = 4
  return (
    <div>
      <svg viewBox={`0 0 ${w} ${h}`} style={{ width: '100%', height }}>
        {Array.from({ length: ticks + 1 }).map((_, i) => {
          const y = yMin + (i / ticks) * (yMax - yMin)
          return (
            <g key={i}>
              <line x1={padL} x2={w - padR} y1={sy(y)} y2={sy(y)} stroke="#28303d" strokeWidth="1" />
              <text x={4} y={sy(y) + 4} fill="#8b93a3" fontSize="10">
                {Math.round(y).toLocaleString()}
              </text>
            </g>
          )
        })}
        {cleaned.map((s) =>
          s.pts.length < 2 ? null : (
            <path
              key={s.name}
              d={s.pts.map((p, i) => `${i === 0 ? 'M' : 'L'} ${sx(p.x).toFixed(1)} ${sy(p.y).toFixed(1)}`).join(' ')}
              fill="none"
              stroke={s.color}
              strokeWidth="1.8"
            />
          ),
        )}
      </svg>
      <div className="legend">
        {cleaned.map((s) => (
          <span key={s.name} className="legend-item">
            <span className="legend-dot" style={{ background: s.color }} /> {s.name}
          </span>
        ))}
        {yLabel && <span className="muted" style={{ marginLeft: 'auto' }}>{yLabel}</span>}
      </div>
    </div>
  )
}

// Tiny inline SVG sparkline for the equity curve.
export function Sparkline({ points }) {
  if (!points || points.length < 2) return <div className="muted">Not enough data yet.</div>
  const ys = points.map((p) => p.equity)
  const min = Math.min(...ys)
  const max = Math.max(...ys)
  const range = max - min || 1
  const w = 600
  const h = 60
  const step = w / (points.length - 1)
  const path = points
    .map((p, i) => `${i === 0 ? 'M' : 'L'} ${(i * step).toFixed(1)} ${(h - ((p.equity - min) / range) * h).toFixed(1)}`)
    .join(' ')
  const up = ys[ys.length - 1] >= ys[0]
  return (
    <svg className="spark" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none">
      <path d={path} fill="none" stroke={up ? '#36c275' : '#ff5d6c'} strokeWidth="2" />
    </svg>
  )
}
