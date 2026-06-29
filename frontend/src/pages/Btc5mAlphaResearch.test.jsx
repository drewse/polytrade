import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ResearchReport, ModelLeaderboard } from './Btc5mAlphaResearch.jsx'

const report = (over = {}) => ({
  verdict_code: 2, verdict: 'predictive signal, not yet tradeable',
  headline: 'fair-value model has out-of-sample skill but post-cost EV not significant',
  fair_value: {
    ok: true, auc: 0.61, brier: 0.21, calibration_score: 0.16,
    ev: { ev_after_cost: 0.012, t_stat: 1.2, n_trades: 14, significant: false, ci_low: -0.01, ci_high: 0.03 },
    reliability: [{ bin: '0.0-0.2', predicted: 0.12, actual: 0.10, n: 5 }, { bin: '0.8-1.0', predicted: 0.88, actual: 0.80, n: 6 }],
    top_features: [{ feature: 'btc_ret_5s', importance: 0.3 }],
  },
  ensemble: {
    ok: true, members: [
      { perspective: 'price_action', weight: 0.4, holdout_brier: 0.20, auc: 0.62, ev: { ev_after_cost: 0.02, t_stat: 1.5, n_trades: 10, significant: false } },
      { perspective: 'wallet_behavior', weight: 0.3, holdout_brier: 0.23, auc: 0.55, ev: { ev_after_cost: 0.0, t_stat: 0.2, n_trades: 8, significant: false } },
    ],
    ensemble: { brier: 0.19, calibration_score: 0.24, auc: 0.64, n: 30, reliability: [] },
  },
  feature_discovery: { ok: true, generated: 60, n_stable: 5, promoted: [{ feature: 'sign[btc_ret_5s]', train_corr: 0.2, val_corr: 0.15 }], eliminated_redundant: [{ feature: 'sq[btc_ret_5s]', redundant_with: 'sign[btc_ret_5s]' }] },
  microstructure: { large_trade_impact: { impact_ratio: 1.4 }, spread: { expansion_ratio: 2.1 }, trade_clustering_index: 1.2, price_discovery_speed_s: 7 },
  cross_market: { btc_spot_lead: { peak_lag_s: 9, peak_corr: 0.07, leads: true }, by_duration: { 5: { price_informativeness: 0.4, btc_move_informativeness: 0.2, n: 100 } } },
  evolution: { best: { family: 'btc_lead', score: 12.3, holdout_roi: 0.05, holdout_trades: 10 }, evaluated: 200, generations: 4 },
  decay: { decayed: false, brier_degradation: 0.01 },
  ...over,
})

const models = [
  { name: 'fair_value', kind: 'fair_value', brier: 0.21, calibration_score: 0.16, auc: 0.61, ev_after_cost: 0.012, ev_t_stat: 1.2, n_trades: 14, significant: false, promoted: false },
  { name: 'ensemble', kind: 'ensemble', brier: 0.19, calibration_score: 0.24, auc: 0.64, ev_after_cost: 0.02, ev_t_stat: 2.1, n_trades: 12, significant: true, promoted: true },
]

describe('ResearchReport', () => {
  it('renders the verdict, fair-value, ensemble, discovery, micro/cross', () => {
    render(<ResearchReport report={report()} />)
    expect(screen.getByTestId('research-verdict')).toHaveTextContent('Predictive signal')
    expect(screen.getByTestId('fv-auc')).toHaveTextContent('0.61')
    expect(screen.getByTestId('fv-ev')).toHaveTextContent('0.0120')
    expect(screen.getByTestId('fv-reliability')).toBeInTheDocument()
    expect(screen.getAllByTestId('ensemble-row')).toHaveLength(2)
    expect(screen.getByTestId('discovered-features')).toHaveTextContent('sign[btc_ret_5s]')
    expect(screen.getByTestId('evolution-best')).toHaveTextContent('btc_lead')
  })

  it('shows the tradeable-edge verdict', () => {
    render(<ResearchReport report={report({ verdict_code: 1 })} />)
    expect(screen.getByTestId('research-verdict')).toHaveTextContent('Tradeable edge')
  })

  it('renders empty state without a report', () => {
    render(<ResearchReport report={null} />)
    expect(screen.getByText(/run the research pipeline/i)).toBeInTheDocument()
  })
})

describe('ModelLeaderboard', () => {
  it('renders trained models and the promoted flag', () => {
    render(<ModelLeaderboard models={models} />)
    expect(screen.getByTestId('model-leaderboard')).toBeInTheDocument()
    expect(screen.getAllByTestId('model-row')).toHaveLength(2)
    expect(screen.getByTestId('model-leaderboard')).toHaveTextContent('promoted')
  })

  it('renders empty state', () => {
    render(<ModelLeaderboard models={[]} />)
    expect(screen.getByText(/No models trained yet/)).toBeInTheDocument()
  })
})
