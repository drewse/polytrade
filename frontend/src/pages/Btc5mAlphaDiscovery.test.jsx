import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { DiscoveryReport, ModelGenerations } from './Btc5mAlphaDiscovery.jsx'

const report = (over = {}) => ({
  ok: true, generation: 3, verdict_code: 2, verdict: 'predictive alpha, not yet tradeable',
  headline: '120 stable features mined; retrained model AUC 0.71 but post-cost EV not significant',
  mining: { generated: 1400, evaluated: 220, survived: 38, by_category: { interaction: 12, regime: 8 } },
  top_features: [
    { name: 'x[lag*btc_ret_5s]', category: 'interaction', ic: 0.18, mutual_info: 0.02, shap: 0.07,
      stability_splits: 1.0, stability_regime: 0.75, stability_month: 0.6, decay: 0.2 },
  ],
  new_alpha: ['sign[lag]', 'wallet_x_btc'],
  gained_power: [{ name: 'flow_entropy', ic_change: 0.04 }],
  lost_power: [{ name: 'sq[btc_vol]', ic_change: -0.05 }],
  model: { ok: true, lifecycle_state: 'candidate', vs_prev: 'improved',
    promotion_reason: 'EV after costs not statistically significant',
    metrics: { auc: 0.71, ev_after_cost: -0.02, ev_t_stat: -1.1 } },
  cross_market: { assets: { ETH: { n_markets: 6, avg_peak_lag_s: 8, avg_peak_corr: 0.06, leads_fraction: 0.5, leads: true },
    SOL: { n_markets: 0, leads: false } } },
  external_leads: ['ETH'],
  data_gaps: ['raw L2 order book', 'funding'],
  promotion_rules: 'promote to PAPER only if significant +EV after costs...',
  ...over,
})

const gens = [
  { generation: 3, name: 'fair_value_mined', n_features: 24, auc: 0.71, ev_after_cost: -0.02, ev_t_stat: -1.1,
    regime_stability: 0.6, decay: 0.03, lifecycle_state: 'candidate', vs_prev: 'improved' },
  { generation: 2, name: 'fair_value_mined', n_features: 20, auc: 0.68, ev_after_cost: 0.01, ev_t_stat: 1.0,
    regime_stability: 0.5, decay: 0.04, lifecycle_state: 'demoted', vs_prev: 'degraded' },
]

describe('DiscoveryReport', () => {
  it('renders generation, mining, top features, lifecycle, cross-asset', () => {
    render(<DiscoveryReport report={report()} />)
    expect(screen.getByTestId('discovery-verdict')).toHaveTextContent('Generation 3')
    expect(screen.getByTestId('mined-count')).toHaveTextContent('38')
    expect(screen.getByTestId('model-lifecycle')).toHaveTextContent('candidate')
    expect(screen.getAllByTestId('feature-row')).toHaveLength(1)
    expect(screen.getByTestId('new-alpha')).toHaveTextContent('wallet_x_btc')
  })

  it('shows promoted-to-paper verdict', () => {
    render(<DiscoveryReport report={report({ verdict_code: 1 })} />)
    expect(screen.getByTestId('discovery-verdict')).toHaveTextContent('Alpha promoted to paper')
  })

  it('renders empty state without a report', () => {
    render(<DiscoveryReport report={null} />)
    expect(screen.getByText(/run a generation/i)).toBeInTheDocument()
  })
})

describe('ModelGenerations', () => {
  it('renders generations with lifecycle', () => {
    render(<ModelGenerations generations={gens} />)
    expect(screen.getByTestId('model-gens')).toBeInTheDocument()
    expect(screen.getAllByTestId('gen-row')).toHaveLength(2)
    expect(screen.getByTestId('model-gens')).toHaveTextContent('demoted')
  })

  it('renders empty state', () => {
    render(<ModelGenerations generations={[]} />)
    expect(screen.getByText(/No model generations yet/)).toBeInTheDocument()
  })
})
