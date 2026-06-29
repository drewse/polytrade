import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { PaperReport, GateProgress, QuotesTable, FunnelDiagnostics, FamilyBreakdown } from './Btc5mPassiveMakerPaper.jsx'

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

const diag = (over = {}) => ({
  forward_enabled: false, pipeline_blocked: true, blocked_stages: ['4_paper_quotes'],
  main_ingest: { running: true }, last_run_at: new Date().toISOString(),
  last_summary: { new_indexed: 0, new_quotes: 0 },
  funnel: {
    '1_btc_markets_in_main': { total: 400, new_since_last: 0, latest_ts: null, blocked: false },
    '2_btc5m_indexed': { total: 246, new_since_last: 0, latest_ts: null, blocked: true },
    '3_lab_markets': { total: 226, new_since_last: 0, latest_ts: null, blocked: false },
    '4_paper_quotes': { total: 0, new_since_last: 0, latest_ts: null, blocked: true },
    '5_paper_fills': { total: 0, new_since_last: 0, latest_ts: null, blocked: false },
    '6_settled_fills': { total: 0, new_since_last: 0, latest_ts: null, blocked: false },
  },
  ...over,
})

describe('FunnelDiagnostics', () => {
  it('renders funnel stages + stall warning', () => {
    render(<FunnelDiagnostics diag={diag()} />)
    expect(screen.getByTestId('funnel-table')).toBeInTheDocument()
    expect(screen.getAllByTestId('funnel-row').length).toBeGreaterThanOrEqual(6)
    expect(screen.getByTestId('stall-warning')).toHaveTextContent('STALLED')
  })

  it('no stall warning when pipeline healthy', () => {
    render(<FunnelDiagnostics diag={diag({ pipeline_blocked: false, blocked_stages: [] })} />)
    expect(screen.queryByTestId('stall-warning')).toBeNull()
  })

  it('renders empty without diag', () => {
    render(<FunnelDiagnostics diag={null} />)
    expect(screen.getByText(/No forward-pipeline diagnostics/)).toBeInTheDocument()
  })
})

describe('FamilyBreakdown — BTC vs broad separation', () => {
  const bd = {
    'btc:independent': { quotes: 220, fills: 7, ev_per_fill: 0.017, prob_ev_positive: 0.55, gate_passed: 1, gate_total: 7, gate_status: 'research_only_not_validated' },
    'btc:multi_point': { quotes: 400, fills: 15, ev_per_fill: 0.1, prob_ev_positive: 0.8, gate_passed: 2, gate_total: 7, gate_status: 'research_only_not_validated' },
    'sports:independent': { quotes: 120, fills: 30, ev_per_fill: -0.02, prob_ev_positive: 0.3, gate_passed: 1, gate_total: 7, gate_status: 'research_only_not_validated' },
  }
  it('renders each cohort with its own gate, BTC marked', () => {
    render(<FamilyBreakdown breakdown={bd} />)
    expect(screen.getByTestId('family-breakdown')).toHaveTextContent('btc:independent')
    expect(screen.getByTestId('family-breakdown')).toHaveTextContent('THE gate')
    expect(screen.getAllByTestId('cohort-row')).toHaveLength(3)
  })
  it('renders empty', () => {
    render(<FamilyBreakdown breakdown={{}} />)
    expect(screen.getByText(/No cohorts yet/)).toBeInTheDocument()
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
