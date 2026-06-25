import { Component, useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { api } from './api'
import Overview from './pages/Overview.jsx'
import Wallets from './pages/Wallets.jsx'
import Signals from './pages/Signals.jsx'
import Positions from './pages/Positions.jsx'
import Markets from './pages/Markets.jsx'
import Discovery from './pages/Discovery.jsx'
import Backtests from './pages/Backtests.jsx'
import Settings from './pages/Settings.jsx'

const NAV = [
  { to: '/overview', label: 'Overview' },
  { to: '/discovery', label: 'Discovery' },
  { to: '/wallets', label: 'Wallets' },
  { to: '/signals', label: 'Signals' },
  { to: '/positions', label: 'Paper Positions' },
  { to: '/markets', label: 'Markets' },
  { to: '/backtests', label: 'Backtests' },
  { to: '/settings', label: 'Settings' },
]

function StatusBadges() {
  const [s, setS] = useState(null)
  const [err, setErr] = useState(false)
  useEffect(() => {
    let alive = true
    const tick = () =>
      api.status().then((d) => { if (alive) { setS(d); setErr(false) } }).catch(() => alive && setErr(true))
    tick()
    const id = setInterval(tick, 15000)
    return () => { alive = false; clearInterval(id) }
  }, [])

  if (err) return <div className="status-badges"><span className="src-badge error">⚠ API UNREACHABLE</span></div>
  if (!s) return null
  return (
    <div className="status-badges">
      {s.data_mode === 'live' ? (
        <span className="src-badge live">● LIVE READ-ONLY DATA</span>
      ) : (
        <span className="src-badge mock">● MOCK DATA</span>
      )}
      {!s.ok && <span className="src-badge error" title={s.error || ''}>⚠ API ERROR</span>}
      {s.stale && <span className="src-badge stale">⏱ STALE DATA</span>}
      {s.partial_wallets > 0 && (
        <span className="src-badge partial" title="Live wallet stats are recent-window only">
          ◑ PARTIAL WALLET HISTORY ({s.partial_wallets})
        </span>
      )}
    </div>
  )
}

// Keeps a render error in one page from unmounting the whole app (black screen).
class ErrorBoundary extends Component {
  state = { error: null }
  static getDerivedStateFromError(error) {
    return { error }
  }
  render() {
    if (this.state.error) {
      return (
        <div className="empty">
          Something went wrong rendering this page: {this.state.error.message || 'unknown error'}
        </div>
      )
    }
    return this.props.children
  }
}

export default function App() {
  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          Copy Lab
          <small>Polymarket paper trading</small>
        </div>
        {NAV.map((n) => (
          <NavLink key={n.to} to={n.to} className="nav-link">
            {n.label}
          </NavLink>
        ))}
        <StatusBadges />
        <div className="paper-badge">📝 PAPER TRADING ONLY — no real orders, no keys, read-only</div>
      </aside>
      <main className="main">
        <ErrorBoundary>
        <Routes>
          <Route path="/" element={<Navigate to="/overview" replace />} />
          <Route path="/overview" element={<Overview />} />
          <Route path="/wallets" element={<Wallets />} />
          <Route path="/signals" element={<Signals />} />
          <Route path="/positions" element={<Positions />} />
          <Route path="/markets" element={<Markets />} />
          <Route path="/discovery" element={<Discovery />} />
          <Route path="/backtests" element={<Backtests />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
        </ErrorBoundary>
      </main>
    </div>
  )
}
