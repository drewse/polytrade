import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { RegimeBars, MarketTable, WalletSpecTable, StrategyHeatmap, OriginalityTable, MarketDrilldown } from './MarketIntel.jsx'

const W = '0x1111111111111111111111111111111111111111'

describe('Market Intelligence — pure components', () => {
  it('RegimeBars renders one bar per regime', () => {
    render(<RegimeBars distribution={{ 'Strong Trend': 12, 'Range Bound': 5, 'High Volatility': 3 }} />)
    expect(screen.getByTestId('regime-bars')).toBeInTheDocument()
    expect(screen.getByText('Strong Trend')).toBeInTheDocument()
    expect(screen.getByText('Range Bound')).toBeInTheDocument()
  })

  it('MarketTable renders rows and drills down on click', () => {
    const onSelect = vi.fn()
    render(<MarketTable rows={[
      { market_id: 'm1', question: 'Bitcoin Up or Down 5m #1', regime: 'Strong Trend', secondary_regime: null,
        regime_confidence: 0.8, net_move: 0.33, prob_volatility: 0.02, total_volume: 1000, resolved: true, final_outcome: 'Up' },
    ]} onSelect={onSelect} />)
    expect(screen.getByTestId('market-table')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('market-row'))
    expect(onSelect).toHaveBeenCalledWith('m1')
  })

  it('WalletSpecTable links wallets to Polymarket and shows best regime + decay', () => {
    render(<WalletSpecTable rows={[
      { wallet: W, cluster: 'Momentum', best_regime: 'Breakout', specialization_score: 0.7,
        originality: { role: 'leader' }, originality_score: 0.8, position_size: { avg_stake: 5.2 }, decay: { trend: 'improving' } },
    ]} />)
    expect(screen.getByTestId('wallet-spec-table')).toBeInTheDocument()
    expect(screen.getByText('Breakout')).toBeInTheDocument()
    const link = within(screen.getByTestId('wallet-spec-row')).getByRole('link')
    expect(link).toHaveAttribute('href', expect.stringContaining('polymarket.com/profile/'))
  })

  it('StrategyHeatmap renders a regime grid', () => {
    render(<StrategyHeatmap rows={[
      { strategy_id: 1, name: 'Momentum', by_regime: { 'Strong Trend': { win_rate: 0.7, roi: 0.2, trades: 10 }, 'Range Bound': { win_rate: 0.4, roi: -0.1, trades: 8 } } },
    ]} />)
    expect(screen.getByTestId('heatmap-table')).toBeInTheDocument()
    expect(screen.getByTestId('heatmap-row')).toBeInTheDocument()
  })

  it('OriginalityTable ranks leaders/followers with delays', () => {
    render(<OriginalityTable rows={[
      { wallet: W, role: 'leader', originality_score: 0.9, leads: 10, follows: 1, avg_reaction_delay_s: null, repeated_follow_pct: 0 },
    ]} />)
    expect(screen.getByTestId('originality-table')).toBeInTheDocument()
    expect(screen.getByText('leader')).toBeInTheDocument()
  })

  it('MarketDrilldown shows regime + recommendation with wallet links', () => {
    render(<MarketDrilldown data={{
      market_id: 'm1', question: 'Bitcoin Up or Down 5m #1', primary_regime: 'Strong Trend', secondary_regime: 'Breakout',
      regime_confidence: 0.82, regime_evidence: { net_move: 0.33 }, resolved: true, final_outcome: 'Up',
      price: { opening_prob: 0.3, closing_prob: 0.63, net_move: 0.33, range: 0.33, prob_volatility: 0.02, vwap: 0.5 },
      volume: { total_volume: 1000, trade_count: 12 }, orderflow: { consensus_participation: 0.6, large_wallet_participation: 0.2 },
      recommendation: { regime: 'Strong Trend', best_wallets: [{ wallet: W, win_rate: 0.72 }], best_strategies: [{ name: 'Momentum', roi: 0.2 }], expected_edge: 0.1, research_confidence: 0.7 },
    }} onClose={() => {}} />)
    expect(screen.getByTestId('market-drilldown')).toBeInTheDocument()
    expect(screen.getAllByText(/Strong Trend/).length).toBeGreaterThan(0)
    const link = within(screen.getByTestId('market-drilldown')).getByRole('link')
    expect(link).toHaveAttribute('href', expect.stringContaining('polymarket.com/profile/'))
  })
})
