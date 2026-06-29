import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { PaperReport, GateProgress, QuotesTable } from './Btc5mPassiveMakerPaper.jsx'

const status = (over = {}) => ({
  enabled: false, status: 'research_only_not_validated',
  quotes: 220, fills: 15, skipped: 3, fill_rate: 0.068,
  ev_per_fill: 0.1525, ev_per_day_estimate: 1.4, prob_ev_positive: 0.88, ci95: [-0.097, 0.385],
  spread_captured: 0.011, adverse_selection: -0.05, weeks_covered: 1, fills_target: 100,
  gate: { min_100_fills: false, 'prob_ev_positive_ge_0.95': false, ci_strictly_above_zero: false,
    stable_across_2_weeks: false, worst_queue_positive: true, no_regime_over_60pct: true,
    ev_positive_excluding_top5: true },
  l2_book: { snapshots: 4, with_book: 0, errors: 4, capture_enabled: false },
  ...over,
})

const quotes = [
  { market_id: '0xabcdef1234', duration_minutes: 5, side: 'YES', quote_price: 0.47, best_bid: 0.46, best_ask: 0.5,
    status: 'filled', filled: true, fill_price: 0.47, realized_pnl: 0.53, spread_captured: 0.01, regime: 'mixed' },
  { market_id: '0x99887766', duration_minutes: 15, side: 'NO', quote_price: 0.48, best_bid: 0.47, best_ask: 0.51,
    status: 'expired', filled: false, realized_pnl: 0, reason_not_filled: 'no through', regime: 'chop' },
]

describe('PaperReport', () => {
  it('renders status, counts, P(EV>0), L2 status', () => {
    render(<PaperReport status={status()} />)
    expect(screen.getByTestId('paper-status')).toHaveTextContent('DISABLED')
    expect(screen.getByTestId('paper-status')).toHaveTextContent('not validated')
    expect(screen.getByTestId('quote-count')).toHaveTextContent('220')
    expect(screen.getByTestId('fill-count')).toHaveTextContent('15')
    expect(screen.getByTestId('p-ev')).toHaveTextContent('0.88')
    expect(screen.getByTestId('l2-status')).toHaveTextContent('4')
    expect(screen.getByTestId('gate-progress')).toBeInTheDocument()
  })

  it('renders the failed-validation state', () => {
    render(<PaperReport status={status({ status: 'failed_validation' })} />)
    expect(screen.getByTestId('paper-status')).toHaveTextContent('Failed validation')
  })

  it('renders the paper-validated state', () => {
    render(<PaperReport status={status({ status: 'paper_validated', enabled: true })} />)
    expect(screen.getByTestId('paper-status')).toHaveTextContent('Paper-validated')
    expect(screen.getByTestId('paper-status')).toHaveTextContent('ENABLED')
  })

  it('renders empty without status', () => {
    render(<PaperReport status={null} />)
    expect(screen.getByText(/No harness data yet/)).toBeInTheDocument()
  })
})

describe('GateProgress', () => {
  it('shows per-condition pass/fail and counts', () => {
    render(<GateProgress gate={status().gate} fills={15} target={100} />)
    expect(screen.getByTestId('gate-progress')).toHaveTextContent('15/100 fills')
    expect(screen.getAllByTestId('gate-row')).toHaveLength(7)
    expect(screen.getByTestId('gate-progress')).toHaveTextContent('P(EV>0) ≥ 0.95')
  })
})

describe('QuotesTable', () => {
  it('renders quotes/fills rows', () => {
    render(<QuotesTable rows={quotes} testid="pm-quotes" />)
    expect(screen.getByTestId('pm-quotes')).toBeInTheDocument()
    expect(screen.getAllByTestId('pm-row')).toHaveLength(2)
  })

  it('renders empty state', () => {
    render(<QuotesTable rows={[]} kind="fills" />)
    expect(screen.getByText(/No fills yet/)).toBeInTheDocument()
  })
})
