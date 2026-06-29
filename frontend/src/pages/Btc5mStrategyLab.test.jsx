import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { LabReport, LeaderboardTable } from './Btc5mStrategyLab.jsx'

const report = (over = {}) => ({
  verdict_code: 1, verdict: 'BTC-lead edge found', headline: 'BTC-lead edge: btc_lead#1 ...',
  btc_source_quality: { source: 'kraken_1s', resolution_s: 1, coverage_pct: 99.2, stale_s: 1200, is_true_1s: true },
  lag_report: { peak_lag_s: 2, peak_corr: 0.18, lag0_corr: 0.05, btc_leads: true,
    profile: { '0': 0.05, '1': 0.11, '2': 0.18, '3': 0.12, '4': 0.07 } },
  best_strategy: { name: 'btc_lead#1', family: 'btc_lead', holdout_roi: 0.18, holdout_trades: 24,
    win_rate: 0.62, profit_factor: 1.8, robust_score: 42.1,
    latency_curve: [{ latency_s: 0, roi: 0.18, trades: 24, avg_edge: 0.09 }, { latency_s: 3, roi: 0.04, trades: 24, avg_edge: 0.02 }] },
  family_best_scores: { btc_lead: 42.1, flow_confirm: 8.2 }, n_accepted: 5,
  lag_analysis: { lag_vs_resolution_corr: 0.31 }, flow_imbalance_analysis: { flow_vs_resolution_corr: 0.12 },
  large_trade_analysis: { large_trade_dir_hit_rate: 0.58, baseline_dir_hit_rate: 0.51 }, ...over,
})

const rows = [
  { name: 'btc_lead#1', family: 'btc_lead', robust_score: 42.1, roi: 0.18, win_rate: 0.62,
    profit_factor: 1.8, trades: 24, max_drawdown: 1.2, avg_edge: 0.09, overfit: false },
]

describe('LabReport', () => {
  it('renders the verdict, BTC source quality, lag report, and latency curve', () => {
    render(<LabReport report={report()} />)
    expect(screen.getByTestId('verdict-banner')).toHaveTextContent('BTC-lead edge')
    expect(screen.getByTestId('lab-report')).toHaveTextContent('kraken_1s')
    expect(screen.getByTestId('lag-peak')).toHaveTextContent('2s')
    expect(screen.getByTestId('lag-profile')).toBeInTheDocument()
    expect(screen.getByTestId('latency-curve')).toBeInTheDocument()
  })

  it('shows the data-insufficient verdict', () => {
    render(<LabReport report={report({ verdict_code: 4, headline: 'data still insufficient — coinbase_1m', best_strategy: null })} />)
    expect(screen.getByTestId('verdict-banner')).toHaveTextContent('data still insufficient')
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
