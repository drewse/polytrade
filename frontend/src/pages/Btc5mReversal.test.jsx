import { describe, it, expect } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { ModelLeaderboard, WalletIqCard, FeatureBar, ConsensusView, Dashboard } from './Btc5mReversal.jsx'

const W = '0x1111111111111111111111111111111111111111'
const W2 = '0x2222222222222222222222222222222222222222'

describe('BTC5M Reversal — pure components', () => {
  it('ModelLeaderboard renders model rows and marks the champion', () => {
    const rows = [
      { name: 'logistic_regression', accuracy: 0.71, precision: 0.7, recall: 0.72, f1: 0.71, cv_f1: 0.69, overfit_gap: 0.03, n_train: 100, n_test: 40, is_champion: true },
      { name: 'baseline_majority', accuracy: 0.52, precision: 0, recall: 0, f1: 0, cv_f1: 0, overfit_gap: 0, n_train: 100, n_test: 40, is_champion: false },
    ]
    render(<ModelLeaderboard rows={rows} />)
    expect(screen.getByTestId('model-table')).toBeInTheDocument()
    expect(screen.getAllByTestId('model-row')).toHaveLength(2)
    expect(screen.getByText('★ champion')).toBeInTheDocument()
  })

  it('WalletIqCard shows the IQ card with a clickable Polymarket wallet link', () => {
    const card = { wallet: W, strategy: 'Momentum', average_entry: '38 seconds after market open',
      average_hold: '4m 43s', average_confidence: 'high', strength: 'strong trending markets',
      weakness: 'range-bound markets', copy_confidence: 94, roi: 0.3, profit_factor: 2.1, win_rate: 0.6 }
    render(<WalletIqCard card={card} />)
    expect(screen.getByText('Momentum')).toBeInTheDocument()
    expect(screen.getByText(/IQ 94/)).toBeInTheDocument()
    const link = within(screen.getByTestId('iq-card')).getByRole('link')
    expect(link).toHaveAttribute('href', expect.stringContaining('polymarket.com/profile/'))
    expect(link).toHaveAttribute('target', '_blank')
  })

  it('FeatureBar renders one bar per feature', () => {
    render(<FeatureBar items={[{ feature: 'trend_slope', importance: 0.4 }, { feature: 'rsi', importance: 0.2 }]} />)
    expect(screen.getByTestId('feature-bars')).toBeInTheDocument()
    expect(screen.getByText('trend_slope')).toBeInTheDocument()
    expect(screen.getByText('rsi')).toBeInTheDocument()
  })

  it('ConsensusView renders consensus groups and follower edges with wallet links', () => {
    const data = {
      consensus_groups: [{ size: 2, profitable_together_pct: 92, wallets: [W, W2] }],
      followers: [{ wallet: W2, follows: W, lag_s: 12, agreement: 0.9 }],
      independent: [],
    }
    render(<ConsensusView data={data} />)
    expect(screen.getByTestId('consensus-group')).toBeInTheDocument()
    expect(screen.getByText(/92% together/)).toBeInTheDocument()
    expect(screen.getAllByTestId('follower-row')).toHaveLength(1)
  })

  it('Dashboard renders the key research metrics', () => {
    const d = {
      wallet_count: 12, profitable_wallets: 8, trade_count: 500, markets_indexed: 30,
      models_trained: 5, best_model: 'logistic_regression', best_model_accuracy: 0.7, best_model_f1: 0.69,
      largest_cluster: { cluster: 'Momentum', count: 5 }, consensus_opportunities: [{}, {}],
      top_features: [{ feature: 'rsi', importance: 0.3 }], leader_wallets: [], latest_signals: [],
      shadow_performance: { hit_rate: 0.6, resolved: 10 },
      safety: 'read-only research — never submits orders or affects live trading',
    }
    render(<Dashboard d={d} />)
    expect(screen.getByText('Wallets analyzed')).toBeInTheDocument()
    expect(screen.getByText('logistic_regression')).toBeInTheDocument()
    expect(screen.getByText(/read-only research/)).toBeInTheDocument()
  })
})
