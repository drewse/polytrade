import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ExecutionReport } from './Btc5mExecutionLab.jsx'

const report = (over = {}) => ({
  ok: true, verdict_code: 2, verdict: 'execution helps but not enough',
  headline: 'passive (passive_2s) lifts EV to 0.03 from market -0.02 but not to significance',
  best_policy: { policy: 'passive_2s', ev_after_cost: 0.03, fill_rate: 0.18, significant: false, avg_spread_captured: 0.018 },
  execution_frontier: [
    { policy: 'market', fill_rate: 1.0, avg_fill_price: 0.52, avg_spread_captured: -0.02, ev_after_cost: -0.02, roi: -0.04, t_stat: -1.2, sharpe: -0.2, max_drawdown: 1.1, significant: false },
    { policy: 'passive_2s', fill_rate: 0.18, avg_fill_price: 0.48, avg_spread_captured: 0.018, ev_after_cost: 0.03, roi: 0.06, t_stat: 1.1, sharpe: 0.3, max_drawdown: 0.4, significant: false },
  ],
  fill_probability: { overall_5s_fill_rate: 0.21, hazard_lambda_per_s: 0.05,
    empirical_fill_rate: { '1.0': 0.05, '2.0': 0.12, '5.0': 0.21 },
    modelled_fill_rate: { '0.25': 0.01, '0.5': 0.02, '1.0': 0.05, '2.0': 0.1, '5.0': 0.22 },
    note: '1s/2s/5s empirical; 250ms/500ms modelled' },
  breakdowns: { by_regime: { chop: { signals: 20, fill_rate: 0.2, ev_after_cost: 0.01, significant: false },
    high_vol: { signals: 18, fill_rate: 0.3, ev_after_cost: 0.05, significant: true } } },
  promotion_experiment: { models_tested: 2, models_flipped_to_paper: 0, results: [
    { model: 'fair_value', n_signals: 40, market: { state: 'candidate', ev: -0.02, t: -1.2, fills: 40, fill_rate: 1.0 },
      passive: { state: 'candidate', ev: 0.03, t: 1.1, fills: 7, fill_rate: 0.18 }, flipped_to_paper: false },
  ] },
  research_answers: Array.from({ length: 7 }, (_, i) => ({ q: `Q${i + 1}?`, a: 'no', detail: 'd' })),
  approximations: ['fills from 1s historical trade stream (adverse selection)', '250ms modelled'],
  ...over,
})

describe('ExecutionReport', () => {
  it('renders verdict, frontier, fill curve, promotion, answers', () => {
    render(<ExecutionReport report={report()} />)
    expect(screen.getByTestId('execution-verdict')).toHaveTextContent('Execution helps but not enough')
    expect(screen.getByTestId('best-policy')).toHaveTextContent('passive_2s')
    expect(screen.getAllByTestId('frontier-row')).toHaveLength(2)
    expect(screen.getByTestId('fill-curve')).toHaveTextContent('5s')
    expect(screen.getByTestId('flips')).toHaveTextContent('0')
    expect(screen.getAllByTestId('promo-row')).toHaveLength(1)
    expect(screen.getByTestId('research-answers').querySelectorAll('li')).toHaveLength(7)
  })

  it('shows the tradeable-edge verdict', () => {
    render(<ExecutionReport report={report({ verdict_code: 1 })} />)
    expect(screen.getByTestId('execution-verdict')).toHaveTextContent('Execution creates a tradeable edge')
  })

  it('shows the not-the-bottleneck verdict', () => {
    render(<ExecutionReport report={report({ verdict_code: 3 })} />)
    expect(screen.getByTestId('execution-verdict')).toHaveTextContent('not the bottleneck')
  })

  it('renders empty state', () => {
    render(<ExecutionReport report={null} />)
    expect(screen.getByText(/run the simulation/i)).toBeInTheDocument()
  })
})
