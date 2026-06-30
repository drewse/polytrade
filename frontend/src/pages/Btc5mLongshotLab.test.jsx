import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { LongshotReport, CalibrationTable, GridTable } from './Btc5mLongshotLab.jsx'

const report = (over = {}) => ({
  ok: true, verdict_code: 2, headline: 'cheap-side edge at mid +0.0210/share (slope 0.78); mid<0.45 EV 0.03 (t=3.1, n=300)',
  calibration: {
    calibration_slope: 0.78, cheap_side_edge_at_mid: 0.021, interpretation: 'slope<1 ⇒ overreaction',
    bins: [
      { bin: '0.3-0.4', n: 200, implied_up: 0.35, actual_up: 0.42, mispricing: 0.07 },
      { bin: '0.6-0.7', n: 180, implied_up: 0.65, actual_up: 0.58, mispricing: -0.07 },
    ],
  },
  grid: [
    { execution: 'mid', max_entry: 0.45, n: 300, ev_per_trade: 0.03, win_rate: 0.46, roi: 0.07, t_stat: 3.1, prob_ev_positive: 0.99, significant: true },
    { execution: 'maker', max_entry: 0.45, n: 20, ev_per_trade: 0.05, win_rate: 0.5, roi: 0.1, t_stat: 1.2, prob_ev_positive: 0.85, significant: false },
    { execution: 'taker', max_entry: 0.50, n: 800, ev_per_trade: -0.04, win_rate: 0.45, roi: -0.08, t_stat: -2.5, prob_ev_positive: 0.01, significant: false },
  ],
  headline_cells: {
    mid_cheap: { ev_per_trade: 0.03, n: 300, t_stat: 3.1, prob_ev_positive: 0.99 },
    maker_cheap: { ev_per_trade: 0.05, n: 20, prob_ev_positive: 0.85 },
    taker_all: { ev_per_trade: -0.04 },
  },
  wallet_benchmark: { avg_entry: 0.43, profitable_wallets: 12 },
  ...over,
})

describe('LongshotReport', () => {
  it('renders verdict, edge, calibration, grid, benchmark', () => {
    render(<LongshotReport report={report()} />)
    expect(screen.getByTestId('longshot-verdict')).toHaveTextContent('Real mispricing')
    expect(screen.getByTestId('edge')).toHaveTextContent('0.021')
    expect(screen.getByTestId('calib-table')).toBeInTheDocument()
    expect(screen.getByTestId('grid-table')).toBeInTheDocument()
    expect(screen.getAllByTestId('grid-row')).toHaveLength(3)
    expect(screen.getByTestId('longshot-headline')).toHaveTextContent('cheap-side edge')
  })

  it('renders the no-edge verdict', () => {
    render(<LongshotReport report={report({ verdict_code: 4 })} />)
    expect(screen.getByTestId('longshot-verdict')).toHaveTextContent('No cheap-side mispricing')
  })

  it('renders the tradeable verdict', () => {
    render(<LongshotReport report={report({ verdict_code: 1 })} />)
    expect(screen.getByTestId('longshot-verdict')).toHaveTextContent('+EV as a maker')
  })

  it('renders empty state', () => {
    render(<LongshotReport report={null} />)
    expect(screen.getByText(/run the cheap-side test/i)).toBeInTheDocument()
  })
})

describe('CalibrationTable + GridTable', () => {
  it('renders calibration rows', () => {
    render(<CalibrationTable calib={report().calibration} />)
    expect(screen.getAllByTestId('calib-row')).toHaveLength(2)
  })
  it('renders grid rows', () => {
    render(<GridTable grid={report().grid} />)
    expect(screen.getAllByTestId('grid-row')).toHaveLength(3)
  })
})
