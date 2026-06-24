import { useEffect, useMemo, useState } from 'react'
import { STRATEGIES, api, fmt } from '../api'
import { Loading, MultiLineChart, PageHead, PnL, Toast } from '../components/common.jsx'

const COLORS = {
  copy_sharp_wallets: '#36c275',
  fade_losing_wallets: '#ff5d6c',
  whale_shock_reversion: '#f2c14e',
  random_baseline: '#5b8cff',
  no_trade_baseline: '#8b93a3',
}
const short = (s) => s.replace(/_/g, ' ')

function drawdownCurve(curve) {
  let peak = -Infinity
  return (curve || [])
    .filter((p) => p.t != null)
    .map((p) => {
      peak = Math.max(peak, p.equity)
      const dd = peak > 0 ? ((peak - p.equity) / peak) * 100 : 0
      return { t: p.t, value: -dd }
    })
}

export default function Backtests() {
  const [cfg, setCfg] = useState({
    name: 'mock backtest',
    train_fraction: 0.5,
    category: '',
    min_wallet_score: 65,
    strategies: [...STRATEGIES],
  })
  const [current, setCurrent] = useState(null)
  const [history, setHistory] = useState([])
  const [categories, setCategories] = useState([])
  const [trades, setTrades] = useState([])
  const [tradeStrategy, setTradeStrategy] = useState('copy_sharp_wallets')
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [toast, setToast] = useState(null)

  useEffect(() => {
    Promise.all([api.backtests(), api.markets()])
      .then(([bt, markets]) => {
        setHistory(bt)
        setCategories([...new Set(markets.map((m) => m.category).filter(Boolean))].sort())
        if (bt[0]) return loadBacktest(bt[0].id)
      })
      .finally(() => setLoading(false))
  }, []) // eslint-disable-line

  const loadBacktest = async (id) => {
    const bt = await api.backtest(id)
    setCurrent(bt)
    const t = await api.backtestTrades(id, tradeStrategy)
    setTrades(t)
  }

  const run = async () => {
    setBusy(true)
    try {
      const body = {
        name: cfg.name,
        train_fraction: Number(cfg.train_fraction),
        min_wallet_score: Number(cfg.min_wallet_score),
        strategies: cfg.strategies,
      }
      if (cfg.category) body.category = cfg.category
      const bt = await api.runBacktest(body)
      setCurrent(bt)
      setHistory(await api.backtests())
      setTrades(await api.backtestTrades(bt.id, tradeStrategy))
      setToast({ msg: `Backtest #${bt.id} complete` })
    } catch (e) {
      setToast({ msg: e.message, err: true })
    } finally {
      setBusy(false)
    }
  }

  const toggleStrategy = (s) =>
    setCfg((c) => ({
      ...c,
      strategies: c.strategies.includes(s)
        ? c.strategies.filter((x) => x !== s)
        : [...c.strategies, s],
    }))

  useEffect(() => {
    if (current) api.backtestTrades(current.id, tradeStrategy).then(setTrades)
  }, [tradeStrategy]) // eslint-disable-line

  const equitySeries = useMemo(
    () =>
      (current?.results || []).map((r) => ({
        name: short(r.strategy),
        color: COLORS[r.strategy] || '#fff',
        curve: r.equity_curve.map((p) => ({ t: p.t, value: p.equity })),
      })),
    [current],
  )
  const drawdownSeries = useMemo(
    () =>
      (current?.results || []).map((r) => ({
        name: short(r.strategy),
        color: COLORS[r.strategy] || '#fff',
        curve: drawdownCurve(r.equity_curve),
      })),
    [current],
  )

  if (loading) return <Loading />

  const results = [...(current?.results || [])].sort((a, b) => b.roi - a.roi)

  return (
    <div>
      <PageHead title="Backtests" subtitle="Replay history: would copying have beaten the baselines?">
        <button onClick={run} disabled={busy || cfg.strategies.length === 0}>
          {busy ? 'Running…' : 'Run backtest'}
        </button>
      </PageHead>

      {/* config / filters */}
      <div className="panel">
        <h2>Configuration & filters</h2>
        <div className="grid-2">
          <div className="field">
            <label>Name</label>
            <input value={cfg.name} onChange={(e) => setCfg({ ...cfg, name: e.target.value })} />
          </div>
          <div className="field">
            <label>Train fraction (walk-forward split): {cfg.train_fraction}</label>
            <input type="range" min="0.2" max="0.8" step="0.05" value={cfg.train_fraction}
              onChange={(e) => setCfg({ ...cfg, train_fraction: e.target.value })} />
            <div className="hint">Wallet scores come from the first {Math.round(cfg.train_fraction * 100)}% of history; the rest is traded.</div>
          </div>
          <div className="field">
            <label>Category</label>
            <select value={cfg.category} onChange={(e) => setCfg({ ...cfg, category: e.target.value })}>
              <option value="">All categories</option>
              {categories.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </div>
          <div className="field">
            <label>Min wallet score (for copy strategy)</label>
            <input type="number" value={cfg.min_wallet_score}
              onChange={(e) => setCfg({ ...cfg, min_wallet_score: e.target.value })} />
          </div>
        </div>
        <div className="field">
          <label>Strategies</label>
          <div className="toolbar" style={{ flexWrap: 'wrap' }}>
            {STRATEGIES.map((s) => (
              <button key={s} className={`sm ${cfg.strategies.includes(s) ? '' : 'secondary'}`}
                onClick={() => toggleStrategy(s)} style={{ borderLeft: `3px solid ${COLORS[s]}` }}>
                {short(s)}
              </button>
            ))}
          </div>
        </div>
        {history.length > 0 && (
          <div className="field">
            <label>Load a past run</label>
            <select value={current?.id || ''} onChange={(e) => loadBacktest(Number(e.target.value))}>
              {history.map((h) => (
                <option key={h.id} value={h.id}>#{h.id} · {h.name} · {fmt.date(h.created_at)}</option>
              ))}
            </select>
          </div>
        )}
      </div>

      {!current ? (
        <div className="empty">Run a backtest to see results.</div>
      ) : (
        <>
          <div className="panel">
            <h2>Strategy comparison {current.config?.n_test_trades != null &&
              <span className="muted" style={{ fontWeight: 400 }}>
                · {current.config.n_train_trades} train / {current.config.n_test_trades} test trades
              </span>}</h2>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Strategy</th><th className="right">End bankroll</th><th className="right">PnL</th>
                    <th className="right">ROI</th><th className="right">Max DD</th><th className="right">Win%</th>
                    <th className="right">Trades</th><th className="right">Avg ret</th>
                    <th className="right">Best</th><th className="right">Worst</th>
                  </tr>
                </thead>
                <tbody>
                  {results.map((r) => (
                    <tr key={r.strategy}>
                      <td><span className="legend-dot" style={{ background: COLORS[r.strategy], marginRight: 8 }} />{short(r.strategy)}</td>
                      <td className="right">{fmt.usd(r.ending_bankroll)}</td>
                      <td className="right"><PnL value={r.total_pnl} fmtFn={fmt.usd} /></td>
                      <td className="right"><PnL value={r.roi * 100} fmtFn={fmt.pct} /></td>
                      <td className="right neg">{(r.max_drawdown * 100).toFixed(1)}%</td>
                      <td className="right">{(r.win_rate * 100).toFixed(1)}%</td>
                      <td className="right">{r.num_trades}</td>
                      <td className="right"><PnL value={r.avg_trade_return * 100} fmtFn={fmt.pct} /></td>
                      <td className="right pos">{fmt.usd(r.best_trade)}</td>
                      <td className="right neg">{fmt.usd(r.worst_trade)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="panel">
            <h2>Equity curves</h2>
            <MultiLineChart series={equitySeries} yLabel="bankroll ($)" />
          </div>

          <div className="panel">
            <h2>Drawdown (%)</h2>
            <MultiLineChart series={drawdownSeries} height={160} yLabel="drawdown (%)" />
          </div>

          <div className="panel">
            <h2>Trades</h2>
            <div className="toolbar" style={{ marginBottom: 12 }}>
              <select style={{ width: 220 }} value={tradeStrategy} onChange={(e) => setTradeStrategy(e.target.value)}>
                {(current.results || []).map((r) => (
                  <option key={r.strategy} value={r.strategy}>{short(r.strategy)} ({r.num_trades})</option>
                ))}
              </select>
              <span className="muted">{trades.length} trades</span>
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Closed</th><th>Market</th><th>Category</th><th>Outcome</th>
                    <th className="right">Entry</th><th className="right">Result</th>
                    <th className="right">Size</th><th className="right">PnL</th><th className="right">Return</th>
                  </tr>
                </thead>
                <tbody>
                  {trades.length === 0 && <tr><td colSpan="9" className="muted">No trades for this strategy.</td></tr>}
                  {trades.slice(0, 300).map((t) => (
                    <tr key={t.id}>
                      <td className="muted">{fmt.date(t.closed_at)}</td>
                      <td className="mono">{t.market_id}</td>
                      <td className="muted">{t.category || '—'}</td>
                      <td>{t.outcome}</td>
                      <td className="right">{fmt.price(t.entry_price)}</td>
                      <td className="right">{t.exit_price >= 1 ? 'win' : 'lose'}</td>
                      <td className="right">{fmt.usd(t.size)}</td>
                      <td className="right"><PnL value={t.pnl} fmtFn={fmt.usd2} /></td>
                      <td className="right"><PnL value={t.return_pct * 100} fmtFn={fmt.pct} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}

      <Toast message={toast?.msg} error={toast?.err} onDone={() => setToast(null)} />
    </div>
  )
}
