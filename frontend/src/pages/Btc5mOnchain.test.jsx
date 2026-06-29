import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { OnchainPanel, DiagnosticsPanel } from './Btc5mOnchain.jsx'

const diagnostics = (over = {}) => ({
  blocks_scanned: 1200, logs_scanned: 350, orderfilled_decoded: 4, events_matching_watched: 4,
  btc_token_map_matches: 3, ignored_by_reason: { 'price > max entry': 2 }, error_count: 0,
  last_block_scanned: 99887766, last_orderfilled: 'block 99887766 0x12ab…→0x34cd…',
  last_orderfilled_at: new Date(Date.now() - 5000).toISOString(),
  last_watched_event: 'block 99887766 0x4c94…  buy 12345678…',
  last_watched_event_at: new Date(Date.now() - 8000).toISOString(),
  last_btc_market_event: 'Bitcoin Up or Down 5m buy @ 0.5',
  last_btc_market_event_at: new Date(Date.now() - 9000).toISOString(),
  last_error: null, token_map: { size: 8, refreshed_at: new Date().toISOString(), error: null }, ...over,
})

const status = (over = {}) => ({
  enabled: true, paper_only: true, live_execution: false, running: false,
  rpc_connected: false, rpc_configured: true, token_map_size: 8, last_processed_block: 12345,
  watched_wallets: ['0x4c9497941333332d29f1c235dd23200f3623ffad'],
  exchanges: ['0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e'], signals_captured: 22, last_error: null,
  safety: 'PAPER-ONLY on-chain latency measurement',
  stats: {
    signals: 22, measured: 22, actionable_buys: 18, median_latency_s: 3.2, p90_latency_s: 4.5,
    worst_latency_s: 6.1, best_latency_s: 2.0, pct_under_5s: 86, pct_under_10s: 100,
    avg_abs_drift: 0.012, est_roi_loss_to_latency: 0.024, target_latency_s: 5,
    verdict: 'viable', recommendation: 'proceed to live micro-test V4',
  },
  diagnostics: diagnostics(), diagnosis: { code: 'detecting', message: 'detecting actionable signals (4)' },
  ...over,
})

const signals = {
  signals: [{ id: 1, block_number: 100, watched_wallet: '0x4c94', wallet_role: 'maker',
    question: 'Bitcoin Up or Down 5m', direction: 'YES', side: 'buy', price: 0.5,
    detection_latency_s: 3.1, price_drift: 0.01, seconds_until_expiry: 200, duration_minutes: 5,
    ignored_reason: null }],
  ignored: [{ id: 2, block_number: 101, watched_wallet: '0x4c94', wallet_role: 'taker',
    question: 'BTC 15m', side: 'buy', price: 0.75, detection_latency_s: 4.0, seconds_until_expiry: 100,
    ignored_reason: 'price > max entry' }],
}

describe('OnchainPanel', () => {
  it('renders and shows the paper-only badge (no live execution)', () => {
    render(<OnchainPanel status={status()} signals={signals} />)
    expect(screen.getByTestId('onchain-panel')).toBeInTheDocument()
    expect(screen.getByTestId('paper-badge')).toHaveTextContent('paper-only')
  })

  it('shows the verdict', () => {
    render(<OnchainPanel status={status()} signals={signals} />)
    expect(screen.getByTestId('onchain-verdict')).toHaveTextContent('VIABLE')
  })

  it('shows a not-viable verdict when latency is bad', () => {
    render(<OnchainPanel status={status({ stats: { ...status().stats, verdict: 'not_viable', median_latency_s: 40 } })} signals={signals} />)
    expect(screen.getByTestId('onchain-verdict')).toHaveTextContent('NOT VIABLE')
  })

  it('Start button calls onStart and is enabled when env-enabled and stopped', () => {
    const onStart = vi.fn()
    render(<OnchainPanel status={status()} signals={signals} onStart={onStart} />)
    const btn = screen.getByTestId('onchain-start')
    expect(btn).not.toBeDisabled()
    fireEvent.click(btn)
    expect(onStart).toHaveBeenCalled()
  })

  it('shows Stop when running and calls onStop', () => {
    const onStop = vi.fn()
    render(<OnchainPanel status={status({ running: true })} signals={signals} onStop={onStop} />)
    fireEvent.click(screen.getByTestId('onchain-stop'))
    expect(onStop).toHaveBeenCalled()
  })

  it('Run once calls onRunOnce', () => {
    const onRunOnce = vi.fn()
    render(<OnchainPanel status={status()} signals={signals} onRunOnce={onRunOnce} />)
    fireEvent.click(screen.getByTestId('onchain-runonce'))
    expect(onRunOnce).toHaveBeenCalled()
  })

  it('renders detected and ignored signal tables', () => {
    render(<OnchainPanel status={status()} signals={signals} />)
    expect(screen.getByTestId('onchain-signals')).toBeInTheDocument()
    expect(screen.getByTestId('onchain-ignored')).toBeInTheDocument()
    expect(screen.getAllByTestId('onchain-row')).toHaveLength(2)
  })

  it('disables Start when env-disabled', () => {
    render(<OnchainPanel status={status({ enabled: false })} signals={signals} />)
    expect(screen.getByTestId('onchain-start')).toBeDisabled()
  })

  it('renders the diagnostics panel', () => {
    render(<OnchainPanel status={status()} signals={signals} />)
    expect(screen.getByTestId('diagnostics-panel')).toBeInTheDocument()
  })
})

describe('DiagnosticsPanel', () => {
  it('shows the diagnosis banner and counters', () => {
    render(<DiagnosticsPanel diagnostics={diagnostics()} diagnosis={{ code: 'detecting', message: 'detecting actionable signals (4)' }} />)
    expect(screen.getByTestId('diagnosis-banner')).toHaveTextContent('detecting')
    expect(screen.getByText('1200')).toBeInTheDocument()   // blocks scanned
    expect(screen.getByText('350')).toBeInTheDocument()    // OrderFilled seen
  })

  it('surfaces "no watched trade" diagnosis', () => {
    render(<DiagnosticsPanel
      diagnostics={diagnostics({ events_matching_watched: 0, btc_token_map_matches: 0, orderfilled_decoded: 0 })}
      diagnosis={{ code: 'no_watched_trade', message: 'chain active but none from watched wallets' }} />)
    expect(screen.getByTestId('diagnosis-banner')).toHaveTextContent('no watched trade')
  })

  it('surfaces "token map issue" diagnosis and ignored reasons', () => {
    render(<DiagnosticsPanel
      diagnostics={diagnostics({ btc_token_map_matches: 0, ignored_by_reason: { 'token not in BTC up/down map': 3 } })}
      diagnosis={{ code: 'token_map_issue', message: 'watched traded but token not in BTC map' }} />)
    expect(screen.getByTestId('diagnosis-banner')).toHaveTextContent('token map issue')
    expect(screen.getByText(/token not in BTC up\/down map/)).toBeInTheDocument()
  })

  it('shows token-map error when present', () => {
    render(<DiagnosticsPanel
      diagnostics={diagnostics({ token_map: { size: 0, refreshed_at: null, error: 'gamma timeout' } })}
      diagnosis={{ code: 'rpc_log_issue', message: 'x' }} />)
    expect(screen.getByText(/gamma timeout/)).toBeInTheDocument()
  })
})
