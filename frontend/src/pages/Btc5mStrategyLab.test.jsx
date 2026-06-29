import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { LabReport, LeaderboardTable } from './Btc5mStrategyLab.jsx'

const report = (over = {}) => ({
  verdict_code: 1, headline: 'BTC price leads Polymarket repricing',
  best_strategy: { name: 'btc_lead#1', family: 'btc_lead', holdout_roi: 0.18, holdout_trades: 24,
    win_rate: 0.62, profit_factor: 1.8, robust_score: 42.1 },
  family_best_scores: { btc_lead: 42.1, flow_confirm: 8.2 }, n_accepted: 5,
  lag_analysis: { lag_vs_resolution_corr: 0.31 }, flow_imbalance_analysis: { flow_vs_resolution_corr: 0.12 },
  large_trade_analysis: { large_trade_dir_hit_rate: 0.58, baseline_dir_hit_rate: 0.51 }, ...over,
})

const rows = [
  { name: 'btc_lead#1', family: 'btc_lead', robust_score: 42.1, roi: 0.18, win_rate: 0.62,
    profit_factor: 1.8, trades: 24, max_drawdown: 1.2, avg_edge: 0.09, overfit: false },
]

describe('LabReport', () => {
  it('renders the verdict and best strategy', () => {
    render(<LabReport report={report()} />)
    expect(screen.getByTestId('verdict-banner')).toHaveTextContent('BTC price leads')
    expect(screen.getByTestId('lab-report')).toHaveTextContent('btc_lead#1')
  })

  it('shows the no-edge verdict', () => {
    render(<LabReport report={report({ verdict_code: 5, headline: 'no durable edge found', best_strategy: null })} />)
    expect(screen.getByTestId('verdict-banner')).toHaveTextContent('no durable edge found')
  })

  it('renders empty state without a report', () => {
    render(<LabReport report={null} />)
    expect(screen.getByText(/build the dataset/i)).toBeInTheDocument()
  })
})

describe('LeaderboardTable', () => {
  it('renders accepted strategies', () => {
    render(<LeaderboardTable rows={rows} testid="accepted-table" empty="none" />)
    expect(screen.getByTestId('accepted-table')).toBeInTheDocument()
    expect(screen.getAllByTestId('lab-row')).toHaveLength(1)
  })

  it('renders empty state', () => {
    render(<LeaderboardTable rows={[]} testid="x" empty="No accepted strategies yet" />)
    expect(screen.getByText(/No accepted strategies yet/)).toBeInTheDocument()
  })
})
